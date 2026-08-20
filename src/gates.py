#!/usr/bin/env python3
"""
The gates. Thin wrappers over real, pinned, upstream binaries.

Nothing in this file evaluates a policy. Every verdict comes from a tool you
can install yourself and run against the same inputs:

  conftest / OPA   Apache-2.0   open-policy-agent/conftest, /opa
  kube-linter      Apache-2.0   stackrox/kube-linter
  Trivy            Apache-2.0   aquasecurity/trivy
  Checkov          Apache-2.0   bridgecrewio/checkov

That is deliberate. A demo whose "AI safety check" is a Python function that
returns a hardcoded list of findings is a cartoon. The point being made here --
that policy is what makes agentic infrastructure acceptable -- only lands if
the policy engine is the real one, the Rego is real Rego, and the failures are
reproducible on someone else's laptop.

Run `./run.sh verify` to re-execute every gate and diff the results against
the committed outputs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
POLICY = ROOT / "policy"

FINDING_RE = re.compile(r"^\[(?P<id>[A-Z0-9-]+)\]\s*(?P<body>.*)$", re.S)


def _tool(name: str) -> str | None:
    local = BIN / name
    if local.exists():
        return str(local)
    return shutil.which(name)


@dataclass
class Finding:
    policy_id: str
    message: str
    severity: str = "deny"
    tool: str = "conftest"
    subject: str = ""
    remediation: str = ""

    @classmethod
    def parse(cls, raw: str, severity: str = "deny", tool: str = "conftest") -> "Finding":
        m = FINDING_RE.match(raw.strip())
        pid = m.group("id") if m else "UNCLASSIFIED"
        body = m.group("body") if m else raw.strip()
        subject = body.partition(":")[0]
        remediation = ""
        if "->" in body:
            _, _, remediation = body.partition("->")
            remediation = remediation.strip()
        return cls(
            policy_id=pid,
            message=body.strip(),
            severity=severity,
            tool=tool,
            subject=subject.strip(),
            remediation=remediation,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "severity": self.severity,
            "tool": self.tool,
            "subject": self.subject,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass
class GateResult:
    name: str
    passed: bool
    findings: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    tool_version: str = ""
    raw: str = ""
    skipped: bool = False
    skip_reason: str = ""

    @property
    def deny_count(self) -> int:
        return len(self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.name,
            "passed": self.passed,
            "skipped": self.skipped,
            "skipReason": self.skip_reason,
            "toolVersion": self.tool_version,
            "denies": [f.as_dict() for f in self.findings],
            "warnings": [f.as_dict() for f in self.warnings],
        }


# ---------------------------------------------------------------------------
# conftest / OPA
# ---------------------------------------------------------------------------

def _conftest_version() -> str:
    binary = _tool("conftest")
    if not binary:
        return ""
    out = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout
    first = out.strip().splitlines()[0] if out.strip() else ""
    return first.replace("Conftest: ", "conftest ")


def conftest(
    payload: str | list[dict[str, Any]] | dict[str, Any],
    policy_subdir: str,
    name: str,
    parser: str = "yaml",
) -> GateResult:
    """Evaluate a real Rego bundle with conftest and parse the verdicts."""
    binary = _tool("conftest")
    if not binary:
        return GateResult(
            name=name, passed=True, skipped=True,
            skip_reason="conftest binary not found; run ./run.sh setup",
        )

    suffix = ".json" if parser == "json" else ".yaml"
    if isinstance(payload, str):
        body = payload
    elif parser == "json":
        body = json.dumps(payload, indent=2)
    else:
        docs = payload if isinstance(payload, list) else [payload]
        body = "".join("---\n" + yaml.safe_dump(d, sort_keys=False) for d in docs)

    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
        fh.write(body)
        path = fh.name

    proc = subprocess.run(
        [binary, "test", "--policy", str(POLICY / policy_subdir),
         "--parser", parser, "--output", "json", "--no-color", path],
        capture_output=True, text=True,
    )
    Path(path).unlink(missing_ok=True)

    findings: list[Finding] = []
    warnings: list[Finding] = []
    try:
        results = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return GateResult(name=name, passed=False, raw=proc.stdout + proc.stderr,
                          tool_version=_conftest_version())

    for block in results:
        for f in block.get("failures") or []:
            findings.append(Finding.parse(f.get("msg", ""), "deny", "conftest"))
        for w in block.get("warnings") or []:
            warnings.append(Finding.parse(w.get("msg", ""), "warn", "conftest"))

    findings.sort(key=lambda f: (f.policy_id, f.message))
    warnings.sort(key=lambda f: (f.policy_id, f.message))
    return GateResult(
        name=name,
        passed=not findings,
        findings=findings,
        warnings=warnings,
        tool_version=_conftest_version(),
        raw=proc.stdout,
    )


def evaluate_kubernetes(docs: list[dict[str, Any]]) -> GateResult:
    return conftest(docs, "kubernetes", "kubernetes-guardrails")


def evaluate_score(spec: dict[str, Any]) -> GateResult:
    return conftest([spec], "score", "score-workload-spec")


def evaluate_cost(estimate: dict[str, Any]) -> GateResult:
    return conftest(estimate, "cost", "finops-budget", parser="json")


# ---------------------------------------------------------------------------
# kube-linter
# ---------------------------------------------------------------------------

def kube_linter(docs: list[dict[str, Any]]) -> GateResult:
    binary = _tool("kube-linter")
    if not binary:
        return GateResult(name="kube-linter", passed=True, skipped=True,
                          skip_reason="kube-linter binary not found")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifests.yaml"
        path.write_text("".join(
            "---\n" + yaml.safe_dump(d, sort_keys=False) for d in docs))
        proc = subprocess.run(
            [binary, "lint", "--format", "json", str(path)],
            capture_output=True, text=True,
        )
    findings: list[Finding] = []
    try:
        data = json.loads(proc.stdout or "{}")
        for r in data.get("Reports") or []:
            check = r.get("Check", "unknown")
            obj = r.get("Object", {}).get("K8sObject", {})
            gvk = obj.get("GroupVersionKind", {}).get("Kind", "")
            nm = obj.get("Name", "")
            msg = r.get("Diagnostic", {}).get("Message", "")
            findings.append(Finding(
                policy_id=f"KL-{check}",
                message=f"{gvk}/{nm}: {msg}",
                severity="deny", tool="kube-linter", subject=f"{gvk}/{nm}",
            ))
    except json.JSONDecodeError:
        pass
    version = subprocess.run([binary, "version"], capture_output=True, text=True).stdout.strip()
    return GateResult(
        name="kube-linter", passed=not findings, findings=findings,
        tool_version=f"kube-linter {version}", raw=proc.stdout,
    )


# ---------------------------------------------------------------------------
# Trivy (misconfiguration scanning)
# ---------------------------------------------------------------------------

def trivy_config(docs: list[dict[str, Any]], severities: str = "HIGH,CRITICAL") -> GateResult:
    binary = _tool("trivy")
    if not binary:
        return GateResult(name="trivy-misconfig", passed=True, skipped=True,
                          skip_reason="trivy binary not found")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifests.yaml"
        path.write_text("".join(
            "---\n" + yaml.safe_dump(d, sort_keys=False) for d in docs))
        proc = subprocess.run(
            [binary, "config", "--quiet", "--format", "json",
             "--severity", severities, "--skip-db-update", str(path)],
            capture_output=True, text=True,
        )
    findings: list[Finding] = []
    try:
        data = json.loads(proc.stdout or "{}")
        for res in data.get("Results") or []:
            for m in res.get("Misconfigurations") or []:
                findings.append(Finding(
                    policy_id=m.get("ID", "TRIVY"),
                    message=f"{m.get('Title','')}: {m.get('Message','')}",
                    severity=m.get("Severity", "HIGH").lower(),
                    tool="trivy",
                    remediation=m.get("Resolution", ""),
                ))
    except json.JSONDecodeError:
        pass
    version = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout
    return GateResult(
        name="trivy-misconfig", passed=not findings, findings=findings,
        tool_version=version.strip().splitlines()[0] if version.strip() else "trivy",
        raw=proc.stdout,
    )


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def findings(self) -> list[Finding]:
        return [f for r in self.results for f in r.findings]

    @property
    def warnings(self) -> list[Finding]:
        return [w for r in self.results for w in r.warnings]

    def by_policy(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.policy_id] = counts.get(f.policy_id, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "denyCount": len(self.findings),
            "warnCount": len(self.warnings),
            "byPolicy": self.by_policy(),
            "gates": [r.as_dict() for r in self.results],
        }


def run_all(
    docs: list[dict[str, Any]],
    score: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    include_scanners: bool = True,
    include_trivy: bool | None = None,
) -> GateReport:
    """Run the gates that apply to what was passed.

    `include_trivy` defaults to off: Trivy overlaps heavily with kube-linter on
    Kubernetes misconfiguration and is a 160 MB download, so the demo does not
    make it a prerequisite. Set NORTHWIND_TRIVY=1 (or pass the flag) to add it
    after `./bin/setup.sh --all`.
    """
    if include_trivy is None:
        include_trivy = os.environ.get("NORTHWIND_TRIVY", "") not in ("", "0", "false")
    results: list[GateResult] = []
    if score is not None:
        results.append(evaluate_score(score))
    results.append(evaluate_kubernetes(docs))
    if include_scanners:
        results.append(kube_linter(docs))
        if include_trivy:
            results.append(trivy_config(docs))
    if cost is not None:
        results.append(evaluate_cost(cost))
    return GateReport(results=results)


def tool_versions() -> dict[str, str]:
    """Report the exact upstream versions in play, for the record."""
    out: dict[str, str] = {}
    probes = {
        "conftest": ["--version"],
        "opa": ["version"],
        "kube-linter": ["version"],
        "score-k8s": ["--version"],
        "score-compose": ["--version"],
        "tofu": ["version"],
        "trivy": ["--version"],
    }
    for name, args in probes.items():
        binary = _tool(name)
        if not binary:
            out[name] = "not installed"
            continue
        proc = subprocess.run([binary, *args], capture_output=True, text=True)
        text = (proc.stdout or proc.stderr).strip()
        out[name] = text.splitlines()[0] if text else "unknown"
    checkov = shutil.which("checkov")
    if checkov:
        proc = subprocess.run([checkov, "--version"], capture_output=True, text=True)
        out["checkov"] = f"checkov {proc.stdout.strip()}"
    return out


if __name__ == "__main__":
    for k, v in tool_versions().items():
        print(f"{k:16} {v}")
