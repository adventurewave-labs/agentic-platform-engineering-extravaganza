#!/usr/bin/env python3
"""
Real cloud prices from Infracost, instead of the checked-in rate card.

WHY THIS FILE EXISTS
--------------------
`src/costing.py` prices a change from a static table. The README already says
so, and the table is the right default: it means this repo has no account
requirement, no API key, and no network dependency, and the FinOps gate can be
demonstrated on a laptop on a plane.

But "the schema is an Infracost subset -- swap in `infracost breakdown` and the
Rego does not change" is an assertion, and assertions are the thing this
repository is supposed to be allergic to. This module is that assertion made
executable. The Rego genuinely does not change: the only thing that moves is
where `monthlyCostUsd` comes from.

    export NORTHWIND_COST_SOURCE=infracost
    export INFRACOST_API_KEY=...       # free tier is sufficient
    ./run.sh demo

Off by default, and never used by `./run.sh verify` -- a reproducible demo
cannot depend on a live price API, and the committed artefacts would stop being
byte-reproducible the first time AWS moved a price. That is the correct
trade-off, not a limitation to apologise for: the rate card is for determinism,
this module is for accuracy, and you should not want both from one number.

HOW IT WORKS
------------
Infracost prices Terraform, not Score. So this synthesises the smallest
possible Terraform fragment that corresponds to the resources the platform
already decided on -- an `aws_db_instance` for the SQLInstance composite and an
`aws_lb` for the ingress -- runs `infracost breakdown --format json` over it,
and maps the returned monthly costs back onto our resource lines by name.

Compute and log ingestion stay on the rate card: they are Kubernetes-side, and
inventing a Terraform resource to price them would be exactly the kind of
plausible fiction this repository exists to argue against.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class InfracostError(RuntimeError):
    pass


# Our instance classes are RDS classes already; the mapping is the identity.
# Kept explicit so an unmapped class fails loudly rather than being priced as
# something else.
SUPPORTED_DB_CLASSES = {
    "db.t4g.small", "db.t4g.medium", "db.t4g.large",
    "db.r6g.xlarge", "db.r6g.2xlarge",
}


def available() -> bool:
    return bool(shutil.which("infracost")) and bool(os.environ.get("INFRACOST_API_KEY"))


def _terraform(db: dict[str, Any] | None, region: str) -> str:
    """The smallest Terraform that corresponds to what the platform rendered."""
    parts = [
        f'provider "aws" {{\n  region = "{region}"\n}}\n',
        'resource "aws_lb" "ingress" {\n'
        '  name               = "ingress"\n'
        '  load_balancer_type = "application"\n'
        '}\n',
    ]
    if db:
        params = db["params"]
        cls = params["instanceClass"]
        if cls not in SUPPORTED_DB_CLASSES:
            raise InfracostError(f"unmapped instance class: {cls}")
        parts.append(
            'resource "aws_db_instance" "postgres" {\n'
            f'  instance_class          = "{cls}"\n'
            '  engine                  = "postgres"\n'
            f'  allocated_storage       = {int(params["storageGb"])}\n'
            '  storage_type            = "gp3"\n'
            f'  multi_az                = {str(bool(params["multiAz"])).lower()}\n'
            f'  backup_retention_period = {int(params["backupRetentionDays"])}\n'
            '}\n'
        )
    return "\n".join(parts)


def price(resources: list[dict[str, Any]], region: str = "eu-west-1",
          timeout: int = 120) -> dict[str, float]:
    """Return {our resource name: monthly USD} for the lines Infracost can price.

    Raises InfracostError rather than returning a partial guess. A cost gate
    fed a number nobody can account for is worse than a cost gate that stopped.
    """
    if not shutil.which("infracost"):
        raise InfracostError("infracost is not on PATH — https://infracost.io/docs")
    if not os.environ.get("INFRACOST_API_KEY"):
        raise InfracostError("INFRACOST_API_KEY is not set")

    db = next((r for r in resources if r["type"] == "platform_sqlinstance"), None)
    lb = next((r for r in resources if r["type"] == "load_balancer"), None)

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "main.tf").write_text(_terraform(db, region))
        proc = subprocess.run(
            ["infracost", "breakdown", "--path", tmp, "--format", "json",
             "--no-color"],
            capture_output=True, text=True, timeout=timeout,
        )
    if proc.returncode != 0:
        raise InfracostError(
            f"infracost exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    try:
        report = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise InfracostError("infracost did not return JSON") from exc

    priced: dict[str, float] = {}
    for project in report.get("projects") or []:
        for r in ((project.get("breakdown") or {}).get("resources") or []):
            monthly = r.get("monthlyCost")
            if monthly is None:
                continue
            name = r.get("name", "")
            if name.endswith("aws_db_instance.postgres") and db:
                priced[db["name"]] = round(float(monthly), 2)
            elif name.endswith("aws_lb.ingress") and lb:
                priced[lb["name"]] = round(float(monthly), 2)
    if not priced:
        raise InfracostError("infracost returned no priced resources")
    return priced


def apply(estimate: dict[str, Any], region: str = "eu-west-1") -> dict[str, Any]:
    """Overlay Infracost's prices onto an estimate, in place, and retotal.

    The document handed to `policy/cost/budget.rego` keeps exactly the same
    shape. `costSource` is added so a reader can tell a rate-card number from a
    priced one without having to ask.
    """
    priced = price(estimate["resources"], region)
    for r in estimate["resources"]:
        if r["name"] in priced:
            r["monthlyCostUsd"] = priced[r["name"]]
            r["priceSource"] = "infracost"
        else:
            r["priceSource"] = "rate-card"
    estimate["totalMonthlyCostUsd"] = round(
        sum(r["monthlyCostUsd"] for r in estimate["resources"]), 2)
    estimate["costSource"] = "infracost+rate-card"
    return estimate
