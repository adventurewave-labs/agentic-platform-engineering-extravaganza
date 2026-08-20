#!/usr/bin/env python3
"""
Northwind platform control plane — software catalog + authorization.

The entity model is deliberately Backstage-shaped (Component / System / API /
Group / Resource, plus the newer `AiResource` kind) so that everything here is
portable to a real Backstage instance: the same catalog-info.yaml files, the
same relations, the same template parameter schema.

The part that is *not* Backstage-shaped is the authorization model, and that is
on purpose. Every tool this platform exposes to an agent carries a
`ToolPermission` mapped to a platform action, and the action is checked against
the *caller's identity* before the tool is even listed. An agent that cannot
approve a production promotion never sees `platform.approve_promotion` in its
tool list -- it is not tempted, and it cannot hallucinate the call.

That pattern is lifted from OpenChoreo, which is the only OSS IDP I found that
gates agent tool access through the same policy decision point as human access
(see pkg/mcp/tools/*.go in openchoreo/openchoreo). It is the single most
important design idea in this repo.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "platform" / "catalog"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

class Action:
    """Platform actions. One per thing an identity might be allowed to do."""

    VIEW_CATALOG = "catalog:read"
    REGISTER_ENTITY = "catalog:write"
    REFRESH_ENTITY = "catalog:refresh"
    LIST_TEMPLATES = "scaffolder:read"
    EXECUTE_TEMPLATE = "scaffolder:execute"
    RENDER_WORKLOAD = "platform:render"
    EVALUATE_POLICY = "policy:evaluate"
    ESTIMATE_COST = "finops:read"
    PLAN_PROMOTION = "delivery:plan"
    OPEN_PULL_REQUEST = "delivery:propose"
    APPROVE_PROMOTION = "delivery:approve"
    READ_TELEMETRY = "observability:read"


@dataclass(frozen=True)
class Identity:
    """Who is calling. Agents are first-class principals, not superusers."""

    name: str
    kind: str  # "user" | "agent" | "service"
    groups: tuple[str, ...] = ()
    allowed_actions: frozenset[str] = frozenset()

    def can(self, action: str) -> bool:
        return action in self.allowed_actions

    def describe(self) -> str:
        return f"{self.kind}:{self.name}"


# The four principals the demo uses. Each name matches an AiResource entity in
# platform/catalog/ai-resources.yaml, which narrows it further.
#
# Note what the platform-agent is NOT granted: APPROVE_PROMOTION. The agent can
# plan a production promotion and open the pull request that would perform it,
# but a human in @northwind/payments has to approve it. Human-in-the-loop is a
# property of the authorization model here, not a prompt instruction -- which
# means it cannot be jailbroken, only revoked.
IDENTITIES: dict[str, Identity] = {
    "platform-agent": Identity(
        name="platform-agent",
        kind="agent",
        groups=("group:default/platform-team",),
        allowed_actions=frozenset({
            Action.VIEW_CATALOG,
            Action.REGISTER_ENTITY,
            Action.REFRESH_ENTITY,
            Action.LIST_TEMPLATES,
            Action.EXECUTE_TEMPLATE,
            Action.RENDER_WORKLOAD,
            Action.EVALUATE_POLICY,
            Action.ESTIMATE_COST,
            Action.PLAN_PROMOTION,
            Action.OPEN_PULL_REQUEST,
        }),
    ),
    "drift-agent": Identity(
        name="drift-agent",
        kind="agent",
        groups=("group:default/platform-team",),
        allowed_actions=frozenset({
            Action.VIEW_CATALOG,
            Action.EVALUATE_POLICY,
            Action.READ_TELEMETRY,
            Action.OPEN_PULL_REQUEST,
        }),
    ),
    "cost-reviewer": Identity(
        name="cost-reviewer",
        kind="agent",
        groups=(),
        allowed_actions=frozenset({
            Action.VIEW_CATALOG,
            Action.LIST_TEMPLATES,
            Action.EVALUATE_POLICY,
            Action.ESTIMATE_COST,
        }),
    ),
    "release-manager": Identity(
        name="release-manager",
        kind="user",
        groups=("group:default/payments", "group:default/platform-team"),
        allowed_actions=frozenset({
            Action.VIEW_CATALOG,
            Action.REGISTER_ENTITY,
            Action.REFRESH_ENTITY,
            Action.LIST_TEMPLATES,
            Action.EXECUTE_TEMPLATE,
            Action.RENDER_WORKLOAD,
            Action.EVALUATE_POLICY,
            Action.ESTIMATE_COST,
            Action.PLAN_PROMOTION,
            Action.OPEN_PULL_REQUEST,
            Action.APPROVE_PROMOTION,
            Action.READ_TELEMETRY,
        }),
    ),
}


def resolve_identity(name: str | None = None) -> Identity:
    name = name or os.environ.get("NORTHWIND_IDENTITY", "platform-agent")
    if name not in IDENTITIES:
        raise KeyError(
            f"unknown identity {name!r}; known: {', '.join(sorted(IDENTITIES))}"
        )
    return IDENTITIES[name]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

@dataclass
class Entity:
    raw: dict[str, Any]
    source: str = ""
    generation: int = 1

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "")

    @property
    def name(self) -> str:
        return self.raw.get("metadata", {}).get("name", "")

    @property
    def namespace(self) -> str:
        return self.raw.get("metadata", {}).get("namespace", "default")

    @property
    def ref(self) -> str:
        return f"{self.kind.lower()}:{self.namespace}/{self.name}"

    @property
    def owner(self) -> str:
        return self.raw.get("spec", {}).get("owner", "")

    @property
    def tags(self) -> list[str]:
        return self.raw.get("metadata", {}).get("tags", []) or []

    def summary(self) -> dict[str, Any]:
        meta = self.raw.get("metadata", {})
        spec = self.raw.get("spec", {})
        out = {
            "ref": self.ref,
            "kind": self.kind,
            "name": self.name,
            "title": meta.get("title", self.name),
            "description": meta.get("description", ""),
            "owner": spec.get("owner", ""),
            "lifecycle": spec.get("lifecycle", ""),
            "type": spec.get("type", ""),
            "tags": self.tags,
        }
        return {k: v for k, v in out.items() if v not in ("", [], None)}


class Catalog:
    """Loads Backstage-shaped entity files from platform/catalog/."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or CATALOG_DIR)
        self.entities: list[Entity] = []
        self.refresh_log: list[str] = []
        self.load()

    def load(self) -> None:
        self.entities = []
        if not self.directory.exists():
            return
        for path in sorted(self.directory.rglob("*.yaml")):
            # Software templates are loaded separately by the scaffolder.
            if "templates" in path.parts and path.name != "template.yaml":
                continue
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if isinstance(doc, dict) and doc.get("kind"):
                    rel = str(path.relative_to(ROOT))
                    self.entities.append(Entity(raw=doc, source=rel))

    # -- queries ------------------------------------------------------------

    def query(
        self,
        kind: str | None = None,
        owner: str | None = None,
        tag: str | None = None,
        text: str | None = None,
        limit: int = 50,
    ) -> list[Entity]:
        results = []
        for e in self.entities:
            if kind and e.kind.lower() != kind.lower():
                continue
            if owner and owner not in e.owner:
                continue
            if tag and tag not in e.tags:
                continue
            if text:
                blob = yaml.safe_dump(e.raw).lower()
                if not fnmatch.fnmatch(blob, f"*{text.lower()}*"):
                    continue
            results.append(e)
            if len(results) >= limit:
                break
        return results

    def get(self, ref: str) -> Entity | None:
        ref = ref.lower()
        for e in self.entities:
            if e.ref == ref or e.name.lower() == ref:
                return e
        return None

    def register(self, doc: dict[str, Any], source: str = "runtime") -> Entity:
        entity = Entity(raw=doc, source=source)
        existing = self.get(entity.ref)
        if existing:
            existing.raw = doc
            existing.generation += 1
            return existing
        self.entities.append(entity)
        return entity

    def refresh(self, ref: str) -> Entity | None:
        """Re-queue an entity for processing and return its fresh state.

        This mirrors Backstage's `refresh-catalog-entity` action (shipped in
        v1.54.0), which exists specifically so an agent can scaffold something
        and then immediately read back the catalog's view of it without racing
        the processing loop. Without this, the scaffold -> verify half of an
        agentic golden path does not close.
        """
        entity = self.get(ref)
        if entity is None:
            return None
        entity.generation += 1
        self.refresh_log.append(ref)
        return entity

    # -- AiResource governance ---------------------------------------------

    def ai_resources(self) -> list[Entity]:
        return [e for e in self.entities if e.kind == "AiResource"]

    def allowed_tools_for(self, agent_name: str) -> list[str] | None:
        """Read the allowedTools allow-list an AiResource declares for an agent.

        The catalog is the source of truth for what an agent may do, which
        means changing an agent's blast radius is a pull request against a YAML
        file that a human reviews -- not a prompt edit.
        """
        for e in self.ai_resources():
            if e.name == agent_name:
                return e.raw.get("spec", {}).get("allowedTools")
        return None


# ---------------------------------------------------------------------------
# Software templates (golden paths)
# ---------------------------------------------------------------------------

@dataclass
class Template:
    raw: dict[str, Any]
    directory: Path

    @property
    def name(self) -> str:
        return self.raw.get("metadata", {}).get("name", "")

    @property
    def title(self) -> str:
        return self.raw.get("metadata", {}).get("title", self.name)

    @property
    def description(self) -> str:
        return self.raw.get("metadata", {}).get("description", "")

    @property
    def tags(self) -> list[str]:
        return self.raw.get("metadata", {}).get("tags", []) or []

    def parameter_schema(self) -> dict[str, Any]:
        params = self.raw.get("spec", {}).get("parameters", [])
        merged: dict[str, Any] = {"required": [], "properties": {}}
        for group in params:
            merged["required"].extend(group.get("required", []))
            merged["properties"].update(group.get("properties", {}))
        return merged

    def skeleton(self) -> Path:
        return self.directory / "skeleton"


def load_templates(directory: Path | None = None) -> list[Template]:
    base = Path(directory or CATALOG_DIR / "templates")
    out: list[Template] = []
    if not base.exists():
        return out
    for path in sorted(base.rglob("template.yaml")):
        doc = yaml.safe_load(path.read_text())
        if isinstance(doc, dict) and doc.get("kind") == "Template":
            out.append(Template(raw=doc, directory=path.parent))
    return out


def find_template(name: str) -> Template | None:
    for t in load_templates():
        if t.name == name:
            return t
    return None
