#!/usr/bin/env python3
"""
Unit tests for cost estimation and the Infracost adapter.

The FinOps act of the demo is only meaningful if the estimate moves in the
right direction for the right reason. These pin the arithmetic and the unit
parsing -- `500m` of CPU and `512Mi` of memory are the two most quietly
wrong-able conversions in the repository.
"""

import unittest

import context  # noqa: F401

import costing
from renderer import ServiceRequest


def _req(**kw):
    base = dict(
        name="svc", description="d", owner="group:default/payments",
        cost_center="CC-4471", monthly_budget_usd=400.0, classification="pci",
        residency="eu", environments=["staging", "prod"], database=True,
        replicas=3, db_instance_class="db.t4g.medium", db_storage_gb=100,
        db_multi_az=False, db_region="eu-west-1",
    )
    base.update(kw)
    return ServiceRequest(**base)


class TestUnitParsing(unittest.TestCase):

    def test_millicores(self):
        self.assertAlmostEqual(costing._cpu_to_vcpu("500m"), 0.5)
        self.assertAlmostEqual(costing._cpu_to_vcpu("100m"), 0.1)

    def test_whole_cores(self):
        self.assertAlmostEqual(costing._cpu_to_vcpu("2"), 2.0)

    def test_memory_suffixes(self):
        self.assertAlmostEqual(costing._mem_to_gib("512Mi"), 0.5)
        self.assertAlmostEqual(costing._mem_to_gib("2Gi"), 2.0)
        self.assertAlmostEqual(costing._mem_to_gib("1Ti"), 1024.0)


class TestEstimate(unittest.TestCase):

    def test_document_shape_is_what_the_rego_reads(self):
        est = costing.estimate(_req(), "prod")
        for key in ("service", "environment", "costCenter", "monthlyBudgetUsd",
                    "totalMonthlyCostUsd", "currency", "resources"):
            self.assertIn(key, est)
        for r in est["resources"]:
            self.assertIn("monthlyCostUsd", r)
            self.assertIn("costCenter", r["params"])

    def test_total_is_the_sum_of_the_lines(self):
        est = costing.estimate(_req(), "prod")
        self.assertAlmostEqual(
            est["totalMonthlyCostUsd"],
            round(sum(r["monthlyCostUsd"] for r in est["resources"]), 2))

    def test_default_source_is_declared(self):
        # A number's provenance is part of the number. A reader must be able to
        # tell a rate-card figure from a priced one without asking.
        self.assertEqual(costing.estimate(_req(), "prod")["costSource"], "rate-card")

    def test_a_bigger_instance_class_costs_more(self):
        small = costing.estimate(_req(db_instance_class="db.t4g.medium"), "prod")
        large = costing.estimate(_req(db_instance_class="db.r6g.xlarge"), "prod")
        self.assertGreater(large["totalMonthlyCostUsd"], small["totalMonthlyCostUsd"])

    def test_multi_az_doubles_the_instance_line_in_prod_only(self):
        single = costing.estimate(_req(db_multi_az=False), "prod")
        double = costing.estimate(_req(db_multi_az=True), "prod")
        db_single = next(r for r in single["resources"] if r["type"] == "platform_sqlinstance")
        db_double = next(r for r in double["resources"] if r["type"] == "platform_sqlinstance")
        self.assertGreater(db_double["monthlyCostUsd"], db_single["monthlyCostUsd"])
        # Not in staging: paying twice for availability nobody is measuring.
        staging = costing.estimate(_req(db_multi_az=True), "staging")
        db_staging = next(r for r in staging["resources"] if r["type"] == "platform_sqlinstance")
        self.assertFalse(db_staging["params"]["multiAz"])

    def test_no_database_means_no_database_line(self):
        est = costing.estimate(_req(database=False), "prod")
        self.assertFalse([r for r in est["resources"] if r["type"] == "platform_sqlinstance"])

    def test_rate_card_override(self):
        import json
        import tempfile
        from pathlib import Path
        card = dict(costing.RATE_CARD)
        card["load_balancer_month"] = 999.0
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "card.json"
            p.write_text(json.dumps(card))
            import os
            os.environ["NORTHWIND_RATE_CARD"] = str(p)
            try:
                est = costing.estimate(_req(), "prod")
            finally:
                os.environ.pop("NORTHWIND_RATE_CARD", None)
        lb = next(r for r in est["resources"] if r["type"] == "load_balancer")
        self.assertEqual(lb["monthlyCostUsd"], 999.0)


class TestInfracostAdapter(unittest.TestCase):
    """The adapter is off by default and needs no binary to be tested: what is
    worth pinning is that it overlays prices without changing the document
    shape the Rego reads, and that it refuses rather than guessing."""

    def _estimate(self):
        return costing.estimate(_req(), "prod")

    def test_apply_overlays_prices_and_retotals(self):
        from sources import infracost
        est = self._estimate()
        db = next(r for r in est["resources"] if r["type"] == "platform_sqlinstance")
        lb = next(r for r in est["resources"] if r["type"] == "load_balancer")
        original_keys = set(est)

        infracost.price = lambda resources, region="eu-west-1", timeout=120: {
            db["name"]: 111.11, lb["name"]: 22.22}
        out = infracost.apply(est, "eu-west-1")

        self.assertEqual(db["monthlyCostUsd"], 111.11)
        self.assertEqual(db["priceSource"], "infracost")
        self.assertEqual(out["totalMonthlyCostUsd"],
                         round(sum(r["monthlyCostUsd"] for r in out["resources"]), 2))
        # Shape must be a superset, never a different document.
        self.assertTrue(original_keys <= set(out))

    def test_unpriced_lines_are_labelled_rate_card(self):
        from sources import infracost
        est = self._estimate()
        infracost.price = lambda resources, region="eu-west-1", timeout=120: {}
        # An empty price map is not a valid result -- price() itself raises --
        # but apply() must still label honestly if handed one.
        out = infracost.apply(est, "eu-west-1")
        self.assertTrue(all(r["priceSource"] == "rate-card" for r in out["resources"]))

    def test_unmapped_instance_class_is_refused(self):
        from sources import infracost
        with self.assertRaises(infracost.InfracostError):
            infracost._terraform(
                {"params": {"instanceClass": "db.made.up", "storageGb": 10,
                            "multiAz": False, "backupRetentionDays": 7}},
                "eu-west-1")

    def test_generated_terraform_reflects_the_platform_decision(self):
        from sources import infracost
        tf = infracost._terraform(
            {"params": {"instanceClass": "db.t4g.medium", "storageGb": 100,
                        "multiAz": True, "backupRetentionDays": 30}},
            "eu-west-1")
        self.assertIn('instance_class          = "db.t4g.medium"', tf)
        self.assertIn("multi_az                = true", tf)
        self.assertIn("backup_retention_period = 30", tf)
        self.assertIn('region = "eu-west-1"', tf)


if __name__ == "__main__":
    unittest.main()
