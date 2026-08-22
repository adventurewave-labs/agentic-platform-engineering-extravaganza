#!/usr/bin/env python3
"""
The acceptance checks are themselves checked: each one is broken on purpose.

WHY THIS EXISTS
---------------
An audit found three checks in `build_report.py` that could not fail.

  * T02 claimed the Rego bundles compiled. It ran
    `conftest test --policy policy /dev/null`, which dies on parser selection
    before it loads any Rego, so the string it grepped for could never appear.
    It reported success against a bundle with a syntax error in it.
  * T06 claimed the agent only ever changes inputs, never rendered output. It
    inspected `{d.field for d in decisions}` -- the agent's own account of what
    it had changed -- and never looked at a rendered document. A remediation
    step that stamped every object still passed.
  * T04 claimed the unguided manifests were rejected. It asserted `>= 20`
    findings against an actual figure of 42, so half the policy set could stop
    firing without it noticing.

A green scorecard is a claim about the system. A check that cannot go red is
not evidence for that claim, and the failure is invisible precisely because
everything looks fine. So every check whose failure mode matters now has the
thing it watches broken underneath it here, and is required to say so.

These tests mutate module-level state and the live policy directory. Each
restores what it touched in a `finally`, and `test_docs.py` re-runs the real
bundle afterwards, so a leak would surface rather than silently weaken the
suite.
"""

import copy
import tempfile
import unittest
from pathlib import Path

import context  # noqa: F401

import agent
import build_report
import gates
import platform_mcp
import renderer

POLICY = Path(gates.POLICY)


class ScorecardCase(unittest.TestCase):

    def assertCheckFails(self, fn, because):
        passed, detail = fn()
        self.assertFalse(
            passed,
            f"{fn.__name__} still reported PASS while {because}. "
            f"It said: {detail!r}")
        return detail

    def assertCheckPasses(self, fn):
        passed, detail = fn()
        self.assertTrue(passed, f"{fn.__name__} failed unexpectedly: {detail!r}")
        return detail


class TestT02NoticesABrokenBundle(ScorecardCase):

    def test_a_syntax_error_in_the_bundle_turns_the_check_red(self):
        broken = POLICY / "kubernetes" / "zzz_scorecard_fixture.rego"
        broken.write_text("package main\ndeny[msg] { this is not valid rego ((\n")
        try:
            detail = self.assertCheckFails(
                build_report.c_rego_compiles,
                "policy/kubernetes contained a rule that does not parse")
        finally:
            broken.unlink(missing_ok=True)
        self.assertIn("opa check failed", detail)

    def test_it_passes_again_once_the_bundle_is_whole(self):
        self.assertCheckPasses(build_report.c_rego_compiles)


class TestT04NoticesPoliciesGoingQuiet(ScorecardCase):

    def test_half_the_denials_disappearing_turns_the_check_red(self):
        real = gates.run_all

        class Half:
            """Everything still runs; only half the verdicts survive."""

            def __init__(self, report):
                self._report = report

            @property
            def findings(self):
                return self._report.findings[:21]

            def by_policy(self):
                from collections import Counter
                return dict(Counter(f.policy_id for f in self.findings))

        gates.run_all = lambda docs, **kw: Half(real(docs, **kw))
        try:
            detail = self.assertCheckFails(
                build_report.c_unguided_denied,
                "only half the policy denials were being reported")
        finally:
            gates.run_all = real
        self.assertIn("run record says", detail)


class TestT06NoticesTheAgentTouchingOutput(ScorecardCase):
    """The three ways an agent could edit output while reporting that it did not."""

    def setUp(self):
        self.real_remediate = agent.remediate
        self.real_golden_path = renderer.golden_path

    def tearDown(self):
        agent.remediate = self.real_remediate
        renderer.golden_path = self.real_golden_path

    def test_mutating_the_documents_it_was_shown(self):
        seen = []
        real_gp = self.real_golden_path

        def spy(req, env, *a, **kw):
            spec, docs = real_gp(req, env, *a, **kw)
            seen.clear()
            seen.extend(docs)
            return spec, docs

        real_rem = self.real_remediate

        def tampering(req, findings, env):
            for doc in seen:
                doc.setdefault("metadata", {}).setdefault(
                    "annotations", {})["tampered-by-agent"] = "yes"
            return real_rem(req, findings, env)

        renderer.golden_path = spy
        agent.remediate = tampering
        detail = self.assertCheckFails(
            build_report.c_no_output_patching,
            "remediation stamped an annotation onto every rendered object")
        self.assertIn("mutated the rendered documents", detail)

    def test_changing_an_input_without_declaring_it(self):
        """The field is a legitimate input; the silence about it is not."""
        real_rem = self.real_remediate

        def smuggling(req, findings, env):
            updated, decisions = real_rem(req, findings, env)
            updated.description += " <!-- injected by the agent -->"
            return updated, decisions

        agent.remediate = smuggling
        detail = self.assertCheckFails(
            build_report.c_no_output_patching,
            "remediation changed the description without declaring it")
        self.assertIn("undeclared: description", detail)

    def test_reporting_a_change_it_did_not_make(self):
        """The mirror image: a decision record with nothing behind it."""
        real_rem = self.real_remediate

        def boasting(req, findings, env):
            updated, decisions = real_rem(req, findings, env)
            decisions = list(decisions) + [agent.Decision(
                policy_id="NW-PCI-999", field="db_multi_az",
                before=False, after=True, rationale="claimed, never applied")]
            return updated, decisions

        agent.remediate = boasting
        detail = self.assertCheckFails(
            build_report.c_no_output_patching,
            "remediation reported a change it never made")
        self.assertIn("claimed but unchanged: db_multi_az", detail)

    def test_it_passes_when_the_agent_behaves(self):
        self.assertCheckPasses(build_report.c_no_output_patching)


class TestT14NoticesASilentManifestChange(ScorecardCase):

    def test_a_field_no_recorded_number_covers_still_turns_it_red(self):
        """A label nobody thought to assert. Every scalar in the run record
        still matches; only the digest moves."""
        real_gp = renderer.golden_path

        def poisoned(req, env, *a, **kw):
            spec, docs = real_gp(req, env, *a, **kw)
            for doc in docs:
                doc.setdefault("metadata", {}).setdefault(
                    "labels", {})["unnoticed"] = "1"
            return spec, docs

        renderer.golden_path = poisoned
        try:
            detail = self.assertCheckFails(
                build_report.c_reproducible,
                "every rendered object had gained a label")
        finally:
            renderer.golden_path = real_gp
        self.assertIn("differ byte for byte", detail)


class TestAuthzChecksNoticeAnOpenDoor(ScorecardCase):

    def test_t10_notices_every_identity_seeing_every_tool(self):
        real = platform_mcp.Session.visible_tools
        platform_mcp.Session.visible_tools = lambda self: list(platform_mcp.TOOLS.values())
        try:
            detail = self.assertCheckFails(
                build_report.c_identity_scoping,
                "every identity could see every tool")
        finally:
            platform_mcp.Session.visible_tools = real
        self.assertIn("not ordered by privilege", detail)

    def test_t12_notices_an_open_origin_guard(self):
        real = platform_mcp._origin_allowed
        platform_mcp._origin_allowed = lambda origin: True
        try:
            detail = self.assertCheckFails(
                build_report.c_mcp_origin,
                "the origin guard accepted every origin")
        finally:
            platform_mcp._origin_allowed = real
        self.assertIn("ACCEPTED", detail)

    def test_t08_notices_the_agent_being_handed_the_approval_tool(self):
        real = platform_mcp.Session.visible_tools
        platform_mcp.Session.visible_tools = lambda self: list(platform_mcp.TOOLS.values())
        try:
            self.assertCheckFails(
                build_report.c_authz_agent_denied,
                "approve_promotion was listed to the agent")
        finally:
            platform_mcp.Session.visible_tools = real


if __name__ == "__main__":
    unittest.main()
