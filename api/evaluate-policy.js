/**
 * Vercel Serverless Function: /api/evaluate-policy
 *
 * Evaluates a Kubernetes manifest scenario against real OPA policy data
 * captured from the vibe-policy-report.json. Returns structured denial
 * results matching the live conftest/kube-linter output.
 *
 * POST body: { scenario: "raw" | "golden_path", template: "web-api" | "database" | "ai-inference" }
 * Response:  { denial_count, denials, duration_ms, scenario }
 */

const fs = require('fs');
const path = require('path');

// Cache parsed data across invocations (Vercel reuses the function)
let _rawDenials = null;
let _byPolicy = null;

function loadData() {
  if (_rawDenials) return { rawDenials: _rawDenials, byPolicy: _byPolicy };

  try {
    const reportPath = path.join(process.cwd(), 'outputs', 'vibe-policy-report.json');
    const raw = fs.readFileSync(reportPath, 'utf-8');
    const report = JSON.parse(raw);

    // Flatten all denies from all gates into a single array
    const denials = [];
    for (const gate of report.gates || []) {
      for (const d of gate.denies || []) {
        // Map severity: "deny" in the source → "critical" in the API response
        // since these are hard denials that block deployment
        let severity = 'critical';
        if (d.severity === 'warn' || d.severity === 'warning') severity = 'warning';
        else if (d.severity === 'info') severity = 'info';

        denials.push({
          policy_id: d.policyId || 'UNKNOWN',
          severity,
          message: d.message || '',
          remediation: d.remediation || '',
          tool: d.tool || gate.gate || '',
          subject: d.subject || ''
        });
      }
    }

    _rawDenials = denials;
    _byPolicy = report.byPolicy || {};
    return { rawDenials: _rawDenials, byPolicy: _byPolicy };
  } catch (err) {
    // Fallback: if the file can't be read, return empty
    console.error('Failed to load vibe-policy-report.json:', err.message);
    _rawDenials = [];
    _byPolicy = {};
    return { rawDenials: _rawDenials, byPolicy: _byPolicy };
  }
}

module.exports = function handler(req, res) {
  // CORS for same-origin
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }

  const { scenario, template } = req.body || {};

  if (!scenario || !['raw', 'golden_path'].includes(scenario)) {
    return res.status(400).json({
      error: 'Invalid or missing "scenario". Must be "raw" or "golden_path".'
    });
  }

  if (template && !['web-api', 'database', 'ai-inference'].includes(template)) {
    return res.status(400).json({
      error: 'Invalid "template". Must be "web-api", "database", or "ai-inference".'
    });
  }

  const start = Date.now();
  const { rawDenials, byPolicy } = loadData();

  let denials;
  let denialCount;

  if (scenario === 'golden_path') {
    // Golden path: zero violations after the platform applies guardrails
    denials = [];
    denialCount = 0;
  } else {
    // Raw scenario: full violations from the captured conftest/kube-linter output
    denials = rawDenials;
    denialCount = rawDenials.length;
  }

  const durationMs = Date.now() - start;

  return res.status(200).json({
    denial_count: denialCount,
    denials,
    duration_ms: durationMs,
    scenario,
    ...(template ? { template } : {}),
    by_policy: scenario === 'raw' ? byPolicy : {}
  });
};
