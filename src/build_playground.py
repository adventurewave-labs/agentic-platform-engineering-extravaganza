#!/usr/bin/env python3
"""
Generate the data behind the policy playground on the showcase page.

The page lets a visitor walk a manifest from "what the model handed back" to
"what the golden path renders", one control at a time, and watch the denial
list shrink. Every list it shows is a real conftest evaluation captured here at
build time -- the browser is replaying recorded verdicts, not approximating
Rego in JavaScript.

    python3 src/build_playground.py    # writes outputs/playground.json
    python3 src/build_site.py          # re-renders index.html from it
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import gates  # noqa: E402
import renderer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

REQUEST = ("service called payments-ledger PCI EU high volume postgres "
           "staging and prod")


# Each step is one thing a platform team encodes once so nobody types it again.
STEPS: list[dict[str, Any]] = [
    {"key": "raw", "label": "What the model handed back",
     "detail": "No platform. A plausible, runnable, non-compliant Deployment."},
    {"key": "registry", "label": "Pin the image, use the approved registry",
     "detail": "NW-K8S-001, NW-K8S-008 — immutable tag from ghcr.io/northwind-retail."},
    {"key": "labels", "label": "Attach ownership and cost attribution",
     "detail": "NW-K8S-003 — who owns this, which cost centre pays, what classification."},
    {"key": "resources", "label": "Declare a resource envelope",
     "detail": "NW-K8S-002 — requests and limits on every container."},
    {"key": "security", "label": "Harden the pod security context",
     "detail": "NW-K8S-004 — non-root, no privilege escalation, read-only rootfs, drop ALL."},
    {"key": "probes", "label": "Add health probes",
     "detail": "NW-K8S-005 — without these, Argo Rollouts cannot verify a canary."},
    {"key": "secrets", "label": "Move credentials to a Secret reference",
     "detail": "NW-K8S-006 — no literal passwords in the pod spec."},
    {"key": "availability", "label": "Meet the production availability floor",
     "detail": "NW-K8S-007 — more than one replica, spread across zones."},
    {"key": "pci", "label": "Apply the PCI control set",
     "detail": "NW-PCI-001/005/006 — default-deny NetworkPolicy, audit sink, no SA token."},
]


def build_stage(req: renderer.ServiceRequest, upto: int) -> list[dict[str, Any]]:
    """Apply the first `upto` corrections to the unguided manifests."""
    docs = copy.deepcopy(renderer.vibe_manifests(req))
    applied = {s["key"] for s in STEPS[1:upto + 1]}

    for doc in docs:
        if doc["kind"] not in ("Deployment", "StatefulSet"):
            continue
        meta = doc["metadata"]
        pod = doc["spec"]["template"]["spec"]
        containers = pod.get("containers", [])
        is_db = doc["kind"] == "StatefulSet"

        if "registry" in applied:
            for c in containers:
                c["image"] = (f"{renderer.APPROVED_REGISTRY}/postgres:17.4.0" if is_db
                              else f"{renderer.APPROVED_REGISTRY}/{req.name}:1.4.2")

        if "labels" in applied:
            meta.setdefault("labels", {}).update({
                "app.kubernetes.io/name": meta["name"],
                "backstage.io/component": req.name,
                "northwind.io/owner": req.owner_short,
                "northwind.io/cost-center": req.cost_center,
                "northwind.io/data-classification": req.classification,
                "northwind.io/environment": "prod",
            })

        if "resources" in applied:
            for c in containers:
                c["resources"] = {
                    "limits": {"cpu": req.cpu, "memory": req.memory},
                    "requests": {"cpu": req.cpu_request, "memory": req.memory_request},
                }

        if "security" in applied:
            pod.setdefault("securityContext", {}).update({
                "runAsNonRoot": True, "runAsUser": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            })
            for c in containers:
                c.setdefault("securityContext", {}).update({
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                })

        if "probes" in applied and doc["kind"] == "Deployment":
            for c in containers:
                probe = {"httpGet": {"path": "/healthz", "port": req.port},
                         "initialDelaySeconds": 5, "periodSeconds": 10}
                c["readinessProbe"] = copy.deepcopy(probe)
                c["livenessProbe"] = {**copy.deepcopy(probe), "initialDelaySeconds": 15}
        elif "probes" in applied:
            for c in containers:
                exec_probe = {"exec": {"command": ["pg_isready"]}, "periodSeconds": 10}
                c["readinessProbe"] = copy.deepcopy(exec_probe)
                c["livenessProbe"] = copy.deepcopy(exec_probe)

        if "secrets" in applied:
            for c in containers:
                new_env = []
                for e in c.get("env", []):
                    if "value" in e and any(
                            h in e["name"].lower()
                            for h in ("password", "secret", "token", "url")):
                        new_env.append({"name": e["name"], "valueFrom": {
                            "secretKeyRef": {"name": f"{req.name}-db", "key": "password"}}})
                    else:
                        new_env.append(e)
                c["env"] = new_env

        if "availability" in applied and doc["kind"] == "Deployment":
            doc["spec"]["replicas"] = 3

        if "pci" in applied:
            meta.setdefault("annotations", {})[
                "northwind.io/network-policy"] = f"{req.name}-default-deny"
            doc["spec"]["template"].setdefault("metadata", {}).setdefault(
                "annotations", {})["northwind.io/audit-sink"] = "loki://platform/payments"
            pod["automountServiceAccountToken"] = False

    if "pci" in applied:
        docs.append(renderer._network_policy(
            req, "prod",
            {"app.kubernetes.io/name": req.name,
             "northwind.io/data-classification": req.classification}))
    return docs


def main() -> int:
    req = agent.parse_intent(REQUEST)
    stages = []
    for i, step in enumerate(STEPS):
        docs = build_stage(req, i)
        report = gates.run_all(docs, include_scanners=True)
        stages.append({
            "key": step["key"],
            "label": step["label"],
            "detail": step["detail"],
            "denyCount": len(report.findings),
            "byPolicy": report.by_policy(),
            "findings": [
                {"id": f.policy_id,
                 "tool": f.tool,
                 "subject": f.subject,
                 "message": f.message.split("->")[0].strip(),
                 "fix": f.remediation}
                for f in report.findings
            ],
        })
        print(f"  {step['key']:14} {len(report.findings):>3} denials", file=sys.stderr)

    # The golden path, for the final panel: render, then run the same
    # remediation loop the demo runs, so the panel reflects the state a change
    # would actually be merged in.
    import costing
    gp = req
    iterations = 0
    for iterations in range(1, 6):
        spec, docs = renderer.golden_path(gp, "prod")
        final = gates.run_all(docs, score=spec, cost=costing.estimate(gp, "prod"))
        if final.passed:
            break
        gp, decisions = agent.remediate(gp, final.findings, "prod")
        if not decisions:
            break
    print(f"  {'goldenPath':14} {len(final.findings):>3} denials "
          f"after {iterations} iteration(s)", file=sys.stderr)

    payload = {
        "request": REQUEST,
        "stages": stages,
        "goldenPath": {
            "denyCount": len(final.findings),
            "iterations": iterations,
            "manifestKinds": sorted({f"{d['kind']}" for d in docs}),
            "objectCount": len(docs),
            "monthlyCostUsd": costing.estimate(gp, "prod")["totalMonthlyCostUsd"],
        },
        "toolVersions": {k: v for k, v in gates.tool_versions().items()
                         if k in ("conftest", "score-k8s", "kube-linter")},
    }
    out = ROOT / "outputs" / "playground.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\n  wrote {out.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
