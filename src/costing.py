#!/usr/bin/env python3
"""
Cost estimation for a proposed change.

The estimate is produced *before* anything is provisioned and handed to the
policy engine as just another input, which is the only arrangement that makes
cost a guardrail rather than a postmortem. Infracost's own framing shifted this
way in 2026 -- its tagline is now "cloud cost intelligence for engineers, AI
coding agents, and CI/CD" -- and the reason is obvious once agents are opening
pull requests: the thing proposing infrastructure no longer has a monthly bill
of its own to feel bad about.

The rate card below is a static, checked-in table so this repo has no external
API dependency and no account requirement. Prices are illustrative and
deliberately round; the *mechanism* is the point, not the accuracy of any given
figure. Point `NORTHWIND_RATE_CARD` at your own JSON, or swap this module for
`infracost breakdown --format json` -- the schema consumed by policy/cost/ is a
subset of Infracost's, so the Rego does not change.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# USD per month, illustrative, eu-west-1-ish.
RATE_CARD: dict[str, Any] = {
    "db_instance": {
        "db.t4g.small": 26.00,
        "db.t4g.medium": 52.00,
        "db.t4g.large": 104.00,
        "db.r6g.xlarge": 372.00,
        "db.r6g.2xlarge": 744.00,
    },
    "db_storage_gb_month": 0.13,
    "db_backup_gb_month": 0.10,
    "multi_az_multiplier": 2.0,
    "vcpu_hour": 0.0405,
    "gib_hour": 0.0045,
    "hours_per_month": 730,
    "load_balancer_month": 18.50,
    "log_gb_month": 0.55,
    "estimated_log_gb": {"prod": 42.0, "staging": 6.0, "dev": 1.5},
}


def _load_rate_card() -> dict[str, Any]:
    override = os.environ.get("NORTHWIND_RATE_CARD")
    if override and Path(override).exists():
        return json.loads(Path(override).read_text())
    return RATE_CARD


def _cpu_to_vcpu(value: str) -> float:
    value = str(value).strip()
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0
    return float(value)


def _mem_to_gib(value: str) -> float:
    value = str(value).strip()
    units = {"Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1.0, "Ti": 1024.0}
    for suffix, mult in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * mult
    return float(value) / (1024 ** 3)


def estimate(req, environment: str, previous_monthly_cost: float = 0.0) -> dict[str, Any]:
    """Produce an Infracost-shaped estimate for one environment.

    Returns the exact document that policy/cost/budget.rego evaluates.
    """
    rc = _load_rate_card()
    resources: list[dict[str, Any]] = []

    replicas = req.replicas_for(environment)
    vcpu = _cpu_to_vcpu(req.cpu) * replicas
    gib = _mem_to_gib(req.memory) * replicas
    compute = (vcpu * rc["vcpu_hour"] + gib * rc["gib_hour"]) * rc["hours_per_month"]
    resources.append({
        "name": f"{req.name}-compute",
        "type": "kubernetes_workload",
        "monthlyCostUsd": round(compute, 2),
        "params": {
            "replicas": replicas,
            "cpu": req.cpu,
            "memory": req.memory,
            "costCenter": req.cost_center,
        },
    })

    if req.database:
        instance_class = req.db_class_for(environment)
        multi_az = bool(req.db_multi_az and environment == "prod")
        base = rc["db_instance"].get(instance_class, 52.00)
        if multi_az:
            base *= rc["multi_az_multiplier"]
        storage = req.db_storage_gb * rc["db_storage_gb_month"]
        backup = (
            req.db_storage_gb
            * (req.db_backup_retention_days / 30.0)
            * rc["db_backup_gb_month"]
        )
        resources.append({
            "name": f"{req.name}-postgres",
            "type": "platform_sqlinstance",
            "monthlyCostUsd": round(base + storage + backup, 2),
            "params": {
                "instanceClass": instance_class,
                "storageGb": req.db_storage_gb,
                "backupRetentionDays": req.db_backup_retention_days,
                "multiAz": multi_az,
                "region": req.db_region,
                "costCenter": req.cost_center,
            },
        })

    resources.append({
        "name": f"{req.name}-ingress",
        "type": "load_balancer",
        "monthlyCostUsd": round(rc["load_balancer_month"], 2),
        "params": {"costCenter": req.cost_center},
    })

    log_gb = rc["estimated_log_gb"].get(environment, 5.0)
    resources.append({
        "name": f"{req.name}-observability",
        "type": "log_ingestion",
        "monthlyCostUsd": round(log_gb * rc["log_gb_month"], 2),
        "params": {"estimatedGb": log_gb, "costCenter": req.cost_center},
    })

    total = round(sum(r["monthlyCostUsd"] for r in resources), 2)
    return {
        "service": req.name,
        "environment": environment,
        "costCenter": req.cost_center,
        "monthlyBudgetUsd": float(req.monthly_budget_usd),
        "totalMonthlyCostUsd": total,
        "previousMonthlyCostUsd": float(previous_monthly_cost),
        "currency": "USD",
        "resources": resources,
    }


def estimate_all(req, previous: dict[str, float] | None = None) -> dict[str, Any]:
    previous = previous or {}
    per_env = {
        env: estimate(req, env, previous.get(env, 0.0)) for env in req.environments
    }
    return {
        "service": req.name,
        "totalMonthlyCostUsd": round(
            sum(e["totalMonthlyCostUsd"] for e in per_env.values()), 2
        ),
        "environments": per_env,
    }


def render_table(est: dict[str, Any]) -> str:
    lines = []
    width = max((len(r["name"]) for r in est["resources"]), default=20) + 2
    lines.append(f"  {'RESOURCE'.ljust(width)}{'TYPE'.ljust(24)}{'USD/MONTH':>12}")
    lines.append(f"  {'-' * (width + 24 + 12)}")
    for r in sorted(est["resources"], key=lambda x: -x["monthlyCostUsd"]):
        lines.append(
            f"  {r['name'].ljust(width)}{r['type'].ljust(24)}"
            f"{r['monthlyCostUsd']:>12,.2f}"
        )
    lines.append(f"  {'-' * (width + 24 + 12)}")
    lines.append(
        f"  {'TOTAL'.ljust(width)}{('budget $' + format(est['monthlyBudgetUsd'], ',.2f')).ljust(24)}"
        f"{est['totalMonthlyCostUsd']:>12,.2f}"
    )
    return "\n".join(lines)
