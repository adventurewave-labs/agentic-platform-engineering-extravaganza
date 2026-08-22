#!/usr/bin/env python3
"""
Unit tests for the day-2 drift agent and the Argo CD adapter.

The adapter is tested without an Argo CD: what needs pinning is the
normalisation -- that a `managed-resources` payload becomes the same shape the
fixture has, that a NetworkPolicy present in git and absent from the cluster is
recorded as an absence rather than as a missing key, and that attribution
reports a field manager rather than inventing a person.
"""

import json
import unittest

import context  # noqa: F401

import driftd


class TestDetect(unittest.TestCase):

    def test_the_fixture_drifts_the_way_the_readme_says(self):
        r = driftd.detect("payments-ledger", "prod")
        self.assertTrue(r["observed"])
        self.assertEqual(r["syncStatus"], "OutOfSync")
        self.assertEqual(r["driftCount"], 4)
        self.assertEqual(r["highestSeverity"], "critical")

    def test_findings_are_ordered_most_severe_first(self):
        drift = driftd.detect("payments-ledger", "prod")["drift"]
        order = [driftd.SEVERITY_ORDER[d["severity"]] for d in drift]
        self.assertEqual(order, sorted(order))

    def test_a_synced_workload_reports_no_drift(self):
        r = driftd.detect("payments-gateway", "prod")
        self.assertEqual(r["syncStatus"], "Synced")
        self.assertEqual(r["drift"], [])

    def test_an_unknown_workload_says_so_rather_than_claiming_clean(self):
        """The dangerous failure is reporting 'no drift' for something never
        observed. It must be distinguishable from a genuinely clean workload."""
        r = driftd.detect("does-not-exist", "prod")
        self.assertFalse(r["observed"])
        self.assertIn("no observed state", r["note"])
        self.assertNotIn("syncStatus", r)

    def test_every_drift_field_carries_a_remediation(self):
        for d in driftd.detect("payments-ledger", "prod")["drift"]:
            self.assertTrue(d["remediation"], d["field"])


class TestRemediationPatch(unittest.TestCase):

    def test_the_patch_restores_desired_state_and_needs_a_human(self):
        report = driftd.detect("payments-ledger", "prod")
        patch = driftd.remediation_patch("payments-ledger", "prod", report)
        self.assertTrue(patch["requiresHumanApproval"])
        self.assertEqual(patch["patch"]["replicas"], 6)
        self.assertEqual(patch["patch"]["backupRetentionDays"], 30)
        self.assertEqual(patch["patch"]["networkPolicy"],
                         "payments-ledger-default-deny")

    def test_the_patch_is_a_branch_not_an_apply(self):
        """The whole argument of the drift act: the agent proposes a diff a
        human merges. If a field named like an apply ever appears here, the
        claim in the README has quietly stopped being true."""
        patch = driftd.remediation_patch(
            "payments-ledger", "prod", driftd.detect("payments-ledger", "prod"))
        self.assertTrue(patch["branch"].startswith("drift/"))
        self.assertFalse({"apply", "kubeconfig", "cluster"} & set(patch))

    def test_rationale_names_who_and_how_for_each_field(self):
        patch = driftd.remediation_patch(
            "payments-ledger", "prod", driftd.detect("payments-ledger", "prod"))
        self.assertEqual(len(patch["rationale"]), 4)
        for line in patch["rationale"]:
            self.assertIn("changed by", line)
            self.assertIn("via", line)


# --- the Argo CD adapter, without an Argo CD ---------------------------------

DEPLOY_TARGET = {
    "kind": "Deployment",
    "metadata": {"name": "payments-ledger"},
    "spec": {"replicas": 6, "template": {"spec": {
        "automountServiceAccountToken": False,
        "containers": [{"name": "app", "image": "ghcr.io/n/pl:1.4.2",
                        "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}}}]}}},
}
DEPLOY_LIVE = {
    "kind": "Deployment",
    "metadata": {"name": "payments-ledger", "managedFields": [
        {"manager": "argocd-controller", "time": "2026-07-23T11:04:19Z",
         "fieldsV1": {"f:spec": {"f:template": {}}}},
        {"manager": "kubectl-scale", "time": "2026-07-29T02:14:33Z",
         "fieldsV1": {"f:spec": {"f:replicas": {}}}},
    ]},
    "spec": {"replicas": 18, "template": {"spec": {
        "automountServiceAccountToken": True,
        "containers": [{"name": "app", "image": "ghcr.io/n/pl:1.4.2",
                        "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}}}]}}},
}
NETPOL_TARGET = {"kind": "NetworkPolicy",
                 "metadata": {"name": "payments-ledger-default-deny"}}
SQL_TARGET = {"kind": "SQLInstance", "metadata": {"name": "db"},
              "spec": {"parameters": {"backupRetentionDays": 30}}}
SQL_LIVE = {"kind": "SQLInstance", "metadata": {"name": "db"},
            "spec": {"parameters": {"backupRetentionDays": 7}}}


class TestArgoCDAdapter(unittest.TestCase):

    def setUp(self):
        from sources import argocd
        self.argocd = argocd
        self._real_get = argocd._get

        def fake_get(path, params=None, timeout=20):
            if path.endswith("/managed-resources"):
                return {"items": [
                    {"targetState": json.dumps(DEPLOY_TARGET),
                     "liveState": json.dumps(DEPLOY_LIVE)},
                    {"targetState": json.dumps(NETPOL_TARGET), "liveState": None},
                    {"targetState": json.dumps(SQL_TARGET),
                     "liveState": json.dumps(SQL_LIVE)},
                ]}
            return {"status": {"operationState": {"finishedAt": "2026-07-23T11:04:19Z"}}}

        argocd._get = fake_get

    def tearDown(self):
        self.argocd._get = self._real_get

    def _workload(self):
        state = self.argocd.fetch_observed_state(
            "payments-ledger-prod", "payments-ledger", "prod")
        return state["payments-ledger/prod"]

    def test_it_produces_the_fixture_shape(self):
        w = self._workload()
        self.assertEqual(set(w), {"lastReconciled", "desired", "observed", "attribution"})

    def test_desired_and_observed_are_read_from_target_and_live(self):
        w = self._workload()
        self.assertEqual(w["desired"]["replicas"], 6)
        self.assertEqual(w["observed"]["replicas"], 18)
        self.assertEqual(w["desired"]["backupRetentionDays"], 30)
        self.assertEqual(w["observed"]["backupRetentionDays"], 7)

    def test_a_policy_present_in_git_and_absent_from_the_cluster_is_an_absence(self):
        """A missing key and a deleted NetworkPolicy are very different
        findings. Recording the deletion as `None` is what makes the drift
        agent report it instead of skipping it."""
        w = self._workload()
        self.assertIn("networkPolicy", w["observed"])
        self.assertIsNone(w["observed"]["networkPolicy"])

    def test_attribution_names_a_field_manager_not_a_person(self):
        w = self._workload()
        replicas = w["attribution"]["replicas"]
        self.assertTrue(replicas["changedBy"].startswith("field-manager:"))
        self.assertIn("kubectl-scale", replicas["changedBy"])
        self.assertEqual(replicas["changedAt"], "2026-07-29T02:14:33Z")
        self.assertIn("out of band", replicas["via"])

    def test_severity_comes_from_the_policy_not_a_guess(self):
        w = self._workload()
        self.assertEqual(w["attribution"]["backupRetentionDays"]["severity"], "critical")
        self.assertEqual(w["attribution"]["backupRetentionDays"]["policyId"], "NW-PCI-003")
        self.assertEqual(w["attribution"]["replicas"]["severity"], "high")

    def test_only_drifted_fields_are_attributed(self):
        w = self._workload()
        self.assertNotIn("image", w["attribution"])
        self.assertNotIn("cpuLimit", w["attribution"])

    def test_the_verdict_layer_consumes_it_unchanged(self):
        """The point of the adapter: driftd does not know or care which source
        produced the state."""
        import os
        os.environ["NORTHWIND_DRIFT_SOURCE"] = "argocd"
        os.environ["ARGOCD_SERVER"] = "https://argocd.invalid"
        os.environ["ARGOCD_TOKEN"] = "t"
        try:
            report = driftd.detect("payments-ledger", "prod")
        finally:
            for k in ("NORTHWIND_DRIFT_SOURCE", "ARGOCD_SERVER", "ARGOCD_TOKEN"):
                os.environ.pop(k, None)
        self.assertEqual(report["syncStatus"], "OutOfSync")
        fields = {d["field"] for d in report["drift"]}
        self.assertEqual(
            fields, {"replicas", "backupRetentionDays", "networkPolicy",
                     "automountServiceAccountToken"})

    def test_an_unreachable_argocd_falls_back_loudly(self):
        import os
        self.argocd._get = self._real_get  # let it actually try, and fail
        os.environ["NORTHWIND_DRIFT_SOURCE"] = "argocd"
        os.environ.pop("ARGOCD_SERVER", None)
        try:
            report = driftd.detect("payments-ledger", "prod")
        finally:
            os.environ.pop("NORTHWIND_DRIFT_SOURCE", None)
        # Falls back to the fixture rather than reporting a clean estate.
        self.assertTrue(report["observed"])
        self.assertEqual(report["driftCount"], 4)


if __name__ == "__main__":
    unittest.main()
