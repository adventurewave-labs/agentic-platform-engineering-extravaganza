#!/usr/bin/env python3
"""
Unit tests for the reasoning layer.

`./run.sh verify` proves the system converges. It does not tell you *which*
function broke when it stops converging, and it cannot cover the branches the
worked example never reaches -- the staging-only FinOps rules, the unmappable
denial, the intent extractor's whole surface. A regex intent extractor with no
table-driven test is the single most likely thing in this repository to rot
quietly, so it gets one.

Stdlib unittest on purpose: PyYAML stays the only dependency this repo has.
"""

import unittest

import context  # noqa: F401

import agent
from gates import Finding
from renderer import ServiceRequest


class TestParseIntent(unittest.TestCase):

    def test_name_from_called(self):
        r = agent.parse_intent("a service called payments-ledger for cards")
        self.assertEqual(r.name, "payments-ledger")

    def test_name_from_named(self):
        r = agent.parse_intent("we need a thing named order-router")
        self.assertEqual(r.name, "order-router")

    def test_name_falls_back(self):
        self.assertEqual(agent.parse_intent("just make something").name, "new-service")

    def test_classification(self):
        cases = {
            "a PCI cardholder store": "pci",
            "handles GDPR personal data": "confidential",
            "an internal tool": "internal",
            "a public marketing page": "public",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(agent.parse_intent(text).classification, expected)

    def test_pci_beats_confidential_when_both_present(self):
        # Order matters: the strictest classification present must win, or the
        # residency and encryption rules never fire.
        r = agent.parse_intent("PCI cardholder data, also GDPR personal data")
        self.assertEqual(r.classification, "pci")

    def test_residency(self):
        for text, expected in {"EU residency": "eu", "hosted in Frankfurt": "eu",
                               "US only": "us", "for APAC": "apac",
                               "no preference": ""}.items():
            with self.subTest(text=text):
                self.assertEqual(agent.parse_intent(text).residency, expected)

    def test_residency_does_not_match_inside_words(self):
        # 'us' inside 'customers' must not be read as US residency. This is the
        # exact failure a naive substring check produces.
        self.assertEqual(agent.parse_intent("a service for our customers").residency, "")

    def test_environments_default_and_explicit(self):
        self.assertEqual(agent.parse_intent("a service").environments, ["staging", "prod"])
        self.assertEqual(
            agent.parse_intent("dev, staging and production").environments,
            ["dev", "staging", "prod"])

    def test_scale_hint_inflates_the_first_proposal(self):
        # The extractor deliberately over-provisions on a vague scale hint; the
        # FinOps gate is what argues it back down. If this stops being true the
        # cost act of the demo silently becomes a no-op.
        quiet = agent.parse_intent("a service called a-b with a postgres database")
        hot = agent.parse_intent("a service called a-b, high volume at peak, postgres")
        self.assertGreater(hot.replicas, quiet.replicas)
        self.assertTrue(hot.db_multi_az)
        self.assertFalse(quiet.db_multi_az)

    def test_database_detected(self):
        self.assertTrue(agent.parse_intent("needs a postgres database").database)
        self.assertFalse(agent.parse_intent("a stateless proxy").database)


def _req(**kw):
    base = dict(
        name="svc", description="d", owner="group:default/payments",
        cost_center="CC-4471", monthly_budget_usd=400.0, classification="pci",
        residency="eu", environments=["prod"], database=True, replicas=3,
        db_instance_class="db.r6g.xlarge", db_storage_gb=500,
        db_multi_az=True, db_region="eu-west-1",
    )
    base.update(kw)
    return ServiceRequest(**base)


def _finding(pid, message):
    return Finding(policy_id=pid, message=message, subject="s")


class TestRemediate(unittest.TestCase):

    def test_pci_backup_window(self):
        req, decisions = agent.remediate(
            _req(), [_finding("NW-PCI-003", "db: backupRetentionDays is 7 -> raise to 30")],
            "prod")
        self.assertEqual(req.db_backup_retention_days, 30)
        self.assertEqual([d.field for d in decisions], ["db_backup_retention_days"])

    def test_pci_encryption(self):
        req, _ = agent.remediate(
            _req(db_encrypted=False),
            [_finding("NW-PCI-003", "db: encryption at rest is off -> enable it")], "prod")
        self.assertTrue(req.db_encrypted)

    def test_cost_never_weakens_a_compliance_control(self):
        """The rule that matters most. A budget denial must never be answered
        by shortening the backup window or turning off encryption."""
        before = _req(db_backup_retention_days=30, db_encrypted=True)
        after, _ = agent.remediate(
            before,
            [_finding("NW-FIN-001", "total: $960.82 exceeds the $400.00 envelope -> reduce")],
            "prod")
        self.assertEqual(after.db_backup_retention_days, 30)
        self.assertTrue(after.db_encrypted)
        self.assertNotEqual(after.db_instance_class, before.db_instance_class)

    def test_cost_takes_one_step_at_a_time(self):
        after, decisions = agent.remediate(
            _req(), [_finding("NW-FIN-001", "over budget"),
                     _finding("NW-FIN-003", "over budget")], "prod")
        # One reduction per round, then re-measure.
        self.assertEqual(len(decisions), 1)
        self.assertEqual(after.db_instance_class, "db.t4g.large")

    def test_cost_falls_through_to_storage_when_class_is_minimal(self):
        after, _ = agent.remediate(
            _req(db_instance_class="db.t4g.small", db_storage_gb=500),
            [_finding("NW-FIN-001", "over budget")], "prod")
        self.assertEqual(after.db_storage_gb, 250)

    def test_residency_declaration_sets_the_region_too(self):
        after, _ = agent.remediate(
            _req(residency="", db_region="us-east-1"),
            [_finding("NW-SCORE-005", "pci workload has no declared residency")], "prod")
        self.assertEqual(after.residency, "eu")
        self.assertTrue(after.db_region.startswith("eu-"))

    def test_no_change_produces_no_decision(self):
        _, decisions = agent.remediate(
            _req(db_backup_retention_days=30),
            [_finding("NW-PCI-003", "backupRetentionDays")], "prod")
        self.assertEqual(decisions, [])

    def test_unmappable_denial_is_reported_not_papered_over(self):
        findings = [_finding("NW-XYZ-999", "something no input controls")]
        req, decisions = agent.remediate(_req(), findings, "prod")
        left = agent.unresolved(findings, decisions)
        self.assertEqual([f.policy_id for f in left], ["NW-XYZ-999"])

    def test_two_cost_rules_on_one_line_item_are_both_covered(self):
        findings = [_finding("NW-FIN-001", "over"), _finding("NW-FIN-003", "over")]
        _, decisions = agent.remediate(_req(), findings, "prod")
        self.assertEqual(agent.unresolved(findings, decisions), [])


class TestReasonerSelection(unittest.TestCase):

    def test_default_is_deterministic(self):
        for backend in ("", "deterministic", "replay", "none"):
            fn, label = agent.get_reasoner(backend)
            self.assertIs(fn, agent.remediate)
            self.assertIn("deterministic", label)

    def test_llm_backend_is_used_when_reachable(self):
        import os
        from fake_llm import serve
        with serve() as (base_url, _):
            os.environ["NORTHWIND_LLM_BASE_URL"] = base_url
            try:
                fn, label = agent.get_reasoner("llm")
                self.assertIsNot(fn, agent.remediate)
                self.assertNotIn("deterministic", label)
            finally:
                os.environ.pop("NORTHWIND_LLM_BASE_URL", None)

    def test_llm_backend_falls_back_when_unreachable(self):
        import os
        os.environ["NORTHWIND_LLM_BASE_URL"] = "http://127.0.0.1:1"
        try:
            fn, label = agent.get_reasoner("llm")
            self.assertIs(fn, agent.remediate)
            self.assertIn("fallback", label)
        finally:
            os.environ.pop("NORTHWIND_LLM_BASE_URL", None)

    def test_llm_reply_that_is_not_json_changes_nothing(self):
        """A model that ignores the output contract must be inert, not
        destructive. Returning the request unchanged stalls the loop, which the
        caller reports; guessing would be worse."""
        from fake_llm import serve
        import os
        with serve(transcript=[]) as (base_url, _):
            os.environ["NORTHWIND_LLM_BASE_URL"] = base_url
            try:
                backend = agent.LLMBackend()
                before = _req()
                after, decisions = backend.remediate(
                    before, [_finding("NW-PCI-003", "backupRetentionDays")], "prod")
                self.assertEqual(decisions, [])
                self.assertEqual(after, before)
            finally:
                os.environ.pop("NORTHWIND_LLM_BASE_URL", None)

    def test_llm_cannot_invent_fields(self):
        """A model proposing a field the request does not have must be ignored
        rather than crashing or silently growing the schema."""
        from fake_llm import serve
        import os
        with serve(transcript=[{"changes": {"not_a_field": 1, "replicas": 4},
                                "rationale": {}}]) as (base_url, _):
            os.environ["NORTHWIND_LLM_BASE_URL"] = base_url
            try:
                after, decisions = agent.LLMBackend().remediate(
                    _req(), [_finding("NW-K8S-007", "x")], "prod")
                self.assertEqual([d.field for d in decisions], ["replicas"])
                self.assertFalse(hasattr(after, "not_a_field"))
            finally:
                os.environ.pop("NORTHWIND_LLM_BASE_URL", None)


class TestTranscriptIsCurrent(unittest.TestCase):
    """The recorded transcript in tests/fake_llm.py must stay the deterministic
    reasoner's actual decisions. If the reasoner changes and the transcript does
    not, T15 would be replaying a fiction -- so this regenerates it and compares.
    """

    def test_transcript_matches_the_deterministic_reasoner(self):
        import costing
        import gates
        import renderer
        from fake_llm import TRANSCRIPT

        req = agent.parse_intent(
            "service called payments-ledger PCI EU high volume postgres "
            "staging and prod")
        recorded = []
        for _ in range(5):
            spec, docs = renderer.golden_path(req, "prod")
            report = gates.run_all(docs, score=spec, cost=costing.estimate(req, "prod"))
            if report.passed:
                break
            req, decisions = agent.remediate(req, report.findings, "prod")
            if not decisions:
                break
            recorded.append({d.field: d.after for d in decisions})

        self.assertEqual([t["changes"] for t in TRANSCRIPT], recorded,
                         "tests/fake_llm.py TRANSCRIPT is stale — update it")


if __name__ == "__main__":
    unittest.main()
