#!/usr/bin/env python3
"""
Northwind Platform MCP server.

A real Model Context Protocol server (2025-06-18), stdlib only, no
dependencies beyond PyYAML. Speaks stdio and streamable HTTP. Point Claude
Code, Cursor, Copilot, kagent or anything else that speaks MCP at it:

    claude mcp add northwind -- python3 src/platform_mcp.py
    # or, over HTTP:
    python3 src/platform_mcp.py --http 8099
    claude mcp add --transport http northwind http://127.0.0.1:8099/mcp

WHAT MAKES THIS DIFFERENT FROM A TOOL WRAPPER
---------------------------------------------
Every tool declares a `permission`, and `tools/list` is filtered by the calling
identity before the model ever sees it. An agent that lacks `delivery:approve`
is not told that `platform.approve_promotion` exists. It cannot call it, cannot
hallucinate it, and cannot be talked into it, because the capability is absent
from its world rather than forbidden within it.

This is the difference between a guardrail and a system prompt. A system prompt
that says "never approve production" is a request. An empty tool list is a
fact.

Try it:

    python3 src/platform_mcp.py --list-tools --identity platform-agent   # 12 of 14
    python3 src/platform_mcp.py --list-tools --identity cost-reviewer    #  3 of 14

The pattern is lifted from OpenChoreo (Apache-2.0, CNCF Sandbox), whose control
plane gates every MCP tool through the same policy decision point that governs
human API access. As of this writing it is the only OSS internal developer
platform I found that does so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.parse
import zlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import costing  # noqa: E402
import gates  # noqa: E402
import renderer  # noqa: E402
from catalog import (  # noqa: E402
    Action,
    Catalog,
    Identity,
    load_templates,
    find_template,
    resolve_identity,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "northwind-platform"
SERVER_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    description: str
    permission: str
    schema: dict[str, Any]
    handler: Callable[["Session", dict[str, Any]], Any]
    destructive: bool = False

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": not self.destructive,
                "destructiveHint": self.destructive,
                # Non-standard but useful: surface the platform action so an
                # operator reading a transcript can see what was authorised.
                "northwind.io/permission": self.permission,
            },
        }


TOOLS: dict[str, Tool] = {}


def tool(name: str, permission: str, description: str,
         schema: dict[str, Any] | None = None, destructive: bool = False):
    def wrap(fn):
        TOOLS[name] = Tool(
            name=name, description=description, permission=permission,
            schema=schema or {"type": "object", "properties": {}},
            handler=fn, destructive=destructive,
        )
        return fn
    return wrap


def obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class Session:
    """Per-connection state: who is calling, and what they have built so far."""

    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.catalog = Catalog()
        self.workspace = Path(
            os.environ.get("NORTHWIND_WORKSPACE", ROOT / "workspace")
        )
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.audit: list[dict[str, Any]] = []

    def visible_tools(self) -> list[Tool]:
        allow = self.catalog.allowed_tools_for(self.identity.name)
        out = []
        for t in TOOLS.values():
            if not self.identity.can(t.permission):
                continue
            if allow is not None and t.name not in allow:
                continue
            out.append(t)
        return sorted(out, key=lambda t: t.name)

    def record(self, tool_name: str, ok: bool, detail: str = "") -> None:
        self.audit.append({
            "identity": self.identity.describe(),
            "tool": tool_name,
            "allowed": ok,
            "detail": detail,
        })


# ---------------------------------------------------------------------------
# Catalog tools
# ---------------------------------------------------------------------------

@tool("catalog.query", Action.VIEW_CATALOG,
      "Search the software catalog. Filter by kind (Component, System, API, "
      "Group, Resource, AiResource), owner, tag or free text.",
      obj({"kind": STR, "owner": STR, "tag": STR, "text": STR,
           "limit": {**INT, "default": 25}}))
def _catalog_query(s: Session, a: dict[str, Any]) -> Any:
    hits = s.catalog.query(
        kind=a.get("kind"), owner=a.get("owner"), tag=a.get("tag"),
        text=a.get("text"), limit=int(a.get("limit", 25)),
    )
    return {"count": len(hits), "entities": [e.summary() for e in hits]}


@tool("catalog.get_entity", Action.VIEW_CATALOG,
      "Fetch one catalog entity in full by ref (e.g. component:default/payments-gateway) "
      "or by bare name.",
      obj({"ref": STR}, ["ref"]))
def _catalog_get(s: Session, a: dict[str, Any]) -> Any:
    e = s.catalog.get(a["ref"])
    if not e:
        return {"found": False, "ref": a["ref"]}
    return {"found": True, "generation": e.generation, "source": e.source, "entity": e.raw}


@tool("catalog.refresh_entity", Action.REFRESH_ENTITY,
      "Re-queue an entity for processing and return its fresh state. Use this "
      "immediately after scaffolder.execute so you read the catalog's view of "
      "the new component instead of racing the processing loop.",
      obj({"ref": STR}, ["ref"]), destructive=False)
def _catalog_refresh(s: Session, a: dict[str, Any]) -> Any:
    e = s.catalog.refresh(a["ref"])
    if not e:
        return {"refreshed": False, "ref": a["ref"]}
    return {"refreshed": True, "generation": e.generation, "entity": e.summary()}


@tool("catalog.register", Action.REGISTER_ENTITY,
      "Register or update a catalog entity from a YAML document.",
      obj({"entityYaml": STR}, ["entityYaml"]), destructive=True)
def _catalog_register(s: Session, a: dict[str, Any]) -> Any:
    doc = yaml.safe_load(a["entityYaml"])
    e = s.catalog.register(doc)
    return {"registered": True, "ref": e.ref, "generation": e.generation}


# ---------------------------------------------------------------------------
# Scaffolder tools
# ---------------------------------------------------------------------------

@tool("scaffolder.list_templates", Action.LIST_TEMPLATES,
      "List the golden-path software templates this platform offers.")
def _tpl_list(s: Session, a: dict[str, Any]) -> Any:
    return {"templates": [
        {"name": t.name, "title": t.title, "description": t.description, "tags": t.tags}
        for t in load_templates()
    ]}


@tool("scaffolder.get_template_parameters", Action.LIST_TEMPLATES,
      "Return the JSON Schema for a template's parameters. Read this before "
      "calling scaffolder.execute so the values you supply are valid.",
      obj({"template": STR}, ["template"]))
def _tpl_params(s: Session, a: dict[str, Any]) -> Any:
    t = find_template(a["template"])
    if not t:
        return {"found": False, "template": a["template"]}
    return {"found": True, "template": t.name, "schema": t.parameter_schema()}


@tool("scaffolder.execute", Action.EXECUTE_TEMPLATE,
      "Run a golden-path template. Renders the Score workload spec, the "
      "Kubernetes manifests (via score-k8s and the Northwind provisioner set), "
      "the Argo CD Application and the Kargo promotion pipeline into the "
      "workspace. Returns the file list and the rendered Score spec.",
      obj({"template": STR, "values": {"type": "object"}}, ["template", "values"]),
      destructive=True)
def _tpl_execute(s: Session, a: dict[str, Any]) -> Any:
    values = a.get("values", {})
    req = _request_from_values(values)
    written: list[str] = []
    base = s.workspace / req.name
    for env in req.environments:
        spec, docs = renderer.golden_path(req, env, base / ".render" / env)
        env_dir = base / "deploy" / env
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "score.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (env_dir / "manifests.yaml").write_text(renderer.dump(docs))
        (env_dir / "argocd-application.yaml").write_text(
            yaml.safe_dump(renderer.argocd_application(req, env), sort_keys=False))
        written += [
            str((env_dir / n).relative_to(s.workspace))
            for n in ("score.yaml", "manifests.yaml", "argocd-application.yaml")
        ]
    kargo_dir = base / "deploy" / "kargo"
    kargo_dir.mkdir(parents=True, exist_ok=True)
    (kargo_dir / "pipeline.yaml").write_text(renderer.dump(renderer.kargo_pipeline(req)))
    written.append(str((kargo_dir / "pipeline.yaml").relative_to(s.workspace)))

    catalog_info = {
        "apiVersion": "backstage.io/v1alpha1",
        "kind": "Component",
        "metadata": {
            "name": req.name,
            "description": req.description,
            "tags": [req.classification, "golden-path"],
            "annotations": {
                "northwind.io/data-classification": req.classification,
                "northwind.io/cost-center": req.cost_center,
                "northwind.io/golden-path": "golden-path-service-postgres",
            },
        },
        "spec": {
            "type": "service", "lifecycle": "experimental",
            "owner": req.owner, "system": "payments-platform",
        },
    }
    (base / "catalog-info.yaml").write_text(yaml.safe_dump(catalog_info, sort_keys=False))
    written.append(str((base / "catalog-info.yaml").relative_to(s.workspace)))
    s.catalog.register(catalog_info, source="scaffolder")

    return {
        "executed": True,
        "template": a["template"],
        "component": f"component:default/{req.name}",
        "workspace": str(base),
        "files": sorted(written),
        "request": json.loads(req.to_json()),
    }


def _request_from_values(v: dict[str, Any]) -> renderer.ServiceRequest:
    cost_centers = {
        "group:default/payments": ("CC-4471", 400.0),
        "group:default/storefront": ("CC-2201", 900.0),
        "group:default/platform-team": ("CC-1000", 2500.0),
    }
    owner = v.get("owner", "group:default/payments")
    cc, budget = cost_centers.get(owner, ("UNASSIGNED", 250.0))
    return renderer.ServiceRequest(
        name=v["name"],
        description=v.get("description", ""),
        owner=owner,
        cost_center=cc,
        monthly_budget_usd=budget,
        classification=v.get("dataClassification", "internal"),
        residency=v.get("dataResidency", ""),
        environments=list(v.get("environments", ["staging", "prod"])),
        image_tag=v.get("imageTag", "1.0.0"),
        cpu=v.get("cpu", "500m"),
        memory=v.get("memory", "512Mi"),
        replicas=int(v.get("replicas", 3)),
        database=bool(v.get("database", True)),
        db_instance_class=v.get("dbInstanceClass", "db.t4g.medium"),
        db_storage_gb=int(v.get("dbStorageGb", 100)),
        db_region=v.get("dbRegion", "eu-west-1"),
        db_backup_retention_days=int(v.get("dbBackupRetentionDays", 7)),
        db_encrypted=bool(v.get("dbEncrypted", True)),
        db_multi_az=bool(v.get("dbMultiAz", False)),
    )


# ---------------------------------------------------------------------------
# Platform tools
# ---------------------------------------------------------------------------

@tool("platform.render_workload", Action.RENDER_WORKLOAD,
      "Render a Score workload spec to Kubernetes manifests using score-k8s "
      "and the Northwind provisioner set (postgres resolves to a Crossplane "
      "SQLInstance composite, not an in-cluster StatefulSet).",
      obj({"values": {"type": "object"}, "environment": STR}, ["values", "environment"]))
def _render(s: Session, a: dict[str, Any]) -> Any:
    req = _request_from_values(a["values"])
    env = a["environment"]
    spec, docs = renderer.golden_path(req, env)
    return {
        "environment": env,
        "scoreSpec": spec,
        "manifestKinds": [f"{d['kind']}/{d['metadata']['name']}" for d in docs],
        "manifests": renderer.dump(docs),
    }


@tool("platform.evaluate_policy", Action.EVALUATE_POLICY,
      "Evaluate the Northwind policy bundle against rendered manifests, a Score "
      "spec, or a cost estimate. Runs real Conftest/OPA and kube-linter. Each "
      "denial carries a policy id and an explicit remediation after '->'.",
      obj({"manifestsYaml": STR, "scoreYaml": STR, "costJson": STR,
           "includeScanners": {**BOOL, "default": True}}))
def _evaluate(s: Session, a: dict[str, Any]) -> Any:
    docs: list[dict[str, Any]] = []
    if a.get("manifestsYaml"):
        docs = [d for d in yaml.safe_load_all(a["manifestsYaml"]) if d]
    score = yaml.safe_load(a["scoreYaml"]) if a.get("scoreYaml") else None
    cost = json.loads(a["costJson"]) if a.get("costJson") else None
    report = gates.run_all(
        docs, score=score, cost=cost,
        include_scanners=bool(a.get("includeScanners", True)),
    )
    return report.as_dict()


@tool("platform.estimate_cost", Action.ESTIMATE_COST,
      "Estimate the monthly cost of a proposed service, per environment, in an "
      "Infracost-compatible shape that policy/cost/ can evaluate directly.",
      obj({"values": {"type": "object"}, "environment": STR}, ["values"]))
def _cost(s: Session, a: dict[str, Any]) -> Any:
    req = _request_from_values(a["values"])
    if a.get("environment"):
        return costing.estimate(req, a["environment"])
    return costing.estimate_all(req)


@tool("platform.plan_promotion", Action.PLAN_PROMOTION,
      "Plan a Kargo promotion: which Freight moves from which Stage to which, "
      "what verification gates it, and whether a human approval is required.",
      obj({"service": STR, "fromStage": STR, "toStage": STR}, ["service", "toStage"]))
def _plan_promotion(s: Session, a: dict[str, Any]) -> Any:
    to_stage = a["toStage"]
    needs_human = to_stage.endswith("prod") or to_stage == "prod"
    return {
        "service": a["service"],
        "from": a.get("fromStage", "staging"),
        "to": to_stage,
        "verification": ["northwind-slo-gate"],
        "requiresHumanApproval": needs_human,
        "approverGroups": ["group:default/payments"] if needs_human else [],
        "callerCanApprove": s.identity.can(Action.APPROVE_PROMOTION),
        "note": (
            "This identity can plan and propose this promotion but cannot "
            "approve it. Open a pull request and request review from the "
            "owning group."
            if needs_human and not s.identity.can(Action.APPROVE_PROMOTION)
            else "Approval available to this identity."
        ),
    }


@tool("platform.open_pull_request", Action.OPEN_PULL_REQUEST,
      "Open a pull request carrying the rendered change. The PR body embeds "
      "the policy verdict and the cost delta so a reviewer sees the evidence, "
      "not just the diff.",
      obj({"service": STR, "title": STR, "body": STR, "branch": STR,
           "files": {"type": "array", "items": STR}}, ["service", "title"]),
      destructive=True)
def _open_pr(s: Session, a: dict[str, Any]) -> Any:
    branch = a.get("branch") or f"platform/{a['service']}-golden-path"
    # zlib.crc32, not hash() -- Python randomises string hashing per process,
    # and a demo that claims reproducibility should not print a different
    # number on every run.
    number = 1000 + (zlib.crc32(a["service"].encode()) % 900)
    return {
        "opened": True,
        "number": number,
        "url": f"https://github.com/northwind-retail/{a['service']}/pull/{number}",
        "branch": branch,
        "title": a["title"],
        "author": s.identity.describe(),
        "requiredApprovals": 1,
        "reviewers": ["group:default/payments"],
        "mergeable": False,
        "note": "Awaiting human review. The agent cannot approve its own pull request.",
    }


@tool("platform.approve_promotion", Action.APPROVE_PROMOTION,
      "Approve a production promotion. Requires the delivery:approve action, "
      "which is granted to humans in the owning group and to no agent identity.",
      obj({"service": STR, "stage": STR}, ["service", "stage"]), destructive=True)
def _approve(s: Session, a: dict[str, Any]) -> Any:
    return {
        "approved": True,
        "service": a["service"],
        "stage": a["stage"],
        "approver": s.identity.describe(),
    }


@tool("platform.detect_drift", Action.READ_TELEMETRY,
      "Compare observed cluster state against the desired state in git and "
      "report divergence, including who or what caused it.",
      obj({"service": STR, "environment": STR}, ["service"]))
def _drift(s: Session, a: dict[str, Any]) -> Any:
    import driftd
    return driftd.detect(a["service"], a.get("environment", "prod"))


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, identity: Identity) -> None:
        self.session = Session(identity)

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return self._ok(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Northwind platform control plane. Start with "
                    "scaffolder.list_templates to see the golden paths, then "
                    "scaffolder.get_template_parameters before executing one. "
                    "Always run platform.evaluate_policy on what you render "
                    "and remediate every denial before opening a pull request: "
                    "each denial names the fix after '->'. You are authenticated "
                    f"as {self.session.identity.describe()}; tools you are not "
                    "authorised for are not listed."
                ),
            })

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return self._ok(mid, {})

        if method == "tools/list":
            return self._ok(mid, {
                "tools": [t.descriptor() for t in self.session.visible_tools()]
            })

        if method == "tools/call":
            return self._call(mid, params)

        if method == "resources/list":
            return self._ok(mid, {"resources": [
                {"uri": "northwind://policy/bundle",
                 "name": "Northwind policy bundle",
                 "description": "The Rego source every gate evaluates.",
                 "mimeType": "text/plain"},
                {"uri": "northwind://catalog/ai-resources",
                 "name": "Agent fleet",
                 "description": "AiResource entities: agents, skills, allowedTools.",
                 "mimeType": "application/yaml"},
                {"uri": "northwind://platform/provisioners",
                 "name": "Score provisioner set",
                 "description": "How abstract resources become real infrastructure.",
                 "mimeType": "application/yaml"},
            ]})

        if method == "resources/read":
            return self._read_resource(mid, params.get("uri", ""))

        if method == "prompts/list":
            return self._ok(mid, {"prompts": []})

        return self._err(mid, -32601, f"method not found: {method}")

    # -- helpers ------------------------------------------------------------

    def _call(self, mid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        t = TOOLS.get(name)

        if t is None:
            self.session.record(name, False, "unknown tool")
            return self._tool_error(mid, f"unknown tool: {name}")

        if not self.session.identity.can(t.permission):
            self.session.record(name, False, f"missing {t.permission}")
            return self._tool_error(mid, (
                f"permission denied: {self.session.identity.describe()} lacks "
                f"the '{t.permission}' action required by {name}. This is an "
                f"authorization decision made by the platform, not a "
                f"suggestion. Ask a principal that holds it."
            ))

        allow = self.session.catalog.allowed_tools_for(self.session.identity.name)
        if allow is not None and name not in allow:
            self.session.record(name, False, "not in AiResource allowedTools")
            return self._tool_error(mid, (
                f"tool {name} is not in the allowedTools list declared by the "
                f"AiResource entity for {self.session.identity.name}. Widening "
                f"it is a pull request against platform/catalog/ai-resources.yaml."
            ))

        try:
            result = t.handler(self.session, args)
            self.session.record(name, True)
        except Exception as exc:  # surfaced to the model so it can recover
            self.session.record(name, True, f"error: {exc}")
            return self._tool_error(
                mid, f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
            )

        return self._ok(mid, {
            "content": [{"type": "text",
                         "text": json.dumps(result, indent=2, default=str)}],
            "isError": False,
        })

    def _read_resource(self, mid: Any, uri: str) -> dict[str, Any]:
        mapping = {
            "northwind://policy/bundle": [
                ROOT / "policy" / "kubernetes" / "workload.rego",
                ROOT / "policy" / "kubernetes" / "pci.rego",
                ROOT / "policy" / "score" / "workload_spec.rego",
                ROOT / "policy" / "cost" / "budget.rego",
            ],
            "northwind://catalog/ai-resources": [
                ROOT / "platform" / "catalog" / "ai-resources.yaml"],
            "northwind://platform/provisioners": [
                ROOT / "platform" / "northwind.provisioners.yaml"],
        }
        paths = mapping.get(uri)
        if not paths:
            return self._err(mid, -32602, f"unknown resource: {uri}")
        text = "\n\n".join(
            f"# ---- {p.relative_to(ROOT)} ----\n{p.read_text()}"
            for p in paths if p.exists()
        )
        return self._ok(mid, {"contents": [
            {"uri": uri, "mimeType": "text/plain", "text": text}]})

    @staticmethod
    def _ok(mid: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    @staticmethod
    def _tool_error(mid: Any, message: str) -> dict[str, Any]:
        # Tool failures are results, not protocol errors: the model needs to
        # read them and change course.
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": message}], "isError": True}}


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

def serve_stdio(server: Server) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


ALLOWED_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _origin_allowed(origin: str) -> bool:
    """Only same-machine browser origins may drive this server.

    The MCP specification requires local HTTP transports to validate Origin,
    because without it any page a user happens to visit can reach a server
    bound to localhost -- and this one exposes scaffolder.execute, which writes
    files. A repository whose whole argument is about authorization should not
    ship the DNS-rebinding hole.

    Set NORTHWIND_ALLOWED_ORIGINS to a comma-separated list to widen it.
    """
    extra = {o.strip() for o in
             os.environ.get("NORTHWIND_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    if origin in extra:
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    return (parsed.hostname or "") in ALLOWED_ORIGIN_HOSTS


def serve_http(server: Server, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            origin = self.headers.get("Origin")
            if origin and _origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers",
                                 "content-type, mcp-session-id, mcp-protocol-version")
            self.end_headers()
            self.wfile.write(body)

        def _bad_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and not _origin_allowed(origin):
                self._send(403, b'{"error":"origin not allowed"}')
                return True
            return False

        def do_OPTIONS(self):
            if self._bad_origin():
                return
            self._send(204, b"")

        def do_GET(self):
            if self.path.rstrip("/") in ("/healthz", "/health"):
                self._send(200, b'{"status":"ok"}')
                return
            if self.path.rstrip("/") == "/mcp":
                self._send(405, b'{"error":"use POST for JSON-RPC"}')
                return
            self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if self._bad_origin():
                return
            if self.path.rstrip("/") != "/mcp":
                self._send(404, b'{"error":"not found"}')
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self._send(400, b'{"error":"invalid json"}')
                return
            batch = msg if isinstance(msg, list) else [msg]
            out = [r for r in (server.handle(m) for m in batch) if r is not None]
            if not out:
                self._send(202, b"")
                return
            payload = out if isinstance(msg, list) else out[0]
            self._send(200, json.dumps(payload).encode())

    host = os.environ.get("NORTHWIND_BIND", "127.0.0.1")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"northwind-platform MCP listening on http://{host}:{port}/mcp",
          file=sys.stderr)
    print(f"identity: {server.session.identity.describe()}", file=sys.stderr)
    httpd.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Northwind Platform MCP server")
    ap.add_argument("--http", type=int, metavar="PORT",
                    help="serve streamable HTTP on 127.0.0.1:PORT/mcp instead of stdio")
    ap.add_argument("--identity", default=os.environ.get("NORTHWIND_IDENTITY", "platform-agent"))
    ap.add_argument("--list-tools", action="store_true",
                    help="print the tool list this identity would receive, then exit")
    args = ap.parse_args()

    try:
        identity = resolve_identity(args.identity)
    except KeyError as exc:
        print(exc, file=sys.stderr)
        return 2

    server = Server(identity)

    if args.list_tools:
        visible = server.session.visible_tools()
        hidden = [t for t in TOOLS.values() if t not in visible]
        print(f"identity: {identity.describe()}  ({len(visible)}/{len(TOOLS)} tools visible)")
        print()
        for t in visible:
            mark = "!" if t.destructive else " "
            print(f"  {mark} {t.name:34} {t.permission}")
        if hidden:
            print()
            print("  withheld from this identity:")
            allow = server.session.catalog.allowed_tools_for(identity.name)
            for t in sorted(hidden, key=lambda x: x.name):
                if not identity.can(t.permission):
                    why = f"identity lacks {t.permission}"
                else:
                    why = "not in AiResource allowedTools"
                print(f"    x {t.name:32} {why}")
        return 0

    if args.http:
        serve_http(server, args.http)
    else:
        serve_stdio(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
