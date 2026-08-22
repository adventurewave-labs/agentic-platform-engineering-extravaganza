#!/usr/bin/env python3
"""
Unit tests for the gate layer: finding parsing and report aggregation.

`Finding.parse` is the seam between a Rego message and everything the agent
does with it. The whole remediation loop depends on the `[POLICY-ID] subject:
what is wrong -> how to fix it` convention holding, so it is tested directly
rather than only through the system run.
"""

import unittest
from pathlib import Path

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


class TestEngineFailureIsNotAPass(unittest.TestCase):
    """The gate must never report "clean" because the engine never ran.

    conftest exits non-zero both for a policy denial and for a bundle that will
    not compile, and it writes compile errors to stderr -- leaving stdout empty.
    A wrapper that reads only stdout sees no findings and calls that a pass.
    This is the regression test for that: an uncompilable rule is dropped into
    the live bundle and the same manifest that earns 15 denials with the bundle
    intact must not come back clean.
    """

    HOSTILE = [{
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "hostile"},
        "spec": {"template": {"spec": {
            "hostNetwork": True,
            "containers": [{
                "name": "c", "image": "nginx:latest",
                "securityContext": {"privileged": True, "runAsUser": 0},
            }],
        }}},
    }]

    @classmethod
    def setUpClass(cls):
        if gates._tool("conftest") is None:
            raise unittest.SkipTest("conftest not installed; run ./bin/setup.sh")

    def test_the_hostile_manifest_is_denied_while_the_bundle_compiles(self):
        """Guards the guard: if this stops denying, the test below proves nothing."""
        result = gates.evaluate_kubernetes(self.HOSTILE)
        self.assertFalse(result.passed)
        self.assertGreaterEqual(len(result.findings), 5)

    def test_an_uncompilable_bundle_raises_instead_of_passing(self):
        broken = gates.POLICY / "kubernetes" / "zzz_broken_fixture.rego"
        broken.write_text("package main\ndeny[msg] { this is not valid rego ((\n")
        try:
            with self.assertRaises(gates.PolicyEngineError) as caught:
                gates.evaluate_kubernetes(self.HOSTILE)
        finally:
            broken.unlink(missing_ok=True)
        self.assertIn("without a result array", str(caught.exception))

    def test_the_bundle_is_left_compiling_afterwards(self):
        """The fixture above writes into the real policy directory. If its
        cleanup ever regresses, every later run would be evaluating a broken
        bundle -- so the removal is asserted, not assumed."""
        self.assertFalse((gates.POLICY / "kubernetes" / "zzz_broken_fixture.rego").exists())
        self.assertFalse(gates.evaluate_kubernetes(self.HOSTILE).passed)

    def test_a_crashed_kube_linter_raises_instead_of_passing(self):
        """kube-linter shares conftest's exit-code ambiguity: 1 means both
        "found 8 lint errors" and "could not read that path". A stub that exits
        the way a crash does must not be read as a clean lint."""
        import subprocess as sp
        import tempfile as tf

        with tf.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "kube-linter"
            stub.write_text(
                "#!/bin/sh\necho 'Error: loading from path: no such file' >&2\nexit 1\n")
            stub.chmod(0o755)
            real = gates._tool
            gates._tool = lambda n: str(stub) if n == "kube-linter" else real(n)
            try:
                with self.assertRaises(gates.PolicyEngineError) as caught:
                    gates.kube_linter(self.HOSTILE)
            finally:
                gates._tool = real
        self.assertIn("without a report object", str(caught.exception))
