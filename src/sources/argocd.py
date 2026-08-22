#!/usr/bin/env python3
"""
Observed state from a live Argo CD, instead of the fixture.

WHY THIS FILE EXISTS
--------------------
`platform/observed-state.yaml` is a fixture, and the README says so. The gap
that criticism leaves open is a fair one: it is easy to declare "in a live
estate you would feed this from Argo CD" and never show what that costs. This
is what it costs -- one module, stdlib only, no new dependency.

It is not wired in by default and it is not exercised by `./run.sh verify`,
because a check that needs a running Argo CD is not a check that runs on
someone else's laptop. Set NORTHWIND_DRIFT_SOURCE=argocd and it takes over.

    export NORTHWIND_DRIFT_SOURCE=argocd
    export ARGOCD_SERVER=https://argocd.example.internal
    export ARGOCD_TOKEN=...            # a project token with applications:get
    ./run.sh drift payments-ledger

WHAT ARGO CD CAN AND CANNOT TELL YOU
------------------------------------
It answers the *what* precisely: `/managed-resources` returns `targetState`
(what git says) and `liveState` (what the cluster has) for every managed
object, which is exactly the desired/observed pair the fixture encodes.

It cannot answer the *who* directly -- there is no "k.mensah ran kubectl scale"
field. The closest real signal is `metadata.managedFields`: the API server
records which field manager last owned each field, so a `replicas` owned by
`kubectl-scale` rather than by the Argo CD controller is a hand-edit, and its
`time` is when it happened. That is a weaker attribution than the fixture's,
and this module reports it at that strength rather than inventing a name.
Severity is derived from the policy the field maps to, not guessed.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Which Score/platform field each Kubernetes path corresponds to, and the
# policy that governs it. The policy id is what makes a drift finding
# actionable rather than merely true.
FIELD_POLICY = {
    "replicas": "NW-FIN-001",
    "image": "NW-K8S-001",
    "cpuLimit": "NW-K8S-002",
    "memoryLimit": "NW-K8S-002",
    "backupRetentionDays": "NW-PCI-003",
    "networkPolicy": "NW-PCI-001",
    "automountServiceAccountToken": "NW-PCI-006",
}

CRITICAL_FIELDS = {"backupRetentionDays", "networkPolicy"}
HIGH_FIELDS = {"replicas", "automountServiceAccountToken", "image"}

# Field managers that mean "a controller did this", i.e. not a hand-edit.
CONTROLLER_MANAGERS = {
    "argocd-controller", "argocd-application-controller",
    "kube-controller-manager", "crossplane", "score-k8s",
}


class ArgoCDError(RuntimeError):
    pass


def _client_config() -> tuple[str, str, bool]:
    server = os.environ.get("ARGOCD_SERVER", "").rstrip("/")
    token = os.environ.get("ARGOCD_TOKEN", "")
    insecure = os.environ.get("ARGOCD_INSECURE", "") not in ("", "0", "false")
    if not server:
        raise ArgoCDError("ARGOCD_SERVER is not set")
    if not token:
        raise ArgoCDError("ARGOCD_TOKEN is not set (needs applications:get)")
    return server, token, insecure


def _get(path: str, params: dict[str, str] | None = None, timeout: int = 20) -> Any:
    server, token, insecure = _client_config()
    url = f"{server}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ArgoCDError(f"{exc.code} from {path}: {exc.read()[:200]!r}") from exc
    except urllib.error.URLError as exc:
        raise ArgoCDError(f"cannot reach {server}: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _container(obj: dict[str, Any]) -> dict[str, Any]:
    spec = (obj.get("spec") or {}).get("template", {}).get("spec", {})
    containers = spec.get("containers") or [{}]
    return containers[0]


def _extract(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the platform-relevant fields out of one Kubernetes object."""
    if not obj:
        return {}
    kind = obj.get("kind", "")
    out: dict[str, Any] = {}
    if kind in ("Deployment", "StatefulSet"):
        spec = obj.get("spec") or {}
        pod = (spec.get("template") or {}).get("spec") or {}
        limits = (_container(obj).get("resources") or {}).get("limits") or {}
        out["replicas"] = spec.get("replicas")
        out["image"] = _container(obj).get("image")
        out["cpuLimit"] = limits.get("cpu")
        out["memoryLimit"] = limits.get("memory")
        out["automountServiceAccountToken"] = pod.get("automountServiceAccountToken", True)
    elif kind == "NetworkPolicy":
        out["networkPolicy"] = (obj.get("metadata") or {}).get("name")
    elif kind == "SQLInstance":
        params = ((obj.get("spec") or {}).get("parameters") or {})
        out["backupRetentionDays"] = params.get("backupRetentionDays")
    return {k: v for k, v in out.items() if v is not None or k == "networkPolicy"}


def _managers(obj: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """field -> {manager, time} from metadata.managedFields.

    This is the only attribution a Kubernetes API server actually offers. It
    names the *manager*, not the person: `kubectl-scale` tells you a human ran
    a command, not which human. Reporting it at that strength is the point.
    """
    if not obj:
        return {}
    out: dict[str, dict[str, str]] = {}
    for entry in (obj.get("metadata") or {}).get("managedFields") or []:
        manager = entry.get("manager", "unknown")
        when = entry.get("time", "unknown")
        fields = json.dumps(entry.get("fieldsV1") or {})
        for path, name in (("f:replicas", "replicas"),
                           ("f:image", "image"),
                           ("f:automountServiceAccountToken", "automountServiceAccountToken"),
                           ("f:backupRetentionDays", "backupRetentionDays")):
            if path in fields:
                out[name] = {"manager": manager, "time": when}
    return out


def _severity(field: str) -> str:
    if field in CRITICAL_FIELDS:
        return "critical"
    return "high" if field in HIGH_FIELDS else "medium"


def _attribution(field: str, managers: dict[str, dict[str, str]]) -> dict[str, Any]:
    meta = managers.get(field, {})
    manager = meta.get("manager", "unknown")
    out_of_band = manager not in CONTROLLER_MANAGERS and manager != "unknown"
    return {
        "severity": _severity(field),
        # Deliberately the field manager, not a person. Argo CD does not know
        # who typed the command and this module will not pretend otherwise.
        "changedBy": f"field-manager:{manager}",
        "changedAt": meta.get("time", "unknown"),
        "via": (f"{manager} owns this field (out of band; Argo CD did not set it)"
                if out_of_band else f"{manager} owns this field"),
        "impact": "",
        "policyId": FIELD_POLICY.get(field, ""),
    }


def fetch_observed_state(app_name: str, service: str | None = None,
                         environment: str = "prod") -> dict[str, Any]:
    """Return one `workloads` mapping in `observed-state.yaml` shape.

    `app_name` is the Argo CD Application. `service`/`environment` name the key
    the drift agent will look the result up under, and default to the
    Application name split on the last `-`.
    """
    if service is None:
        service, _, env = app_name.rpartition("-")
        service = service or app_name
        environment = env or environment

    payload = _get(f"/api/v1/applications/{app_name}/managed-resources")
    app = _get(f"/api/v1/applications/{app_name}")

    desired: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    managers: dict[str, dict[str, str]] = {}

    for item in payload.get("items") or []:
        target = json.loads(item["targetState"]) if item.get("targetState") else None
        live = json.loads(item["liveState"]) if item.get("liveState") else None
        desired.update(_extract(target))
        observed.update(_extract(live))
        managers.update(_managers(live))

    # A NetworkPolicy that git declares and the cluster does not have shows up
    # as a target with no live counterpart, so it never lands in `observed`.
    # Record the absence explicitly -- a missing key and a deleted policy are
    # very different findings.
    if "networkPolicy" in desired and "networkPolicy" not in observed:
        observed["networkPolicy"] = None

    attribution = {f: _attribution(f, managers)
                   for f in desired if observed.get(f) != desired.get(f)}

    status = (app.get("status") or {})
    return {
        f"{service}/{environment}": {
            "lastReconciled": ((status.get("operationState") or {})
                               .get("finishedAt", "unknown")),
            "desired": desired,
            "observed": observed,
            "attribution": attribution,
        }
    }


def load(service: str = "", environment: str = "prod") -> dict[str, Any]:
    """Entry point `src/driftd.py` calls when NORTHWIND_DRIFT_SOURCE=argocd."""
    app = os.environ.get("ARGOCD_APP") or (
        f"{service}-{environment}" if service else "")
    if not app:
        raise ArgoCDError("set ARGOCD_APP, or pass a service name")
    return {
        "lastSweep": "live",
        "source": f"argocd:{app}",
        "workloads": fetch_observed_state(app, service or None, environment),
    }


if __name__ == "__main__":
    import sys
    import yaml
    svc = sys.argv[1] if len(sys.argv) > 1 else "payments-ledger"
    env = sys.argv[2] if len(sys.argv) > 2 else "prod"
    try:
        print(yaml.safe_dump(load(svc, env), sort_keys=False))
    except ArgoCDError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
