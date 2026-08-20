#!/usr/bin/env python3
"""
Rendering: developer intent -> Score spec -> Kubernetes + platform API objects.

Two paths are implemented here, and the difference between them is the entire
argument of this repository.

  vibe_manifests()    What you get when a capable model is asked for
                      "Kubernetes YAML for a payments service with Postgres"
                      and there is no platform in the way. It is not stupid
                      output. It is *plausible* output. That is the problem.

  golden_path()       The same request routed through the platform: a Score
                      workload spec, the Northwind provisioner set, real
                      score-k8s rendering, and the platform's admission-time
                      defaults.

The split matters because of where the remaining risk lands. Platform defaults
eliminate the entire class of structural mistakes deterministically -- security
context, probes, labels, service-account tokens. No model judgement is involved
and none is wanted. What the platform *cannot* decide for you is the judgement
calls: how big, which region, how long to keep backups, how much to spend.
Those are exactly the decisions an agent will get plausibly wrong, and exactly
what the policy bundle is for.

So: defaults for the deterministic half, policy for the judgement half, and the
agent operates in between. That is a platform, and it is why an agentic
platform needs *more* platform engineering, not less.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
PROVISIONERS = ROOT / "platform" / "northwind.provisioners.yaml"

APPROVED_REGISTRY = "ghcr.io/northwind-retail"
EU_REGIONS = {"eu-west-1", "eu-west-2", "eu-central-1", "eu-north-1", "eu-south-1"}


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------

@dataclass
class ServiceRequest:
    """Structured intent. This is what the agent produces from prose."""

    name: str
    description: str = ""
    owner: str = "group:default/payments"
    cost_center: str = "CC-4471"
    monthly_budget_usd: float = 400.0
    classification: str = "internal"       # public | internal | confidential | pci
    residency: str = ""                    # eu | us | apac
    environments: list[str] = field(default_factory=lambda: ["staging", "prod"])
    image_tag: str = "1.0.0"
    cpu: str = "500m"
    memory: str = "512Mi"
    cpu_request: str = "100m"
    memory_request: str = "256Mi"
    replicas: int = 3
    port: int = 8080
    database: bool = True
    db_instance_class: str = "db.t4g.medium"
    db_storage_gb: int = 100
    db_region: str = "eu-west-1"
    db_backup_retention_days: int = 7
    db_encrypted: bool = True
    db_multi_az: bool = False

    @property
    def is_pci(self) -> bool:
        return self.classification == "pci"

    @property
    def image(self) -> str:
        return f"{APPROVED_REGISTRY}/{self.name}:{self.image_tag}"

    @property
    def owner_short(self) -> str:
        return self.owner.replace("group:default/", "")

    def replicas_for(self, environment: str) -> int:
        return self.replicas if environment == "prod" else max(1, min(2, self.replicas))

    def db_class_for(self, environment: str) -> str:
        return self.db_instance_class if environment == "prod" else "db.t4g.medium"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Path A: no platform
# ---------------------------------------------------------------------------

def vibe_manifests(req: ServiceRequest) -> list[dict[str, Any]]:
    """Manifests generated straight from the prompt, with no platform in between.

    Everything wrong here is wrong in a *believable* way. There is no strawman:
    this is a faithful reproduction of the shape of output you get when a model
    is asked for Kubernetes YAML without a golden path to render into. It runs.
    It would pass review from anyone not looking closely. It is also a PCI
    finding, an availability incident and an unbounded bill.
    """
    labels = {"app": req.name}
    return [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": req.name, "labels": dict(labels)},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": dict(labels)},
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "containers": [
                            {
                                "name": req.name,
                                "image": f"{req.name}:latest",
                                "ports": [{"containerPort": req.port}],
                                "env": [
                                    {"name": "DATABASE_URL",
                                     "value": f"postgresql://postgres:postgres@{req.name}-db:5432/{req.name}"},
                                    {"name": "DB_PASSWORD", "value": "postgres"},
                                ],
                            }
                        ]
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": req.name, "labels": dict(labels)},
            "spec": {
                "type": "LoadBalancer",
                "selector": dict(labels),
                "ports": [{"port": 80, "targetPort": req.port}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": f"{req.name}-db", "labels": {"app": f"{req.name}-db"}},
            "spec": {
                "replicas": 1,
                "serviceName": f"{req.name}-db",
                "selector": {"matchLabels": {"app": f"{req.name}-db"}},
                "template": {
                    "metadata": {"labels": {"app": f"{req.name}-db"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "postgres",
                                "image": "postgres:latest",
                                "env": [
                                    {"name": "POSTGRES_PASSWORD", "value": "postgres"},
                                    {"name": "POSTGRES_DB", "value": req.name},
                                ],
                                "ports": [{"containerPort": 5432}],
                            }
                        ]
                    },
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Path B: the golden path
# ---------------------------------------------------------------------------

def score_spec(req: ServiceRequest, environment: str) -> dict[str, Any]:
    """The developer-facing contract. Small, schema'd, and the only file a
    product engineer should ever have to open."""
    annotations = {
        "northwind.io/owner": req.owner,
        "northwind.io/cost-center": req.cost_center,
        "northwind.io/data-classification": req.classification,
        "northwind.io/environment": environment,
    }
    if req.residency:
        annotations["northwind.io/data-residency"] = req.residency

    spec: dict[str, Any] = {
        "apiVersion": "score.dev/v1b1",
        "metadata": {"name": req.name, "annotations": annotations},
        "containers": {
            "app": {
                "image": req.image,
                "variables": {
                    "PORT": str(req.port),
                    "ENVIRONMENT": environment,
                },
                "resources": {
                    "limits": {"cpu": req.cpu, "memory": req.memory},
                    "requests": {"cpu": req.cpu_request, "memory": req.memory_request},
                },
            }
        },
        "service": {"ports": {"http": {"port": req.port, "targetPort": req.port}}},
        "resources": {},
    }

    if req.database:
        spec["containers"]["app"]["variables"].update({
            "DB_HOST": "${resources.db.host}",
            "DB_PORT": "${resources.db.port}",
            "DB_NAME": "${resources.db.name}",
            "DB_USER": "${resources.db.username}",
            "DB_PASSWORD": "${resources.db.password}",
        })
        spec["resources"]["db"] = {
            "type": "postgres",
            "params": {
                "instanceClass": req.db_class_for(environment),
                "storageGb": req.db_storage_gb,
                "region": req.db_region,
                "encrypted": req.db_encrypted,
                "backupRetentionDays": req.db_backup_retention_days,
                "multiAz": req.db_multi_az and environment == "prod",
                "classification": req.classification,
                "residency": req.residency or "eu",
                "costCenter": req.cost_center,
                "owner": req.owner,
                "environment": environment,
            },
        }

    spec["resources"]["route"] = {
        "type": "dns",
        "params": {"environment": environment},
    }
    return spec


def _score_k8s(workdir: Path, score_file: Path, namespace: str) -> list[dict[str, Any]]:
    """Invoke the real score-k8s binary. No emulation."""
    binary = BIN / "score-k8s"
    if not binary.exists():
        binary_path = shutil.which("score-k8s")
        if not binary_path:
            raise RuntimeError(
                "score-k8s not found. Run ./run.sh setup to fetch the pinned "
                "upstream binaries (Apache-2.0, from GitHub releases)."
            )
        binary = Path(binary_path)

    subprocess.run(
        [str(binary), "init", "--no-sample", "--provisioners", str(PROVISIONERS)],
        cwd=workdir, check=True, capture_output=True, text=True,
    )
    out = workdir / "manifests.yaml"
    subprocess.run(
        [str(binary), "generate", str(score_file), "-o", str(out),
         "--namespace", namespace],
        cwd=workdir, check=True, capture_output=True, text=True,
    )
    return [d for d in yaml.safe_load_all(out.read_text()) if d]


def _stabilise_identifiers(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace score-k8s's per-provision UUIDs with content-derived ids.

    score-k8s mints a fresh GUID every time it provisions a resource and
    persists it in `.score-k8s/state.yaml` so that subsequent renders are
    stable. This demo renders into a throwaway directory, so without this step
    every run produces a different GUID and the committed artefacts churn --
    which would quietly destroy the one claim the repository actually makes,
    that the render is reproducible.

    The replacement is derived from the resource's own uid, so it is stable
    across machines and still unique per resource. In a live estate you keep
    the state file instead and delete this function.
    """
    mapping: dict[str, str] = {}

    # score-k8s also derives an instance suffix from the state file it just
    # created, so that too varies per render. Normalise it the same way.
    for doc in docs:
        inst = doc.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/instance", "")
        m = re.fullmatch(r"(?P<stem>.+)-(?P<suffix>[0-9a-f]{10})", inst)
        if m and m.group("suffix") not in mapping:
            mapping[m.group("suffix")] = hashlib.sha256(
                m.group("stem").encode()).hexdigest()[:10]

    for doc in docs:
        anns = doc.get("metadata", {}).get("annotations", {}) or {}
        guid = anns.get("k8s.score.dev/resource-guid")
        uid = anns.get("k8s.score.dev/resource-uid")
        if guid and uid and guid not in mapping:
            digest = hashlib.sha256(uid.encode()).hexdigest()
            mapping[guid] = "-".join(
                (digest[:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32]))
    if not mapping:
        return docs

    def swap(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: swap(v) for k, v in node.items()}
        if isinstance(node, list):
            return [swap(v) for v in node]
        if isinstance(node, str):
            for old, new in mapping.items():
                if old in node:
                    node = node.replace(old, new)
        return node

    return [swap(d) for d in docs]


def apply_platform_defaults(
    docs: list[dict[str, Any]], req: ServiceRequest, environment: str
) -> list[dict[str, Any]]:
    """The platform team's admission layer.

    Everything in this function is a decision that has exactly one correct
    answer at Northwind, which is precisely why no model is asked for its
    opinion about any of it. In a live cluster this is a Kyverno mutating
    policy or a Crossplane composition function; the logic is identical, only
    the enforcement point moves.
    """
    docs = _stabilise_identifiers(copy.deepcopy(docs))
    out: list[dict[str, Any]] = []

    platform_labels = {
        "app.kubernetes.io/name": req.name,
        "app.kubernetes.io/part-of": "payments-platform",
        "app.kubernetes.io/managed-by": "northwind-platform",
        "backstage.io/component": req.name,
        "northwind.io/owner": req.owner_short,
        "northwind.io/cost-center": req.cost_center,
        "northwind.io/data-classification": req.classification,
        "northwind.io/environment": environment,
    }
    if req.residency:
        platform_labels["northwind.io/data-residency"] = req.residency

    hardened_ctx = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    container_ctx = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "privileged": False,
        "capabilities": {"drop": ["ALL"]},
    }

    for doc in docs:
        kind = doc.get("kind")
        meta = doc.setdefault("metadata", {})
        meta.setdefault("labels", {}).update(platform_labels)

        if kind in ("Deployment", "StatefulSet"):
            meta.setdefault("annotations", {}).update({
                "northwind.io/network-policy": f"{req.name}-default-deny",
                "northwind.io/golden-path": "golden-path-service-postgres",
                "argocd.argoproj.io/sync-wave": "1",
            })
            spec = doc.setdefault("spec", {})
            spec["replicas"] = req.replicas_for(environment)
            tmpl = spec.setdefault("template", {})
            tmpl.setdefault("metadata", {}).setdefault("labels", {}).update(platform_labels)
            tmpl.setdefault("metadata", {}).setdefault("annotations", {}).update({
                "northwind.io/audit-sink": f"loki://platform/{req.owner_short}",
            })
            pod = tmpl.setdefault("spec", {})
            pod["automountServiceAccountToken"] = False
            pod["serviceAccountName"] = req.name
            pod.setdefault("securityContext", {}).update(hardened_ctx)
            pod.setdefault("volumes", [])
            if not any(v.get("name") == "tmp" for v in pod["volumes"]):
                pod["volumes"].append({"name": "tmp", "emptyDir": {}})

            # Spread replicas across zones. kube-linter flags any workload with
            # more than one replica and no anti-affinity, and it is right to:
            # three replicas on one node is one replica wearing a hat.
            if req.replicas_for(environment) > 1:
                pod.setdefault("topologySpreadConstraints", [{
                    "maxSkew": 1,
                    "topologyKey": "topology.kubernetes.io/zone",
                    "whenUnsatisfiable": "ScheduleAnyway",
                    "labelSelector": {"matchLabels": {"app.kubernetes.io/name": req.name}},
                }])
                pod.setdefault("affinity", {}).setdefault("podAntiAffinity", {})[
                    "preferredDuringSchedulingIgnoredDuringExecution"] = [{
                        "weight": 100,
                        "podAffinityTerm": {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": {"matchLabels": {
                                "app.kubernetes.io/name": req.name}},
                        },
                    }]

            for c in pod.get("containers", []) + pod.get("initContainers", []):
                c.setdefault("securityContext", {}).update(container_ctx)
                c.setdefault("volumeMounts", [])
                if not any(m.get("name") == "tmp" for m in c["volumeMounts"]):
                    c["volumeMounts"].append({"name": "tmp", "mountPath": "/tmp"})
                # score-k8s puts the port on the Service; a probe that targets a
                # port the container never declares is a probe that silently
                # never runs.
                c.setdefault("ports", [])
                if not any(p.get("containerPort") == req.port for p in c["ports"]):
                    c["ports"].append({"name": "http", "containerPort": req.port,
                                       "protocol": "TCP"})
                probe = {
                    "httpGet": {"path": "/healthz", "port": req.port},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 10,
                    "timeoutSeconds": 2,
                }
                c.setdefault("readinessProbe", copy.deepcopy(probe))
                c.setdefault("livenessProbe", {
                    **copy.deepcopy(probe),
                    "initialDelaySeconds": 15,
                    "failureThreshold": 3,
                })
                c.setdefault("resources", {})
                c["resources"].setdefault("limits", {"cpu": req.cpu, "memory": req.memory})
                c["resources"].setdefault(
                    "requests", {"cpu": req.cpu_request, "memory": req.memory_request}
                )
        out.append(doc)

    # The ServiceAccount is emitted first so that any tool linting this bundle
    # as an ordered stream resolves the reference the Deployment makes to it.
    extras = [_service_account(req, platform_labels),
              _network_policy(req, environment, platform_labels)]
    if req.replicas_for(environment) >= 2:
        extras.append(_pdb(req, platform_labels))

    # Match the namespace score-k8s stamped on everything else, so a reference
    # from a Deployment to its ServiceAccount actually resolves.
    namespace = next(
        (d["metadata"].get("namespace") for d in out
         if d.get("metadata", {}).get("namespace")), None)
    if namespace:
        for d in extras:
            d["metadata"]["namespace"] = namespace
    return extras + out


def _service_account(req: ServiceRequest, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": req.name, "labels": dict(labels)},
        "automountServiceAccountToken": False,
    }


def _network_policy(req: ServiceRequest, environment: str, labels: dict[str, str]) -> dict[str, Any]:
    """Default-deny both directions, then open exactly what the workload needs."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"{req.name}-default-deny", "labels": dict(labels)},
        "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": req.name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [
                        {"namespaceSelector": {"matchLabels": {"northwind.io/tier": "edge"}}},
                        {"podSelector": {"matchLabels": {"app.kubernetes.io/part-of": "payments-platform"}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": req.port}],
                }
            ],
            "egress": [
                {
                    "to": [{"namespaceSelector": {"matchLabels": {"northwind.io/tier": "data"}}}],
                    "ports": [{"protocol": "TCP", "port": 5432}],
                },
                {
                    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                },
            ],
        },
    }


def _pdb(req: ServiceRequest, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": req.name, "labels": dict(labels)},
        "spec": {
            "minAvailable": 1,
            # Without this, a PDB can wedge a node drain forever on pods that
            # are already unhealthy — the classic 3am surprise.
            "unhealthyPodEvictionPolicy": "AlwaysAllow",
            "selector": {"matchLabels": {"app.kubernetes.io/name": req.name}},
        },
    }


def golden_path(
    req: ServiceRequest, environment: str, workdir: Path | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """score.yaml -> real score-k8s render -> platform defaults.

    Returns (score_spec, manifests).
    """
    tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nw-render-"))
    tmp.mkdir(parents=True, exist_ok=True)
    spec = score_spec(req, environment)
    score_file = tmp / "score.yaml"
    score_file.write_text(yaml.safe_dump(spec, sort_keys=False))
    namespace = f"{req.owner_short}-{environment}"
    docs = _score_k8s(tmp, score_file, namespace)
    return spec, apply_platform_defaults(docs, req, environment)


# ---------------------------------------------------------------------------
# Delivery objects
# ---------------------------------------------------------------------------

def argocd_application(req: ServiceRequest, environment: str) -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": f"{req.name}-{environment}",
            "namespace": "argocd",
            "labels": {
                "backstage.io/component": req.name,
                "northwind.io/environment": environment,
                "northwind.io/cost-center": req.cost_center,
            },
            "finalizers": ["resources-finalizer.argocd.argoproj.io"],
        },
        "spec": {
            "project": req.owner_short,
            "source": {
                "repoURL": f"https://github.com/northwind-retail/{req.name}.git",
                "targetRevision": "main",
                "path": f"deploy/{environment}",
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": f"{req.owner_short}-{environment}",
            },
            "syncPolicy": {
                # Production is deliberately not self-healing: Kargo promotes
                # into it, and an agent-authored change must survive a human
                # review before Argo is allowed to act on it.
                "automated": None if environment == "prod" else {
                    "prune": True, "selfHeal": True,
                },
                "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
            },
        },
    }


def kargo_pipeline(req: ServiceRequest) -> list[dict[str, Any]]:
    """Warehouse -> Stage(staging) -> Stage(prod).

    Kargo models "what version is where and how does it move", which is the
    question GitOps alone never answered. It matters more with agents in the
    loop, not less: the promotion path is the thing you audit.
    """
    project = req.owner_short
    return [
        {
            "apiVersion": "kargo.akuity.io/v1alpha1",
            "kind": "Warehouse",
            "metadata": {"name": req.name, "namespace": project},
            "spec": {
                "subscriptions": [
                    {"image": {
                        "repoURL": f"{APPROVED_REGISTRY}/{req.name}",
                        "imageSelectionStrategy": "SemVer",
                        "discoveryLimit": 5,
                    }}
                ]
            },
        },
        {
            "apiVersion": "kargo.akuity.io/v1alpha1",
            "kind": "Stage",
            "metadata": {"name": f"{req.name}-staging", "namespace": project},
            "spec": {
                "requestedFreight": [
                    {"origin": {"kind": "Warehouse", "name": req.name},
                     "sources": {"direct": True}}
                ],
                "promotionTemplate": {"spec": {"steps": [
                    {"uses": "git-clone", "config": {
                        "repoURL": f"https://github.com/northwind-retail/{req.name}.git",
                        "checkout": [{"branch": "main", "path": "./src"}]}},
                    {"uses": "kustomize-set-image", "config": {
                        "path": "./src/deploy/staging",
                        "images": [{"image": f"{APPROVED_REGISTRY}/{req.name}"}]}},
                    {"uses": "git-commit", "config": {
                        "path": "./src", "message": "promote to staging"}},
                    {"uses": "git-push", "config": {"path": "./src"}},
                    {"uses": "argocd-update", "config": {"apps": [
                        {"name": f"{req.name}-staging", "sources": [
                            {"repoURL": f"https://github.com/northwind-retail/{req.name}.git",
                             "desiredCommitFromStep": "commit"}]}]}},
                ]}},
                "verification": {"analysisTemplates": [{"name": "northwind-slo-gate"}]},
            },
        },
        {
            "apiVersion": "kargo.akuity.io/v1alpha1",
            "kind": "Stage",
            "metadata": {
                "name": f"{req.name}-prod",
                "namespace": project,
                "annotations": {
                    # The load-bearing line. Freight only reaches production
                    # through a stage that requires a human, and the agent's
                    # identity is not granted delivery:approve.
                    "kargo.akuity.io/authorized-stage": f"{project}:{req.name}-staging",
                    "northwind.io/requires-human-approval": "true",
                },
            },
            "spec": {
                "requestedFreight": [
                    {"origin": {"kind": "Warehouse", "name": req.name},
                     "sources": {"stages": [f"{req.name}-staging"]}}
                ],
                "promotionTemplate": {"spec": {"steps": [
                    {"uses": "git-clone", "config": {
                        "repoURL": f"https://github.com/northwind-retail/{req.name}.git",
                        "checkout": [{"branch": "main", "path": "./src"}]}},
                    {"uses": "kustomize-set-image", "config": {
                        "path": "./src/deploy/prod",
                        "images": [{"image": f"{APPROVED_REGISTRY}/{req.name}"}]}},
                    {"uses": "git-open-pr", "config": {
                        "repoURL": f"https://github.com/northwind-retail/{req.name}.git",
                        "sourceBranch": f"kargo/promote-{req.name}-prod",
                        "targetBranch": "main"}},
                ]}},
                "verification": {"analysisTemplates": [{"name": "northwind-slo-gate"}]},
            },
        },
    ]


def dump(docs: list[dict[str, Any]]) -> str:
    return "".join(
        "---\n" + yaml.safe_dump(d, sort_keys=False, default_flow_style=False)
        for d in docs
    )
