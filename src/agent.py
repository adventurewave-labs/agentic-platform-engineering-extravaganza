#!/usr/bin/env python3
"""
The golden path agent.

WHAT THIS IS, PRECISELY
-----------------------
An agent loop with a deterministic reasoner by default and an optional LLM
backend. Being straight about that split matters, because the interesting
claim in this repo is not "an LLM wrote some YAML".

The loop is:

    render -> evaluate against real policy -> read the denials
           -> change the *inputs* -> re-render -> re-evaluate

Every denial the platform emits carries a policy id and an explicit
remediation after `->`. The reasoner reads that structure and edits the Score
spec parameters, then the whole artefact is regenerated from those parameters
and re-evaluated by conftest. It never patches the rendered manifest to make a
check go green -- that is the failure mode that makes automated remediation
worse than useless, because the next render throws the patch away and the
finding silently returns.

That the default reasoner is deterministic is a feature, not a shortcut. It
makes the demo reproducible on your machine with no API key, and it isolates
the variable being demonstrated: what changes the outcome here is the quality
of the *platform contract*, not the cleverness of the model. Swap in a real
model with `--backend llm` and the loop is identical --
which is the point. A good platform makes the model boring.

  NORTHWIND_LLM_BASE_URL   OpenAI-compatible endpoint (Ollama, vLLM, OpenRouter, Z.AI...)
  NORTHWIND_LLM_MODEL      model name
  NORTHWIND_LLM_API_KEY    key, if the endpoint wants one
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from renderer import ServiceRequest  # noqa: E402

EU_REGIONS = ["eu-west-1", "eu-west-2", "eu-central-1", "eu-north-1"]
US_REGIONS = ["us-east-1", "us-west-2"]
APAC_REGIONS = ["ap-southeast-1", "ap-northeast-1"]

DOWNSIZE = {
    "db.r6g.2xlarge": "db.r6g.xlarge",
    "db.r6g.xlarge": "db.t4g.large",
    "db.t4g.large": "db.t4g.medium",
    "db.t4g.medium": "db.t4g.small",
    "db.t4g.small": "db.t4g.small",
}


@dataclass
class Decision:
    """One change the agent made, and why."""

    policy_id: str
    field: str
    before: Any
    after: Any
    rationale: str

    def render(self) -> str:
        return f"{self.field}: {self.before!r} -> {self.after!r}"


@dataclass
class Turn:
    iteration: int
    deny_count: int
    warn_count: int
    decisions: list[Decision] = field(default_factory=list)
    by_policy: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Intent extraction
# ---------------------------------------------------------------------------

CLASSIFICATION_HINTS = {
    "pci": ["pci", "card", "cardholder", "payment", "pan", "acquirer"],
    "confidential": ["confidential", "pii", "personal data", "gdpr"],
    "internal": ["internal"],
    "public": ["public", "marketing"],
}

RESIDENCY_HINTS = {
    "eu": ["eu", "europe", "gdpr", "ireland", "frankfurt", "london"],
    "us": ["us", "united states", "america", "virginia"],
    "apac": ["apac", "asia", "singapore", "tokyo"],
}


def parse_intent(text: str) -> ServiceRequest:
    """Turn a sentence into a structured platform request.

    Regex, not a model. A model does this better on messy input and worse on
    reproducibility; for a demo whose whole argument is "the platform is what
    makes this safe", the extractor being boring is on-message. Point
    `--backend` at a real model and this is the one function it replaces.
    """
    lowered = text.lower()

    m = re.search(r"(?:called|named)\s+([a-z][a-z0-9-]{2,40})", lowered)
    if not m:
        m = re.search(r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+){1,3})\b(?=\s+service)", lowered)
    name = m.group(1) if m else "new-service"
    name = name.strip("-")

    classification = "internal"
    for level, hints in CLASSIFICATION_HINTS.items():
        if any(h in lowered for h in hints):
            classification = level
            break

    residency = ""
    for region, hints in RESIDENCY_HINTS.items():
        if any(re.search(rf"\b{re.escape(h)}\b", lowered) for h in hints):
            residency = region
            break

    envs = [e for e in ("dev", "staging", "prod")
            if e in lowered or (e == "prod" and "production" in lowered)]
    if not envs:
        envs = ["staging", "prod"]

    owner = "group:default/payments"
    if "storefront" in lowered:
        owner = "group:default/storefront"

    # Scale hints. This is the single most human thing the extractor does, and
    # the single most likely thing to be wrong: "high volume" is a feeling, not
    # a capacity number, and both a model and a hurried engineer will translate
    # it into the biggest instance class they can name. The platform does not
    # argue with that here -- the FinOps gate does, later, with a number.
    hot = any(h in lowered for h in (
        "high volume", "high traffic", "high-throughput", "peak", "black friday",
        "busy", "heavy load", "large scale", "high scale"))

    return ServiceRequest(
        name=name,
        description=text.strip(),
        owner=owner,
        cost_center="CC-4471" if "payments" in owner else "CC-2201",
        monthly_budget_usd=400.0 if "payments" in owner else 900.0,
        classification=classification,
        residency=residency,
        environments=envs,
        database=("postgres" in lowered or "database" in lowered or "db" in lowered),
        replicas=6 if hot else 3,
        db_instance_class="db.r6g.xlarge" if hot else "db.t4g.medium",
        db_storage_gb=500 if hot else 100,
        db_multi_az=bool(hot),
        db_region=_region_for(residency) if residency else "eu-west-1",
    )


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------

def _region_for(residency: str) -> str:
    return {"eu": EU_REGIONS, "us": US_REGIONS, "apac": APAC_REGIONS}.get(
        residency or "eu", EU_REGIONS)[0]


def remediate(req: ServiceRequest, findings: list, environment: str
              ) -> tuple[ServiceRequest, list[Decision]]:
    """Map policy denials onto changes in the *source of truth*.

    Every branch below edits a Score parameter or a request field. Nothing here
    reaches into rendered YAML. If a denial cannot be mapped to an input, the
    agent must say so and stop rather than paper over it -- see
    `unresolved()`.
    """
    decisions: list[Decision] = []
    updates: dict[str, Any] = {}
    # Policies addressed by a change filed under a different id. Two cost rules
    # can both point at the same oversized line item; shrinking it once answers
    # both, and reporting the second as "unresolved" would be a lie.
    covered: set[str] = set()

    def change(fieldname: str, value: Any, pid: str, why: str) -> None:
        before = updates.get(fieldname, getattr(req, fieldname))
        if before == value:
            return
        updates[fieldname] = value
        decisions.append(Decision(pid, fieldname, before, value, why))

    for f in findings:
        pid = f.policy_id
        msg = f.message

        if pid == "NW-PCI-003" and "backupRetentionDays" in msg:
            change("db_backup_retention_days", 30, pid,
                   "PCI requires a 30-day minimum recovery window")

        elif pid == "NW-PCI-003" and "encryption" in msg:
            change("db_encrypted", True, pid,
                   "PCI data at rest must be encrypted")

        elif pid == "NW-PCI-004":
            change("db_region", _region_for(req.residency), pid,
                   f"declared {req.residency or 'eu'} residency constrains the region")

        elif pid == "NW-SCORE-005":
            change("residency", "eu", pid,
                   "PCI classification requires an explicit residency declaration")
            change("db_region", _region_for("eu"), pid,
                   "align the database region with the newly declared residency")

        # NW-FIN-002 and NW-FIN-005 only fire for non-production environments.
        # The demo gates prod, so these branches are exercised when you widen
        # the loop to staging -- which the platform already prevents by
        # defaulting staging to db.t4g.medium with multi-AZ off. Kept because
        # the rule must still catch a hand-written staging spec.
        elif pid == "NW-FIN-002":
            change("db_instance_class", "db.t4g.medium", pid,
                   f"{environment} does not need a production-grade instance class")

        elif pid == "NW-FIN-005":
            change("db_multi_az", False, pid,
                   f"multi-AZ in {environment} doubles spend for no availability gain")

        elif pid in ("NW-FIN-001", "NW-FIN-003"):
            covered.add(pid)
            if "db_instance_class" in updates or "db_storage_gb" in updates:
                # One reduction per round. Take the smallest step that could
                # work and re-measure, rather than over-correcting blind.
                continue
            # Take the cheapest correction that does not touch a compliance
            # control: shrink the instance class first, storage second, and
            # never the backup window.
            current = updates.get("db_instance_class", req.db_instance_class)
            smaller = DOWNSIZE.get(current, current)
            if smaller != current:
                change("db_instance_class", smaller, pid,
                       f"largest reducible line item; {current} -> {smaller} "
                       f"stays inside the cost-centre envelope")
            else:
                storage = updates.get("db_storage_gb", req.db_storage_gb)
                if storage > 50:
                    change("db_storage_gb", max(50, storage // 2), pid,
                           "halve provisioned storage; it grows on demand")

        elif pid == "NW-K8S-007":
            change("replicas", max(2, req.replicas), pid,
                   "production needs to survive a node drain")

        elif pid == "NW-SCORE-001":
            if "cost-center" in msg:
                change("cost_center", "CC-4471", pid, "attribute spend to the owning group")

    for d in decisions:
        covered.add(d.policy_id)
    remediate.last_covered = covered  # type: ignore[attr-defined]
    return replace(req, **updates), decisions


UNMAPPABLE_HINT = (
    "no input-level remediation is defined for this policy; the agent will not "
    "patch rendered output to silence it"
)


def unresolved(findings: list, decisions: list[Decision]) -> list:
    """Denials the agent could not map back to an input."""
    touched = {d.policy_id for d in decisions}
    touched |= getattr(remediate, "last_covered", set())
    return [f for f in findings if f.policy_id not in touched]


# ---------------------------------------------------------------------------
# Optional LLM backend
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a platform engineering agent operating inside the Northwind Retail \
internal developer platform.

You are given a Score workload specification and a list of policy denials from \
the platform's Open Policy Agent bundle. Each denial has the form:

  [POLICY-ID] subject: what is wrong -> how to fix it

Rules you must follow:
1. Fix causes, never symptoms. Change the Score spec parameters. Never edit \
rendered Kubernetes manifests to make a check pass.
2. Never weaken a compliance control to satisfy a cost control. If a budget \
denial and a PCI denial conflict, reduce capacity, not retention or encryption.
3. If a denial cannot be resolved by changing an input, say so explicitly \
rather than guessing.

Reply with a single JSON object: {"changes": {"<field>": <value>}, \
"rationale": {"<field>": "<one sentence>"}}. No prose outside the JSON."""


class LLMBackend:
    """Thin OpenAI-compatible chat client. Works with Ollama, vLLM, OpenRouter,
    Z.AI, Together, or the OpenAI API itself."""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: int = 60) -> None:
        self.base_url = (base_url or os.environ.get(
            "NORTHWIND_LLM_BASE_URL", "http://127.0.0.1:11434/v1")).rstrip("/")
        self.model = model or os.environ.get("NORTHWIND_LLM_MODEL", "qwen2.5-coder:7b")
        self.api_key = api_key or os.environ.get("NORTHWIND_LLM_API_KEY", "")
        self.timeout = timeout

    def available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/models")
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False

    def complete(self, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        return payload["choices"][0]["message"]["content"]

    def remediate(self, req: ServiceRequest, findings: list, environment: str
                  ) -> tuple[ServiceRequest, list[Decision]]:
        prompt = (
            f"Environment: {environment}\n\n"
            f"Current request:\n{req.to_json()}\n\n"
            "Policy denials:\n"
            + "\n".join(f"- {f.message}" for f in findings)
        )
        raw = self.complete(prompt)
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return req, []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return req, []
        changes = data.get("changes", {}) or {}
        rationale = data.get("rationale", {}) or {}
        decisions, updates = [], {}
        for k, v in changes.items():
            if not hasattr(req, k):
                continue
            before = getattr(req, k)
            if before == v:
                continue
            updates[k] = v
            decisions.append(Decision("LLM", k, before, v,
                                      rationale.get(k, "model-proposed change")))
        return replace(req, **updates), decisions


def get_reasoner(backend: str):
    """Return a callable(req, findings, environment) -> (req, decisions).

    Any value other than the deterministic default routes to the
    OpenAI-compatible client above. There is no per-vendor code here on
    purpose: Ollama, vLLM, OpenRouter, Z.AI, Together and the OpenAI API all
    speak the same wire format, and pretending otherwise would imply
    integrations this repo does not have.
    """
    if backend in ("", "deterministic", "replay", "none"):
        return remediate, "deterministic reasoner (no model, fully reproducible)"
    llm = LLMBackend()
    if not llm.available():
        return remediate, (
            f"deterministic reasoner (fallback: no LLM reachable at {llm.base_url})")
    return llm.remediate, f"{llm.model} via {llm.base_url}"
