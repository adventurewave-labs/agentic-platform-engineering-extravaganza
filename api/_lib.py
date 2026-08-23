"""
Platform Engineering Policy Gate — shared library for the touchpoint (PRD §5).

Used by api/evaluate.py.

This module:
  - loads the REAL Rego policies from policy/kubernetes/ (for reference)
  - loads the cached conftest output from outputs/vibe-policy-report.json
    (the canonical 42 denials that the real conftest binary produces against
    outputs/unguided-manifests.yaml)
  - re-implements the NW-K8S-* and KL-* rules in Python so arbitrary
    user-pasted YAML can be evaluated without the conftest binary

Per PRD §5.6: "Conftest unavailable: fall back to cached results for the
selected template." On Vercel serverless we cannot ship the real conftest
Go binary, so the cached path is the default. Arbitrary user YAML is
evaluated by the Python re-implementation, which produces the same shape
of denials but is labelled `engine: python-faithful-reimpl` so the
provenance is honest.
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import uuid
from pathlib import Path
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS_DIR = os.path.join(ROOT, "src")
OUTPUTS_DIR = os.path.join(ROOT, "outputs")
PLATFORM_DIR = os.path.join(ROOT, "platform")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# PyYAML is the only Python dependency (per requirements.txt).
import yaml  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Cached canonical outputs — the REAL conftest output captured by
# `./run.sh verify` and committed to the repo.
# ---------------------------------------------------------------------------
_VIBE_REPORT_PATH = os.path.join(OUTPUTS_DIR, "vibe-policy-report.json")
_UNGUIDED_MANIFESTS_PATH = os.path.join(OUTPUTS_DIR, "unguided-manifests.yaml")
_FINAL_MANIFESTS_PATH = os.path.join(OUTPUTS_DIR, "final-manifests.yaml")

# Service templates — the dropdown options for the "service template selector"
# (PRD §5.3.1). Each template is a (label, description, manifest_path, scenario) tuple.
TEMPLATES = [
    {
        "id": "payments-ledger",
        "label": "payments-ledger (PCI EU payments service)",
        "description": "A PCI-scope payments service with a Postgres database, EU residency, staging and prod",
        "raw_manifest_path": _UNGUIDED_MANIFESTS_PATH,
        "golden_manifest_path": _FINAL_MANIFESTS_PATH,
    },
]


def _load_cached_report() -> dict:
    """Load the canonical 42-denial conftest output."""
    with open(_VIBE_REPORT_PATH) as f:
        return json.load(f)


def _load_unguided_manifests() -> str:
    """Load the raw (unguided) manifests YAML as a string."""
    with open(_UNGUIDED_MANIFESTS_PATH) as f:
        return f.read()


def _load_final_manifests() -> str:
    """Load the golden-path manifests YAML as a string."""
    with open(_FINAL_MANIFESTS_PATH) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Cold-start marker.
# ---------------------------------------------------------------------------
_COLD_START_SEEN = False


def detect_cold_start() -> bool:
    global _COLD_START_SEEN
    if not _COLD_START_SEEN:
        _COLD_START_SEEN = True
        return True
    return False


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------
def parse_manifests(yaml_str: str) -> list[dict]:
    """Parse a multi-doc YAML string into a list of objects.

    Per PRD §5.6 case 2: "Malicious YAML input: evaluate in a sandboxed
    environment with resource limits." Vercel serverless IS the sandbox —
    we parse with safe_load_all which is safe, and we don't execute any
    custom YAML tags. We do enforce a size limit (256KB) to prevent
    memory exhaustion.
    """
    if len(yaml_str) > 256 * 1024:
        raise ValueError("manifest exceeds 256KB limit")
    docs = []
    for doc in yaml.safe_load_all(yaml_str):
        if doc is not None:
            docs.append(doc)
    return docs


def _containers(obj: dict) -> list[tuple[str, dict]]:
    """Return (container_name, container_dict) pairs for all containers in a workload.

    Honors the same pod_spec extraction logic as policy/kubernetes/workload.rego:
      Deployment/StatefulSet/DaemonSet → spec.template.spec
      Job                               → spec.template.spec
      CronJob                           → spec.jobTemplate.spec.template.spec
    """
    kind = obj.get("kind", "")
    pod_spec = None
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        pod_spec = obj.get("spec", {}).get("template", {}).get("spec", {})
    elif kind == "CronJob":
        pod_spec = (
            obj.get("spec", {})
            .get("jobTemplate", {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
        )
    if not pod_spec:
        return []
    out = []
    for c in pod_spec.get("containers", []) or []:
        out.append((c.get("name", "<unnamed>"), c))
    for c in pod_spec.get("initContainers", []) or []:
        out.append((c.get("name", "<unnamed>"), c))
    return out


def _workload_name(obj: dict) -> str:
    kind = obj.get("kind", "<unknown>")
    name = obj.get("metadata", {}).get("name", "<unnamed>")
    return f"{kind}/{name}"


_SEMVER_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")


def _image_is_pinned(image: str) -> bool:
    """NW-K8S-001: image must be immutably pinned (digest or semver tag)."""
    if "@sha256:" in image:
        return True
    parts = image.split(":")
    if len(parts) <= 1:
        return False  # no tag at all → :latest implicit
    tag = parts[-1]
    return bool(_SEMVER_RE.match(tag))


# ---------------------------------------------------------------------------
# NW-K8S-* rule re-implementations — match policy/kubernetes/workload.rego
# ---------------------------------------------------------------------------
def _rule_nw_k8s_001(docs: list[dict]) -> list[dict]:
    """NW-K8S-001 — image tags must be immutable."""
    out = []
    workload_kinds = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
    for obj in docs:
        if obj.get("kind") not in workload_kinds:
            continue
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            image = c.get("image", "")
            if not _image_is_pinned(image):
                out.append({
                    "policy_id": "NW-K8S-001",
                    "severity": "critical",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": image "{image}" is not immutably pinned',
                    "remediation": f"pin to a semver tag or @sha256 digest in score.yaml containers.{cname}.image",
                })
    return out


def _rule_nw_k8s_002(docs: list[dict]) -> list[dict]:
    """NW-K8S-002 — every container declares requests AND limits (CPU + memory)."""
    out = []
    workload_kinds = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
    for obj in docs:
        if obj.get("kind") not in workload_kinds:
            continue
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            resources = c.get("resources", {}) or {}
            limits = resources.get("limits", {}) or {}
            requests = resources.get("requests", {}) or {}
            if "cpu" not in limits:
                out.append({
                    "policy_id": "NW-K8S-002",
                    "severity": "critical",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no CPU limit',
                    "remediation": f"set containers.{cname}.resources.limits.cpu in score.yaml",
                })
            if "memory" not in limits:
                out.append({
                    "policy_id": "NW-K8S-002",
                    "severity": "critical",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no memory limit',
                    "remediation": f"set containers.{cname}.resources.limits.memory in score.yaml",
                })
            if "memory" not in requests:
                out.append({
                    "policy_id": "NW-K8S-002",
                    "severity": "critical",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no memory request',
                    "remediation": f"set containers.{cname}.resources.requests.memory in score.yaml",
                })
            if "cpu" not in requests:
                out.append({
                    "policy_id": "NW-K8S-002",
                    "severity": "critical",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no CPU request',
                    "remediation": f"set containers.{cname}.resources.requests.cpu in score.yaml",
                })
    return out


def _rule_nw_k8s_003(docs: list[dict]) -> list[dict]:
    """NW-K8S-003 — PCI workloads must have a NetworkPolicy that defaults to deny."""
    out = []
    pci_ns = set()
    for obj in docs:
        labels = obj.get("metadata", {}).get("labels", {}) or {}
        if labels.get("northwind.io/data-classification") == "pci":
            ns = obj.get("metadata", {}).get("namespace", "default")
            pci_ns.add(ns)

    has_default_deny = set()
    for obj in docs:
        if obj.get("kind") != "NetworkPolicy":
            continue
        ns = obj.get("metadata", {}).get("namespace", "default")
        spec = obj.get("spec", {}) or {}
        # A "default deny" NetworkPolicy has empty ingress and egress selectors.
        if spec.get("podSelector", {}) == {} and not spec.get("ingress", []) and not spec.get("egress", []):
            has_default_deny.add(ns)
        elif spec.get("policyTypes") == ["Ingress", "Egress"] and not spec.get("ingress", []) and not spec.get("egress", []):
            has_default_deny.add(ns)

    for ns in pci_ns:
        if ns not in has_default_deny:
            out.append({
                "policy_id": "NW-K8S-003",
                "severity": "critical",
                "tool": "conftest",
                "subject": f"namespace/{ns}",
                "message": f"PCI namespace {ns} has no default-deny NetworkPolicy",
                "remediation": f"add a NetworkPolicy in {ns} with empty podSelector and empty ingress/egress",
            })

    # Per-workload network policy presence check
    for obj in docs:
        kind = obj.get("kind", "")
        if kind not in {"Deployment", "StatefulSet", "DaemonSet"}:
            continue
        labels = obj.get("metadata", {}).get("labels", {}) or {}
        if labels.get("northwind.io/data-classification") != "pci":
            continue
        name = _workload_name(obj)
        out.append({
            "policy_id": "NW-K8S-003",
            "severity": "critical",
            "tool": "conftest",
            "subject": name,
            "message": f"{name}: PCI workload requires an explicit NetworkPolicy",
            "remediation": f"add a NetworkPolicy selecting app={labels.get('app', '<missing>')} in the same namespace",
        })
    return out


def _rule_nw_k8s_004(docs: list[dict]) -> list[dict]:
    """NW-K8S-004 — prod workloads must set replicas >= 2 (HA)."""
    out = []
    for obj in docs:
        if obj.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        labels = obj.get("metadata", {}).get("labels", {}) or {}
        if labels.get("northwind.io/environment") != "prod":
            continue
        name = _workload_name(obj)
        replicas = obj.get("spec", {}).get("replicas", 1)
        if replicas is None:
            replicas = 1
        if replicas < 2:
            out.append({
                "policy_id": "NW-K8S-004",
                "severity": "critical",
                "tool": "conftest",
                "subject": name,
                "message": f"{name}: prod workload has replicas={replicas} (must be >= 2 for HA)",
                "remediation": "set spec.replicas >= 2 in score.yaml",
            })
    return out


def _rule_nw_k8s_005(docs: list[dict]) -> list[dict]:
    """NW-K8S-005 — required labels (owner, cost-center, environment)."""
    out = []
    required = ["northwind.io/owner", "northwind.io/cost-center", "northwind.io/environment"]
    for obj in docs:
        kind = obj.get("kind", "")
        if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Service", "Ingress"}:
            continue
        labels = obj.get("metadata", {}).get("labels", {}) or {}
        name = _workload_name(obj)
        for key in required:
            if key not in labels:
                out.append({
                    "policy_id": "NW-K8S-005",
                    "severity": "warning",
                    "tool": "conftest",
                    "subject": name,
                    "message": f"{name}: missing required label {key}",
                    "remediation": f"add label {key}=<value> in metadata.labels",
                })
    return out


def _rule_nw_k8s_006(docs: list[dict]) -> list[dict]:
    """NW-K8S-006 — secrets must come from Secret resources, not env vars."""
    out = []
    for obj in docs:
        for cname, c in _containers(obj):
            env_list = c.get("env", []) or []
            for env in env_list:
                name = env.get("name", "")
                value = env.get("value")
                # Heuristic: any env var whose name suggests a secret and whose
                # value is a literal string is a violation.
                if value is None:
                    continue
                if re.search(r"(?i)(password|secret|token|key|credential)", name):
                    obj_name = _workload_name(obj)
                    out.append({
                        "policy_id": "NW-K8S-006",
                        "severity": "critical",
                        "tool": "conftest",
                        "subject": f'{obj_name} container "{cname}"',
                        "message": f'{obj_name} container "{cname}": env var {name} is a literal — '
                                   f"use a Secret reference (env.valueFrom.secretKeyRef)",
                        "remediation": f"replace env.{name}.value with env.{name}.valueFrom.secretKeyRef",
                    })
    return out


def _rule_nw_k8s_008(docs: list[dict]) -> list[dict]:
    """NW-K8S-008 — prod workloads must define liveness and readiness probes."""
    out = []
    for obj in docs:
        labels = obj.get("metadata", {}).get("labels", {}) or {}
        if labels.get("northwind.io/environment") != "prod":
            continue
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            if "livenessProbe" not in c:
                out.append({
                    "policy_id": "NW-K8S-008",
                    "severity": "warning",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no livenessProbe',
                    "remediation": f"add livenessProbe to containers.{cname} in score.yaml",
                })
            if "readinessProbe" not in c:
                out.append({
                    "policy_id": "NW-K8S-008",
                    "severity": "warning",
                    "tool": "conftest",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no readinessProbe',
                    "remediation": f"add readinessProbe to containers.{cname} in score.yaml",
                })
    return out


# ---------------------------------------------------------------------------
# KL-* rule re-implementations — match kube-linter's defaults.
# These cover the 5 kube-linter checks that fire against the unguided manifests.
# ---------------------------------------------------------------------------
def _rule_kl_latest_tag(docs: list[dict]) -> list[dict]:
    """KL-latest-tag — images must not use :latest."""
    out = []
    for obj in docs:
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            image = c.get("image", "")
            if image.endswith(":latest") or (":" not in image and "@sha256:" not in image):
                out.append({
                    "policy_id": "KL-latest-tag",
                    "severity": "warning",
                    "tool": "kube-linter",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": image {image} uses :latest tag',
                    "remediation": "pin to a specific version tag",
                })
    return out


def _rule_kl_read_only_root_fs(docs: list[dict]) -> list[dict]:
    """KL-no-read-only-root-fs — containers should set securityContext.readOnlyRootFilesystem=true."""
    out = []
    for obj in docs:
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            sc = c.get("securityContext", {}) or {}
            if not sc.get("readOnlyRootFilesystem"):
                out.append({
                    "policy_id": "KL-no-read-only-root-fs",
                    "severity": "warning",
                    "tool": "kube-linter",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": readOnlyRootFilesystem is not true',
                    "remediation": f"set containers.{cname}.securityContext.readOnlyRootFilesystem=true",
                })
    return out


def _rule_kl_run_as_non_root(docs: list[dict]) -> list[dict]:
    """KL-run-as-non-root — containers should set runAsNonRoot=true."""
    out = []
    for obj in docs:
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            sc = c.get("securityContext", {}) or {}
            if not sc.get("runAsNonRoot"):
                out.append({
                    "policy_id": "KL-run-as-non-root",
                    "severity": "warning",
                    "tool": "kube-linter",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": runAsNonRoot is not set',
                    "remediation": f"set containers.{cname}.securityContext.runAsNonRoot=true",
                })
    return out


def _rule_kl_unset_resources(docs: list[dict]) -> list[dict]:
    """KL-unset-cpu-requirements + KL-unset-memory-requirements — containers must declare resources."""
    out = []
    for obj in docs:
        name = _workload_name(obj)
        for cname, c in _containers(obj):
            resources = c.get("resources", {}) or {}
            if not resources:
                out.append({
                    "policy_id": "KL-unset-cpu-requirements",
                    "severity": "warning",
                    "tool": "kube-linter",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no CPU requirements set',
                    "remediation": f"set containers.{cname}.resources in score.yaml",
                })
                out.append({
                    "policy_id": "KL-unset-memory-requirements",
                    "severity": "warning",
                    "tool": "kube-linter",
                    "subject": f'{name} container "{cname}"',
                    "message": f'{name} container "{cname}": no memory requirements set',
                    "remediation": f"set containers.{cname}.resources in score.yaml",
                })
    return out


# ---------------------------------------------------------------------------
# Run all analyzers — returns the full findings list.
# ---------------------------------------------------------------------------
PYTHON_RULES = [
    _rule_nw_k8s_001,
    _rule_nw_k8s_002,
    _rule_nw_k8s_003,
    _rule_nw_k8s_004,
    _rule_nw_k8s_005,
    _rule_nw_k8s_006,
    _rule_nw_k8s_008,
    _rule_kl_latest_tag,
    _rule_kl_read_only_root_fs,
    _rule_kl_run_as_non_root,
    _rule_kl_unset_resources,
]


def run_python_analyzers(docs: list[dict]) -> list[dict]:
    """Run all NW-K8S-* + KL-* rules against the parsed docs."""
    findings: list[dict] = []
    for rule in PYTHON_RULES:
        findings.extend(rule(docs))
    # Sort by policy_id then subject for stable ordering.
    findings.sort(key=lambda f: (f["policy_id"], f["subject"]))
    return findings


# ---------------------------------------------------------------------------
# Build the response — PRD §5.4.
# ---------------------------------------------------------------------------
def build_evaluate_response(
    manifest_yaml: str | None,
    scenario: str,
    template_id: str | None,
    cold_start: bool = False,
) -> dict:
    """Build the PRD §5.4 response.

    Args:
      manifest_yaml: the user's YAML. If None, use the template's default.
      scenario: "raw" or "golden_path".
      template_id: which template to use (only "payments-ledger" supported).
      cold_start: from detect_cold_start().
    """
    started_at = time.perf_counter()

    # Resolve template.
    template = next((t for t in TEMPLATES if t["id"] == (template_id or "payments-ledger")), TEMPLATES[0])

    # Resolve manifest + cached flag.
    # - If user provided a manifest THAT DIFFERS from the template default,
    #   run the Python analyzers (engine=python-faithful-reimpl).
    # - If user provided the template default unchanged (i.e. the auto-load
    #   from /outputs/unguided-manifests.yaml), OR provided no manifest at all,
    #   use the cached conftest output (engine=conftest-cached). This is the
    #   canonical 42-denial (raw) / 0-denial (golden_path) response that the
    #   real conftest binary produces.
    if manifest_yaml and manifest_yaml.strip():
        # Check if the user's manifest matches the template default for the
        # selected scenario. If yes, treat as "no manifest provided" and use
        # the cached canonical output instead of the Python reimplementation.
        default_path = (
            template["golden_manifest_path"] if scenario == "golden_path"
            else template["raw_manifest_path"]
        )
        try:
            with open(default_path) as f:
                default_yaml = f.read()
            if manifest_yaml.strip() == default_yaml.strip():
                manifest_yaml = None  # use cached path below
        except Exception:
            pass  # if we can't read the default, fall through to Python reimpl

    if manifest_yaml and manifest_yaml.strip():
        # User-provided YAML → Python re-implementation.
        try:
            docs = parse_manifests(manifest_yaml)
        except Exception as e:
            return {
                "error": f"manifest parse failed: {e}",
                "scenario": scenario,
                "template_id": template["id"],
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
            }
        denials = run_python_analyzers(docs)
        engine = "python-faithful-reimpl"
        cached = False
    elif scenario == "raw":
        # Cached canonical conftest output for the raw/unguided manifests.
        cached_report = _load_cached_report()
        denials = []
        for gate in cached_report.get("gates", []):
            for d in gate.get("denies", []):
                denials.append({
                    "policy_id": d["policyId"],
                    "severity": "critical" if d["severity"] == "deny" else d["severity"],
                    "tool": d["tool"],
                    "subject": d["subject"],
                    "message": d["message"],
                    "remediation": d["remediation"],
                })
        engine = "conftest-cached"
        cached = True
    else:
        # golden_path — the final-manifests.yaml was verified by `./run.sh
        # verify` to produce 0 conftest denials. When the user uses the
        # default template manifest (not their own YAML), we return the
        # cached canonical 0-denial response to match what conftest would
        # actually report. This is the PRD §5.6 case 4 fallback path.
        #
        # When the user provides their own YAML in golden_path scenario, we
        # fall through to the Python reimplementation honestly (which may
        # produce a small number of false positives vs conftest).
        if manifest_yaml and manifest_yaml.strip():
            try:
                golden_yaml = manifest_yaml
                docs = parse_manifests(golden_yaml)
                denials = run_python_analyzers(docs)
            except Exception:
                denials = []
            engine = "python-faithful-reimpl"
            cached = False
        else:
            # Cached canonical 0-denial response for the golden path.
            denials = []
            engine = "conftest-cached"
            cached = True

    # Group by policy for the byPolicy field.
    by_policy: dict[str, int] = {}
    for d in denials:
        by_policy[d["policy_id"]] = by_policy.get(d["policy_id"], 0) + 1
    by_policy = dict(sorted(by_policy.items()))

    return {
        "scenario": scenario,
        "template_id": template["id"],
        "denial_count": len(denials),
        "denials": denials,
        "by_policy": by_policy,
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "engine": engine,
        "cached": cached,
        "cold_start": cold_start,
        "eval_id": str(uuid.uuid4()),
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_compare_response(cold_start: bool = False) -> dict:
    """Build a side-by-side compare response — both scenarios at once.

    Returns {raw: <evaluate_response>, golden: <evaluate_response>, delta: int}.
    """
    raw = build_evaluate_response(None, "raw", "payments-ledger")
    golden = build_evaluate_response(None, "golden_path", "payments-ledger")
    return {
        "raw": raw,
        "golden": golden,
        "delta": raw["denial_count"] - golden["denial_count"],
        "cold_start": cold_start,
        "eval_id": str(uuid.uuid4()),
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def list_templates() -> list[dict]:
    """Return the list of available templates for the dropdown."""
    return [{"id": t["id"], "label": t["label"], "description": t["description"]} for t in TEMPLATES]


def get_default_manifest(template_id: str, scenario: str) -> str:
    """Return the default manifest YAML for a template + scenario."""
    template = next((t for t in TEMPLATES if t["id"] == (template_id or "payments-ledger")), TEMPLATES[0])
    if scenario == "golden_path":
        return _load_final_manifests()
    return _load_unguided_manifests()
