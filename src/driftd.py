#!/usr/bin/env python3
"""
Day-2: the drift reconciliation agent.

Golden paths get a service created. What kills platforms is month four, when
the estate has quietly diverged from what git says is true and nobody knows
which of the two is correct.

This module models a small, honest slice of that: observed cluster state versus
the desired state rendered from git, with attribution. The observed state is a
fixture (`platform/observed-state.yaml`) rather than a live cluster, because
the argument here is about the *shape* of the loop -- detect, explain, evaluate
against policy, propose a reconciling change, and stop -- not about who is
holding the kubeconfig.

The important constraint is what the agent is not allowed to do. `drift-agent`
holds `platform.open_pull_request` and does not hold any tool that mutates a
cluster. It cannot "just fix it". Everything it proposes lands as a diff a
human merges, which means the git history stays the truth and the audit trail
survives the agent being wrong.

In a live estate you would wire the observation half to Argo CD's
`get_application_resource_tree` (via argoproj-labs/mcp-for-argocd, Apache-2.0)
or to the Flux Operator MCP server, and the reasoning half to k8sgpt or
HolmesGPT. The verdict layer -- this file -- does not change.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
OBSERVED = ROOT / "platform" / "observed-state.yaml"


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _load_observed() -> dict[str, Any]:
    if not OBSERVED.exists():
        return {}
    return yaml.safe_load(OBSERVED.read_text()) or {}


def detect(service: str, environment: str = "prod") -> dict[str, Any]:
    """Return every way observed state diverges from desired state."""
    observed = _load_observed()
    key = f"{service}/{environment}"
    record = (observed.get("workloads") or {}).get(key)
    if not record:
        return {
            "service": service,
            "environment": environment,
            "observed": False,
            "note": f"no observed state on file for {key}",
            "drift": [],
        }

    desired = record.get("desired", {})
    actual = record.get("observed", {})
    attribution = record.get("attribution", {})

    drift: list[dict[str, Any]] = []
    for field, want in desired.items():
        got = actual.get(field)
        if got == want:
            continue
        meta = attribution.get(field, {})
        drift.append({
            "field": field,
            "desired": want,
            "observed": got,
            "severity": meta.get("severity", "medium"),
            "changedBy": meta.get("changedBy", "unknown"),
            "changedAt": meta.get("changedAt", "unknown"),
            "via": meta.get("via", "unknown"),
            "impact": meta.get("impact", ""),
            "remediation": meta.get("remediation", f"reset {field} to {want!r} in git"),
            "policyId": meta.get("policyId", ""),
        })

    drift.sort(key=lambda d: SEVERITY_ORDER.get(d["severity"], 9))
    return {
        "service": service,
        "environment": environment,
        "observed": True,
        "syncStatus": "OutOfSync" if drift else "Synced",
        "lastReconciled": record.get("lastReconciled", "unknown"),
        "driftCount": len(drift),
        "highestSeverity": drift[0]["severity"] if drift else "none",
        "drift": drift,
    }


def remediation_patch(service: str, environment: str, report: dict[str, Any]) -> dict[str, Any]:
    """Build the reconciling change. A diff, not an apply."""
    patch: dict[str, Any] = {}
    for d in report.get("drift", []):
        patch[d["field"]] = d["desired"]
    return {
        "service": service,
        "environment": environment,
        "branch": f"drift/{service}-{environment}-reconcile",
        "title": f"fix({service}): reconcile {len(patch)} drifted field(s) in {environment}",
        "patch": patch,
        "requiresHumanApproval": True,
        "rationale": [
            f"{d['field']}: {d['observed']!r} -> {d['desired']!r} "
            f"(changed by {d['changedBy']} via {d['via']}; {d['impact']})"
            for d in report.get("drift", [])
        ],
    }


def main() -> int:
    service = sys.argv[1] if len(sys.argv) > 1 else "payments-ledger"
    environment = sys.argv[2] if len(sys.argv) > 2 else "prod"
    report = detect(service, environment)
    print(yaml.safe_dump(report, sort_keys=False))
    if report.get("drift"):
        print("--- proposed reconciliation ---")
        print(yaml.safe_dump(remediation_patch(service, environment, report), sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
