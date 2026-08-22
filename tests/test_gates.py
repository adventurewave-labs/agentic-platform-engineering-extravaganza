#!/usr/bin/env python3
"""
Unit tests for the gate layer: finding parsing and report aggregation.

`Finding.parse` is the seam between a Rego message and everything the agent
does with it. The whole remediation loop depends on the `[POLICY-ID] subject:
what is wrong -> how to fix it` convention holding, so it is tested directly
rather than only through the system run.
"""

import unittest

import context  # noqa: F401

import gates
from gates import Finding, GateReport, GateResult


class TestFindingParse(unittest.TestCase):

    def test_full_shape(self):
        f = Finding.parse(
            "[NW-PCI-003] SQLInstance/payments-ledger-db: backupRetentionDays "
            "is 7, PCI requires 30 -> set spec.parameters.backupRetentionDays to 30")
        self.assertEqual(f.policy_id, "NW-PCI-003")
        self.assertEqual(f.subject, "SQLInstance/payments-ledger-db")
        self.assertEqual(f.remediation,
                         "set spec.parameters.backupRetentionDays to 30")
        self.assertEqual(f.severity, "deny")

    def test_message_without_a_policy_id_is_flagged_not_dropped(self):
        f = Finding.parse("something went wrong somewhere")
        self.assertEqual(f.policy_id, "UNCLASSIFIED")
        self.assertEqual(f.message, "something went wrong somewhere")

    def test_message_without_a_remediation(self):
        f = Finding.parse("[NW-K8S-002] Deployment/x: no resource limits")
        self.assertEqual(f.remediation, "")
        self.assertEqual(f.subject, "Deployment/x")

    def test_multiline_message_survives(self):
        f = Finding.parse("[NW-K8S-001] Deployment/x: mutable tag\n  -> pin a digest")
        self.assertEqual(f.policy_id, "NW-K8S-001")
        self.assertIn("pin a digest", f.remediation)

    def test_warning_severity_is_carried(self):
        f = Finding.parse("[NW-FIN-006] x: y", severity="warn", tool="conftest")
        self.assertEqual(f.severity, "warn")

    def test_as_dict_is_stable(self):
        f = Finding.parse("[A-1] s: m -> r")
        self.assertEqual(set(f.as_dict()), {
            "policyId", "severity", "tool", "subject", "message", "remediation"})


class TestGateReport(unittest.TestCase):

    def _report(self):
        return GateReport(results=[
            GateResult(name="a", passed=False, findings=[
                Finding.parse("[NW-K8S-002] x: y"),
                Finding.parse("[NW-K8S-002] z: y"),
                Finding.parse("[NW-PCI-001] q: y"),
            ]),
            GateResult(name="b", passed=True, warnings=[
                Finding.parse("[NW-FIN-006] w: y", severity="warn")]),
        ])

    def test_counts_and_grouping(self):
        r = self._report()
        self.assertFalse(r.passed)
        self.assertEqual(len(r.findings), 3)
        self.assertEqual(len(r.warnings), 1)
        self.assertEqual(r.by_policy(), {"NW-K8S-002": 2, "NW-PCI-001": 1})

    def test_by_policy_is_sorted(self):
        self.assertEqual(list(self._report().by_policy()), ["NW-K8S-002", "NW-PCI-001"])

    def test_all_passing_is_passing(self):
        r = GateReport(results=[GateResult(name="a", passed=True)])
        self.assertTrue(r.passed)
        self.assertEqual(r.by_policy(), {})

    def test_a_skipped_gate_does_not_silently_pass_the_build(self):
        """A skipped gate reports passed=True so a missing optional scanner does
        not fail the run -- but it must still be visibly skipped in the record,
        or 'all green' stops meaning anything."""
        g = GateResult(name="trivy", passed=True, skipped=True, skip_reason="not found")
        self.assertTrue(g.as_dict()["skipped"])
        self.assertTrue(g.as_dict()["skipReason"])


class TestRealBundle(unittest.TestCase):
    """A handful of assertions against the actual conftest binary, so the Rego
    itself is covered and not only the Python around it."""

    @classmethod
    def setUpClass(cls):
        if gates._tool("conftest") is None:
            raise unittest.SkipTest("conftest not installed; run ./bin/setup.sh")

    def test_compliant_database_passes_the_pci_bundle(self):
        good = {
            "apiVersion": "platform.northwind.io/v1alpha1", "kind": "SQLInstance",
            "metadata": {"name": "ok", "labels": {
                "northwind.io/data-classification": "pci",
                "northwind.io/data-residency": "eu"}},
            "spec": {"parameters": {"storageEncrypted": True,
                                    "backupRetentionDays": 30,
                                    "publiclyAccessible": False,
                                    "region": "eu-west-1"}},
        }
        ids = {f.policy_id for f in gates.evaluate_kubernetes([good]).findings}
        self.assertNotIn("NW-PCI-003", ids)
        self.assertNotIn("NW-PCI-004", ids)

    def test_every_denial_carries_a_remediation(self):
        """The agent maps denials to input changes by reading the text after
        '->'. A rule that omits it is unactionable, so the bundle is held to
        the convention."""
        bad = {
            "apiVersion": "platform.northwind.io/v1alpha1", "kind": "SQLInstance",
            "metadata": {"name": "bad", "labels": {
                "northwind.io/data-classification": "pci",
                "northwind.io/data-residency": "eu"}},
            "spec": {"parameters": {"storageEncrypted": False,
                                    "backupRetentionDays": 1,
                                    "publiclyAccessible": True,
                                    "region": "us-east-1"}},
        }
        findings = gates.evaluate_kubernetes([bad]).findings
        self.assertTrue(findings)
        missing = [f.message for f in findings if not f.remediation]
        self.assertEqual(missing, [], "denials with no '-> remediation'")

    def test_every_denial_carries_a_policy_id(self):
        bad = {"apiVersion": "apps/v1", "kind": "Deployment",
               "metadata": {"name": "bad"},
               "spec": {"replicas": 1, "template": {"spec": {"containers": [
                   {"name": "c", "image": "nginx:latest"}]}}}}
        findings = gates.evaluate_kubernetes([bad]).findings
        self.assertTrue(findings)
        self.assertNotIn("UNCLASSIFIED", {f.policy_id for f in findings})


if __name__ == "__main__":
    unittest.main()
