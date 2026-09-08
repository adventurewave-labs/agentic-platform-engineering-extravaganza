/**
 * Vercel Serverless Function: /api/evaluate-policy
 *
 * Executes REAL conftest + OPA policy evaluation against Kubernetes manifests
 * at request time. Bundles the conftest v0.53.0 binary (with OPA v0.65.0).
 *
 * POST body: {
 *   scenario: "raw" | "golden_path",
 *   template?: "web-api" | "database" | "ai-inference",
 *   manifest?: string  (raw YAML to evaluate — stretch goal)
 * }
 *
 * Response: {
 *   denial_count, denials[], duration_ms, scenario,
 *   engine, by_policy, successes
 * }
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile } = require('child_process');
const { promisify } = require('util');

const execFileAsync = promisify(execFile);

// ─── Configuration ──────────────────────────────────────────────────────────

const CONFTEST_BIN = path.join(process.cwd(), 'bin', 'conftest');
const POLICY_DIR   = path.join(process.cwd(), 'policy');
const OUTPUTS_DIR  = path.join(process.cwd(), 'outputs');
const ENGINE_LABEL = 'conftest 0.53.0 / OPA 0.65.0';
const CONFTEST_TIMEOUT_MS = 30000;

const SCENARIO_MANIFEST = {
  raw:         'unguided-manifests.yaml',
  golden_path: 'final-manifests.yaml',
};

// ─── Denial message parser ─────────────────────────────────────────────────
//
// conftest failure messages follow the convention encoded in our Rego:
//   [NW-K8S-XXX] Subject: what's wrong -> how to fix it
//
// Examples:
//   [NW-K8S-001] Deployment/payments-ledger container "payments-ledger": image "payments-ledger:latest" is not immutably pinned -> pin to a semver tag or @sha256 digest in score.yaml containers.payments-ledger.image
//   [NW-K8S-003] Deployment/payments-ledger: missing required label "app.kubernetes.io/name" -> the golden-path scaffolder injects this from catalog-info.yaml; re-run scaffolder.execute with the owning Group set
//   [NW-K8S-004] Deployment/payments-ledger: pod may run as root -> set securityContext.runAsNonRoot=true (golden-path default; someone overrode it)

function parseDenial(rawMsg) {
  const msg = rawMsg || '';

  // 1. Extract policy_id: [NW-K8S-XXX]
  const policyIdMatch = msg.match(/\[(NW-K8S-\d+)\]/);
  const policy_id = policyIdMatch ? policyIdMatch[1] : 'UNKNOWN';

  // 2. Strip the leading [POLICY_ID] to get the rest
  const afterPolicyId = policyIdMatch ? msg.slice(policyIdMatch.index + policyIdMatch[0].length).trim() : msg;

  // 3. Split on ' -> ' to separate message from remediation
  const arrowIdx = afterPolicyId.indexOf(' -> ');
  let messagePart, remediation;
  if (arrowIdx !== -1) {
    messagePart = afterPolicyId.slice(0, arrowIdx).trim();
    remediation = afterPolicyId.slice(arrowIdx + 4).trim();
  } else {
    messagePart = afterPolicyId;
    remediation = '';
  }

  // 4. Extract subject (Kind/Name pattern) from the message part
  //    Format: "Deployment/name container ...: description" or "Deployment/name: description"
  const subjectMatch = messagePart.match(/^((?:Deployment|StatefulSet|DaemonSet|Job|CronJob|Pod|Service|Ingress|ConfigMap|Secret|Namespace)\/\S+?)(?:\s+container\s+"[^"]*")?(?::\s|\s)/);
  let subject = '';
  let message = messagePart;
  if (subjectMatch) {
    subject = subjectMatch[1];
    // The message is everything after the subject + optional container + ": "
    const afterSubject = messagePart.slice(subjectMatch.index + subjectMatch[0].length);
    // If the match ended with ": ", the message is already trimmed
    // If it ended with just space (before "container"), we need more work
    if (subjectMatch[0].endsWith(': ')) {
      message = afterSubject.trim();
    } else {
      // Try to find ": " after the container clause
      const colonIdx = messagePart.indexOf(': ', subjectMatch.index + subjectMatch[0].length);
      if (colonIdx !== -1) {
        message = messagePart.slice(colonIdx + 2).trim();
      }
    }
  }

  return {
    policy_id,
    severity: 'critical',   // conftest 'deny' → critical per real policy behavior
    message,
    remediation,
    subject,
  };
}

// ─── conftest executor ──────────────────────────────────────────────────────

async function runConftest(manifestPath) {
  const args = ['test', '--policy', POLICY_DIR, manifestPath, '--output', 'json'];

  let stdout;
  try {
    const result = await execFileAsync(CONFTEST_BIN, args, {
      timeout: CONFTEST_TIMEOUT_MS,
      maxBuffer: 10 * 1024 * 1024,  // 10 MB — large manifests can produce big JSON
    });
    stdout = result.stdout;
  } catch (err) {
    // conftest exits non-zero when there are policy violations — that's EXPECTED.
    // stdout still contains the JSON results we need.
    if (err.stdout) {
      stdout = err.stdout;
    } else if (err.killed) {
      throw Object.assign(new Error('conftest evaluation timed out'), { status: 504 });
    } else {
      // If we have no stdout at all, this is a real error
      throw Object.assign(new Error(`conftest execution failed: ${err.message}`), { status: 500 });
    }
  }

  // Parse conftest JSON output
  let conftestResults;
  try {
    conftestResults = JSON.parse(stdout);
  } catch (parseErr) {
    throw Object.assign(new Error(`Failed to parse conftest output: ${parseErr.message}`), { status: 500 });
  }

  // conftest returns an array of { filename, namespace, successes, failures }
  let totalSuccesses = 0;
  const allFailures = [];

  for (const result of conftestResults) {
    totalSuccesses += result.successes || 0;
    for (const f of result.failures || []) {
      allFailures.push(f);
    }
  }

  // Parse each failure into the API contract format
  const denials = allFailures.map(f => parseDenial(f.msg || f.message || String(f)));

  // Build by_policy breakdown
  const byPolicy = {};
  for (const d of denials) {
    byPolicy[d.policy_id] = (byPolicy[d.policy_id] || 0) + 1;
  }

  return {
    denial_count: denials.length,
    denials,
    successes: totalSuccesses,
    by_policy: byPolicy,
  };
}

// ─── Temp file helper (for custom manifest evaluation) ─────────────────────

function writeTempManifest(yamlContent) {
  const tmpDir = os.tmpdir();
  const tmpFile = path.join(tmpDir, `conftest-manifest-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.yaml`);
  fs.writeFileSync(tmpFile, yamlContent, 'utf-8');
  return tmpFile;
}

// ─── Handler ────────────────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }

  const { scenario, template, manifest } = req.body || {};

  // ── Validate conftest binary exists ──
  if (!fs.existsSync(CONFTEST_BIN)) {
    console.error('conftest binary not found at:', CONFTEST_BIN);
    return res.status(500).json({
      error: 'Policy evaluation engine not available',
      detail: `conftest binary not found at ${CONFTEST_BIN}`,
    });
  }

  // ── Validate scenario ──
  if (!scenario || !['raw', 'golden_path'].includes(scenario)) {
    return res.status(400).json({
      error: 'Invalid or missing "scenario". Must be "raw" or "golden_path".',
    });
  }

  // ── Validate template (optional) ──
  if (template && !['web-api', 'database', 'ai-inference'].includes(template)) {
    return res.status(400).json({
      error: 'Invalid "template". Must be "web-api", "database", or "ai-inference".',
    });
  }

  // ── Determine manifest to evaluate ──
  let manifestPath;
  let cleanupTemp = null;

  if (manifest && typeof manifest === 'string' && manifest.trim().length > 0) {
    // Custom manifest: write to temp file
    try {
      manifestPath = writeTempManifest(manifest);
      cleanupTemp = manifestPath;
    } catch (err) {
      return res.status(500).json({
        error: 'Failed to write temporary manifest file',
        detail: err.message,
      });
    }
  } else {
    // Scenario-based manifest selection
    const manifestFile = SCENARIO_MANIFEST[scenario];
    manifestPath = path.join(OUTPUTS_DIR, manifestFile);

    if (!fs.existsSync(manifestPath)) {
      return res.status(400).json({
        error: `Manifest file not found for scenario "${scenario}"`,
        detail: `Expected at ${manifestPath}`,
      });
    }
  }

  // ── Execute conftest ──
  const start = Date.now();
  let result;

  try {
    result = await runConftest(manifestPath);
  } catch (err) {
    // Clean up temp file if we created one
    if (cleanupTemp) {
      try { fs.unlinkSync(cleanupTemp); } catch (_) { /* ignore */ }
    }

    const status = err.status || 500;
    return res.status(status).json({
      error: err.message,
      scenario,
      engine: ENGINE_LABEL,
    });
  }

  const durationMs = Date.now() - start;

  // ── Clean up temp file if we created one ──
  if (cleanupTemp) {
    try { fs.unlinkSync(cleanupTemp); } catch (_) { /* ignore */ }
  }

  // ── Build response ──
  return res.status(200).json({
    denial_count: result.denial_count,
    denials: result.denials,
    duration_ms: durationMs,
    successes: result.successes,
    scenario,
    engine: ENGINE_LABEL,
    by_policy: result.by_policy,
    ...(template ? { template } : {}),
  });
};
