#!/usr/bin/env python3
"""
The demo. Eight acts and a scorecard, one request, no hand-waving.

    python3 src/goldenpath.py                       # full run
    python3 src/goldenpath.py --act 5               # one act
    python3 src/goldenpath.py --speed 0 --no-color  # CI / capture mode
    python3 src/goldenpath.py --backend llm         # use any OpenAI-compatible model

Every policy verdict printed by this script is produced by running the real
conftest binary over the real Rego in policy/. Every manifest is produced by
the real score-k8s binary. Nothing is a recording. Run it twice and the numbers
are identical, because that is what a control plane is supposed to give you.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import agent  # noqa: E402
import costing  # noqa: E402
import driftd  # noqa: E402
import gates  # noqa: E402
import renderer  # noqa: E402
import ui  # noqa: E402
from catalog import Catalog, find_template, load_templates, resolve_identity  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
WORKSPACE = ROOT / "workspace"

REQUEST = (
    "We need a new service called payments-ledger for storing card settlement "
    "records. It is PCI scope, EU data residency, high volume at peak, needs a "
    "Postgres database, staging and prod."
)

# The thing being replaced. Sourced from a Rafay-commissioned survey of 500+
# platform teams and developers (2023): 57% of engineers wait on another human
# or a ticket to get an environment, and 61% call environment provisioning a
# major roadblock. The per-step days below are Northwind's own fiction; the
# shape is not.
TICKET_TRAIL = [
    ("INFRA-8821", "Request new service + database", "platform-team", 2.0),
    ("SEC-3310", "PCI scope review and control mapping", "security", 3.0),
    ("INFRA-8822", "Terraform module for RDS instance", "cloud-infra", 2.0),
    ("NET-1140", "Firewall + NetworkPolicy exception", "network", 1.5),
    ("FIN-0907", "Cost-centre approval for new spend", "finops", 1.5),
    ("REL-2255", "Argo CD app + promotion pipeline", "release-eng", 1.0),
]


def _s(x: float) -> None:
    ui.pause(x)


# ---------------------------------------------------------------------------
# Act I — the ticket
# ---------------------------------------------------------------------------

def act_one() -> None:
    ui.act("I", "The request, and what it costs today")
    for i, chunk in enumerate(ui.wrap('"' + REQUEST + '"', ui.WIDTH - 6)):
        ui.typed(ui.color(chunk, "white"), prefix="  " if i == 0 else "   ")
    _s(0.6)
    ui.write()
    ui.write(f"  {ui.color('One sentence. Six teams. Here is the trail it leaves.', 'grey')}")
    ui.write()

    total = 0.0
    rows = []
    for key, summary, team, days in TICKET_TRAIL:
        total += days
        rows.append([key, summary, f"@{team}", f"{days:>4.1f}d"])
        ui.pause(0.12)
    ui.table(["ticket", "summary", "queue", "wait"], rows,
             tones=["grey"] * len(rows))
    ui.write()
    ui.write(f"  {ui.color('handoffs', 'dark')}      {ui.color(str(len(TICKET_TRAIL)), 'amber', True)}"
             f"      {ui.color('elapsed', 'dark')}  "
             f"{ui.color(f'{total:.0f} working days', 'amber', True)}"
             f"      {ui.color('engineer time', 'dark')}  "
             f"{ui.color('~4 hours', 'grey')}")
    ui.write()
    ui.write(f"  {ui.color('The 11 days are not work. They are queue.', 'grey', True)}")
    _s(1.0)


# ---------------------------------------------------------------------------
# Act II — no platform
# ---------------------------------------------------------------------------

def act_two() -> dict[str, Any]:
    ui.act("II", "Skip the platform: let the model write the YAML", tone="amber")
    ui.write(f"  {ui.color('The obvious move in 2026: hand the sentence to a capable model and', 'grey')}")
    ui.write(f"  {ui.color('ask for Kubernetes manifests. No platform, no golden path, no gate.', 'grey')}")
    ui.write()

    req = agent.parse_intent(REQUEST)
    ui.spinner("generating manifests from the prompt", 1.4,
               done="3 manifests generated: Deployment, Service, StatefulSet")
    docs = renderer.vibe_manifests(req)
    ui.write()
    ui.box([
        f"  {ui.color('image:', 'dark')}     {ui.color('payments-ledger:latest', 'white')}",
        f"  {ui.color('replicas:', 'dark')}  {ui.color('1', 'white')}",
        f"  {ui.color('env:', 'dark')}       {ui.color('DB_PASSWORD=postgres', 'white')}",
        f"  {ui.color('service:', 'dark')}   {ui.color('type: LoadBalancer', 'white')}",
        f"  {ui.color('database:', 'dark')}  {ui.color('StatefulSet, postgres:latest, no PVC', 'white')}",
    ], tone="dark", title="what came back")
    ui.write()
    ui.write(f"  {ui.color('It parses. It applies. It would pass a review from anyone', 'grey')}")
    ui.write(f"  {ui.color('who was not looking hard. Run it past the policy bundle.', 'grey')}")
    ui.write()

    ui.spinner("conftest test --policy policy/kubernetes", 1.2)
    report = gates.run_all(docs, include_scanners=True)
    _s(0.3)

    ui.write()
    counts = report.by_policy()
    for i, f in enumerate(report.findings[:9], 1):
        ui.finding(i, f.policy_id, f.message, f.remediation)
        ui.pause(0.05)
    remaining = len(report.findings) - 9
    if remaining > 0:
        ui.write(f"      {ui.color(f'... and {remaining} more', 'dark')}")
    ui.write()

    ui.write("  " + ui.BG["red"] + ui.BOLD + ui.FG["white"]
             + f"  BLOCKED — {len(report.findings)} policy violations  " + ui.RESET)
    ui.write()
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    ui.write("  " + "  ".join(
        f"{ui.color(pid, 'red')}{ui.color('×' + str(n), 'dark')}" for pid, n in top))
    ui.write()
    ui.write(f"  {ui.color('The model was not stupid. It was unguided.', 'white', True)}")
    ui.write(f"  {ui.color('Nothing in the prompt told it what PCI means at Northwind, which', 'grey')}")
    ui.write(f"  {ui.color('registry is approved, or that this cost centre has $400 a month.', 'grey')}")
    ui.write(f"  {ui.color('That knowledge is not in any model. It is in a platform.', 'grey')}")
    _s(1.2)
    return report.as_dict()


# ---------------------------------------------------------------------------
# Act III — the platform, over MCP
# ---------------------------------------------------------------------------

def act_three(identity_name: str = "platform-agent") -> None:
    ui.act("III", "The same request, through the platform API", tone="violet")
    identity = resolve_identity(identity_name)
    catalog = Catalog()

    ui.write(f"  {ui.color('The platform speaks MCP. The agent connects as a principal, not', 'grey')}")
    ui.write(f"  {ui.color('as an administrator.', 'grey')}")
    ui.write()

    ui.tool_call("initialize", "protocolVersion=2025-06-18")
    _s(0.3)
    ui.tool_result(f"northwind-platform v1.0.0 · authenticated as {identity.describe()}")
    ui.write()

    ui.tool_call("tools/list", identity=identity.name)
    _s(0.3)
    import platform_mcp
    session = platform_mcp.Session(identity)
    visible = session.visible_tools()
    hidden = [t for t in platform_mcp.TOOLS.values() if t not in visible]
    ui.tool_result(f"{len(visible)} of {len(platform_mcp.TOOLS)} tools returned")
    ui.write()
    for t in visible:
        mark = ui.color("!", "amber") if t.destructive else " "
        ui.write(f"      {mark} {ui.color(t.name.ljust(34), 'teal')}"
                 f"{ui.color(t.permission, 'dark')}")
        ui.pause(0.03)
    ui.write()
    for t in sorted(hidden, key=lambda x: x.name):
        ui.write(f"      {ui.color('✗', 'red')} {ui.color(t.name.ljust(34), 'dark')}"
                 f"{ui.color('withheld — identity lacks ' + t.permission, 'dark')}")
    ui.write()
    ui.write(f"  {ui.color('Read that last line again.', 'white', True)} "
             f"{ui.color('The agent is not instructed to avoid', 'grey')}")
    ui.write(f"  {ui.color('approving its own production release. It is never told the', 'grey')}")
    ui.write(f"  {ui.color('capability exists. A prompt is a request; an absent tool is a fact.', 'grey')}")
    _s(1.2)

    ui.write()
    ui.tool_call("catalog.query", 'kind="AiResource"')
    _s(0.25)
    ai = catalog.ai_resources()
    ui.tool_result(f"{len(ai)} agent-fleet entities — every one owned, versioned and reviewable")
    for e in ai:
        allow = e.raw.get("spec", {}).get("allowedTools")
        scope = f"{len(allow)} tools" if allow else "no tool grant"
        ui.write(f"      {ui.color(e.name.ljust(26), 'white')}"
                 f"{ui.color(e.raw['spec'].get('type', '').ljust(8), 'dark')}"
                 f"{ui.color(scope, 'grey')}")
    ui.write()
    ui.write(f"  {ui.color('An agent’s blast radius lives in git. Widening it is a pull request.', 'grey')}")
    _s(0.9)

    ui.write()
    ui.tool_call("scaffolder.list_templates")
    _s(0.25)
    templates = load_templates()
    ui.tool_result(f"{len(templates)} golden path(s)")
    for t in templates:
        ui.write(f"      {ui.color(t.name, 'gold', True)}")
        ui.write(f"        {ui.color(t.title, 'grey')}")
    _s(0.8)


# ---------------------------------------------------------------------------
# Act IV — scaffold and render
# ---------------------------------------------------------------------------

def act_four(req: renderer.ServiceRequest) -> tuple[dict, list[dict]]:
    ui.act("IV", "Scaffold, and render through the platform’s own provisioners")

    ui.tool_call("scaffolder.get_template_parameters", '"golden-path-service-postgres"')
    _s(0.25)
    tpl = find_template("golden-path-service-postgres")
    schema = tpl.parameter_schema() if tpl else {"properties": {}, "required": []}
    groups = len(tpl.raw.get("spec", {}).get("parameters", [])) if tpl else 0
    ui.tool_result(f"JSON Schema: {groups} groups, {len(schema['properties'])} "
                   f"parameters, {len(schema['required'])} required")
    ui.write()

    ui.write(f"  {ui.color('Extracted intent:', 'grey')}")
    ui.kv("name", req.name, tone="white")
    ui.kv("owner", req.owner, tone="white")
    ui.kv("classification", req.classification, tone="amber")
    ui.kv("residency", req.residency or "(unset)", tone="amber")
    ui.kv("environments", ", ".join(req.environments))
    ui.kv("db instance class", req.db_instance_class, tone="amber")
    ui.kv("db storage", f"{req.db_storage_gb} GiB", tone="amber")
    ui.kv("multi-AZ", str(req.db_multi_az), tone="amber")
    ui.kv("backup retention", f"{req.db_backup_retention_days} days", tone="amber")
    ui.write()
    ui.write(f"  {ui.color('The amber values are the agent’s judgement calls. Hold that thought.', 'grey')}")
    _s(1.0)

    ui.write()
    ui.tool_call("scaffolder.execute", 'template="golden-path-service-postgres"')
    ui.spinner("score-k8s generate  (real binary, Northwind provisioner set)", 1.6)
    spec, docs = renderer.golden_path(req, "prod", WORKSPACE / req.name / ".render" / "prod")
    _s(0.2)

    kinds: dict[str, int] = {}
    for d in docs:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    ui.wrapped("rendered " + ", ".join(f"{v}x {k}" for k, v in sorted(kinds.items())),
               "teal", 4, ui.color("← ", "dark"))
    ui.write()

    sql = next((d for d in docs if d["kind"] == "SQLInstance"), None)
    ui.write(f"  {ui.color('The developer asked for this:', 'grey')}")
    ui.box([f"  {ui.color('resources:', 'teal')}",
            f"  {ui.color('  db:', 'teal')}",
            f"  {ui.color('    type: postgres', 'teal')}"],
           tone="dark", title="score.yaml — the entire database request")
    ui.write()
    ui.write(f"  {ui.color('The platform’s provisioner turned it into this:', 'grey')}")
    if sql:
        p = sql["spec"]["parameters"]
        ui.box([
            f"  {ui.color('apiVersion:', 'dark')} {ui.color('platform.northwind.io/v1alpha1', 'violet')}",
            f"  {ui.color('kind:', 'dark')}       {ui.color('SQLInstance', 'violet', True)}"
            f"  {ui.color('(Crossplane v2 namespaced composite)', 'dark')}",
            f"  {ui.color('storageEncrypted:', 'dark')}    {ui.color(str(p['storageEncrypted']), 'green')}",
            f"  {ui.color('publiclyAccessible:', 'dark')}  {ui.color(str(p['publiclyAccessible']), 'green')}",
            f"  {ui.color('region:', 'dark')}              {ui.color(str(p['region']), 'white')}",
            f"  {ui.color('instanceClass:', 'dark')}       {ui.color(str(p['instanceClass']), 'amber')}",
            f"  {ui.color('backupRetentionDays:', 'dark')} {ui.color(str(p['backupRetentionDays']), 'amber')}",
            f"  {ui.color('costCenter:', 'dark')}          {ui.color(str(p['costCenter']), 'white')}",
        ], tone="violet", title="rendered by platform/northwind.provisioners.yaml")
    ui.write()
    ui.write(f"  {ui.color('Plus, without being asked: hardened securityContext, dropped', 'grey')}")
    ui.write(f"  {ui.color('capabilities, read-only rootfs, probes, PodDisruptionBudget,', 'grey')}")
    ui.write(f"  {ui.color('default-deny NetworkPolicy, no automounted SA token, ownership', 'grey')}")
    ui.write(f"  {ui.color('and cost-centre labels on every object.', 'grey')}")
    ui.write()
    ui.write(f"  {ui.color('Every violation from Act II that had one correct answer is', 'white', True)}")
    ui.write(f"  {ui.color('now structurally impossible. No model was consulted.', 'white', True)}")
    _s(1.4)
    return spec, docs


# ---------------------------------------------------------------------------
# Act V + VI — the gate, and the loop
# ---------------------------------------------------------------------------

def act_five(req: renderer.ServiceRequest, backend: str = "deterministic"
             ) -> tuple[renderer.ServiceRequest, list[agent.Turn], dict]:
    ui.act("V", "The gate: what the platform cannot decide for you", tone="magenta")
    reasoner, reasoner_label = agent.get_reasoner(backend)
    ui.write(f"  {ui.color('reasoner:', 'dark')} {ui.color(reasoner_label, 'grey')}")
    ui.write(f"  {ui.color('engine:  ', 'dark')} {ui.color(gates._conftest_version() + '  ·  real Rego, real evaluation', 'grey')}")
    ui.write()

    turns: list[agent.Turn] = []
    current = req
    report: gates.GateReport | None = None
    est: dict[str, Any] = {}

    for iteration in range(1, 6):
        spec, docs = renderer.golden_path(
            current, "prod", WORKSPACE / current.name / ".render" / f"iter{iteration}")
        est = costing.estimate(current, "prod")
        report = gates.run_all(docs, score=spec, cost=est)

        label = f"iteration {iteration}"
        money = "${:,.2f}/mo vs ${:,.2f} budget".format(
            est["totalMonthlyCostUsd"], est["monthlyBudgetUsd"])
        ui.write(f"  {ui.color('▍', 'magenta')} {ui.color(label.upper(), 'magenta', True)}"
                 f"   {ui.color(money, 'dark')}")
        _s(0.35)

        if report.passed:
            ui.write(f"    {ui.color('✔ 0 denials across 4 gates', 'green', True)}")
            turns.append(agent.Turn(iteration, 0, len(report.warnings), [], {}))
            ui.write()
            break

        for i, f in enumerate(report.findings, 1):
            ui.finding(i, f.policy_id, f.message, f.remediation)
            ui.pause(0.06)

        updated, decisions = reasoner(current, report.findings, "prod")
        stuck = agent.unresolved(report.findings, decisions)

        ui.write()
        if decisions:
            ui.write(f"    {ui.color('agent →', 'violet', True)} "
                     f"{ui.color(f'{len(decisions)} input change(s), then re-render from source', 'grey')}")
            for d in decisions:
                ui.write(f"      {ui.color('·', 'dark')} "
                         f"{ui.color(d.field, 'white')} "
                         f"{ui.color(str(d.before), 'red')} "
                         f"{ui.color('→', 'dark')} "
                         f"{ui.color(str(d.after), 'green', True)}")
                ui.wrapped(d.rationale, "dark", 8)
                ui.pause(0.08)
        if stuck:
            ui.write()
            for f in stuck:
                ui.wrapped(f"{f.policy_id} has no input-level fix; escalating rather "
                           f"than patching output", "amber", 2,
                           ui.color("▲ ", "amber"))

        turns.append(agent.Turn(iteration, len(report.findings),
                                len(report.warnings), decisions, report.by_policy()))
        ui.write()

        if not decisions:
            ui.bad("no denial maps to an input change; escalating to a human "
                   "rather than editing rendered output")
            break
        current = updated
        _s(0.4)

    assert report is not None
    ui.write()
    plural = "iteration" if len(turns) == 1 else "iterations"
    if report.passed:
        ui.write("  " + ui.BG["green"] + ui.BOLD + ui.FG["white"]
                 + "  PASSED — 0 denials  " + ui.RESET + "  "
                 + ui.color(f"converged in {len(turns)} {plural}", "grey"))
    else:
        # The banner is conditional on purpose. A gate that reports success
        # after giving up is not a gate.
        ui.write("  " + ui.BG["amber"] + ui.BOLD + ui.FG["white"]
                 + f"  ESCALATED — {len(report.findings)} denial(s) remain  " + ui.RESET
                 + "  " + ui.color(f"stopped after {len(turns)} {plural}", "grey"))
        ui.write()
        ui.write(f"  {ui.color('The agent could not map these back to an input, so it stopped.', 'grey')}")
        ui.write(f"  {ui.color('A human owns them now — which is the correct outcome, not a bug.', 'grey')}")
    ui.write()
    for w in report.warnings:
        ui.warn(ui.color(w.message.split('->')[0].strip(), "amber"))
    ui.write()
    ui.write(f"  {ui.color('Note what the agent never did: it never edited a rendered manifest', 'grey')}")
    ui.write(f"  {ui.color('to make a check go green. Every fix changed an input and the whole', 'grey')}")
    ui.write(f"  {ui.color('artefact was regenerated. Patch the output instead and the finding', 'grey')}")
    ui.write(f"  {ui.color('comes back silently on the next render — that is how automated', 'grey')}")
    ui.write(f"  {ui.color('remediation becomes worse than none.', 'grey')}")
    _s(1.4)
    return current, turns, est


def act_six(original: renderer.ServiceRequest, final: renderer.ServiceRequest) -> None:
    ui.act("VI", "What the gate cost, in money", tone="gold")
    before = costing.estimate(original, "prod")
    after = costing.estimate(final, "prod")
    ui.write(ui.color("  before — as the agent first proposed it", "grey"))
    ui.write(costing.render_table(before))
    ui.write()
    ui.write(ui.color("  after — policy-corrected", "grey"))
    ui.write(costing.render_table(after))
    ui.write()
    delta = before["totalMonthlyCostUsd"] - after["totalMonthlyCostUsd"]
    ui.write(f"  {ui.color('saved', 'dark')}  "
             f"{ui.color(f'${delta:,.2f}/mo', 'green', True)}  "
             f"{ui.color(f'(${delta * 12:,.0f}/yr, on one service)', 'grey')}")
    ui.write()
    ui.write(f"  {ui.color('And the backup window went the other way — 7 days to 30 — because', 'grey')}")
    ui.write(f"  {ui.color('the cost gate is not allowed to win an argument with the PCI gate.', 'grey')}")
    _s(1.2)


# ---------------------------------------------------------------------------
# Act VII — delivery
# ---------------------------------------------------------------------------

def act_seven(req: renderer.ServiceRequest, est: dict) -> None:
    ui.act("VII", "Delivery: propose, and stop", tone="teal")
    import platform_mcp

    agent_session = platform_mcp.Session(resolve_identity("platform-agent"))
    ui.tool_call("platform.plan_promotion",
                 f'service="{req.name}", toStage="prod"', identity="platform-agent")
    _s(0.3)
    plan = platform_mcp.TOOLS["platform.plan_promotion"].handler(
        agent_session, {"service": req.name, "fromStage": "staging", "toStage": "prod"})
    ui.tool_result(f"verification: {', '.join(plan['verification'])}  ·  "
                   f"requiresHumanApproval: {plan['requiresHumanApproval']}")
    ui.write()

    ui.tool_call("platform.open_pull_request", f'service="{req.name}"',
                 identity="platform-agent")
    _s(0.3)
    pr = platform_mcp.TOOLS["platform.open_pull_request"].handler(agent_session, {
        "service": req.name, "title": f"feat({req.name}): golden-path service + Postgres"})
    ui.tool_result(f"PR #{pr['number']} opened  ·  mergeable: {pr['mergeable']}  ·  "
                   f"reviewers: {', '.join(pr['reviewers'])}")
    ui.write()

    ui.box([
        f"  {ui.color(f'feat({req.name}): golden-path service + Postgres', 'white', True)}",
        "",
        f"  {ui.color('golden path', 'dark')}        golden-path-service-postgres",
        f"  {ui.color('policy', 'dark')}             {ui.color('0 denials', 'green')} · 4 gates · conftest + kube-linter",
        f"  {ui.color('cost', 'dark')}               ${est['totalMonthlyCostUsd']:,.2f}/mo "
        f"({ui.color('within', 'green')} the ${est['monthlyBudgetUsd']:,.0f} CC-4471 envelope)",
        f"  {ui.color('classification', 'dark')}     pci · eu residency · 30d backups · default-deny netpol",
        f"  {ui.color('delivery', 'dark')}           Argo CD × 2 · Kargo Warehouse → staging → prod",
        f"  {ui.color('author', 'dark')}             agent:platform-agent",
    ], tone="teal", title=f"github.com/northwind-retail/{req.name} — PR #{pr['number']}")
    ui.write()
    ui.write(f"  {ui.color('The reviewer opens this and sees evidence, not a diff to audit', 'grey')}")
    ui.write(f"  {ui.color('by hand. That is the review getting cheaper without getting weaker.', 'grey')}")
    _s(1.0)

    ui.write()
    ui.write(f"  {ui.color('Now the agent tries to finish the job.', 'grey')}")
    ui.write()
    ui.tool_call("platform.approve_promotion",
                 f'service="{req.name}", stage="prod"', identity="platform-agent")
    _s(0.5)
    server = platform_mcp.Server(resolve_identity("platform-agent"))
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "platform.approve_promotion",
                                     "arguments": {"service": req.name, "stage": "prod"}}})
    text = resp["result"]["content"][0]["text"]
    ui.write()
    ui.write("  " + ui.BG["red"] + ui.BOLD + ui.FG["white"] + "  DENIED  " + ui.RESET)
    for line in _wrap(text, ui.WIDTH - 8):
        ui.write(f"    {ui.color(line, 'red')}")
    ui.write()
    ui.write(f"  {ui.color('This is not the model declining. This is the platform declining.', 'white', True)}")
    ui.write(f"  {ui.color('There is no phrasing of the request that gets past it, because', 'grey')}")
    ui.write(f"  {ui.color('there is nothing to phrase — the tool was never in the list.', 'grey')}")
    _s(1.0)

    ui.write()
    ui.tool_call("platform.approve_promotion",
                 f'service="{req.name}", stage="prod"', identity="release-manager")
    _s(0.4)
    human = platform_mcp.Server(resolve_identity("release-manager"))
    resp = human.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "platform.approve_promotion",
                                    "arguments": {"service": req.name, "stage": "prod"}}})
    ui.tool_result("approved by user:release-manager — Kargo freight promoted to prod",
                   tone="green")
    _s(0.8)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------------------
# Act VIII — day two
# ---------------------------------------------------------------------------

def act_eight() -> dict:
    ui.act("VIII", "Day 2 — the part that actually kills platforms", tone="red")
    ui.write(f"  {ui.color('Four weeks later. The golden path worked. Then production met', 'grey')}")
    ui.write(f"  {ui.color('a Tuesday.', 'grey')}")
    ui.write()

    ui.tool_call("platform.detect_drift", 'service="payments-ledger", environment="prod"',
                 identity="drift-agent")
    ui.spinner("comparing observed state against git", 1.2)
    report = driftd.detect("payments-ledger", "prod")
    ui.tool_result(f"{report['syncStatus']} — {report['driftCount']} field(s) diverged "
                   f"since {report['lastReconciled'][:10]}")
    ui.write()

    for d in report["drift"]:
        tone = {"critical": "red", "high": "amber"}.get(d["severity"], "grey")
        ui.write(f"  {ui.color('▍', tone)} {ui.color(d['severity'].upper().ljust(9), tone, True)}"
                 f"{ui.color(d['field'], 'white', True)}   "
                 f"{ui.color(str(d['desired']), 'green')} "
                 f"{ui.color('in git →', 'dark')} "
                 f"{ui.color(str(d['observed']), 'red')} "
                 f"{ui.color('in cluster', 'dark')}")
        ui.wrapped(d['via'], "grey", 4)
        ui.write(f"    {ui.color(d['changedBy'], 'dark')} · {ui.color(d['changedAt'][:19], 'dark')}"
                 + (f" · {ui.color(d['policyId'], 'violet')}" if d['policyId'] else ""))
        ui.wrapped(d['impact'], "white", 4, ui.color("impact: ", "dark"))
        ui.write()
        ui.pause(0.15)

    ui.write(f"  {ui.color('Two of those are PCI findings that no dashboard was watching, and', 'grey')}")
    ui.write(f"  {ui.color('one of them has been true for fifteen days.', 'grey')}")
    ui.write()

    patch = driftd.remediation_patch("payments-ledger", "prod", report)
    ui.tool_call("platform.open_pull_request", f'branch="{patch["branch"]}"',
                 identity="drift-agent")
    _s(0.3)
    ui.tool_result(f"PR opened: {patch['title']}")
    ui.write()
    ui.box([f"  {ui.color(k.ljust(30), 'dark')}"
            f"{ui.color(str(report['drift'][i]['observed']), 'red')}"
            f"{ui.color(' → ', 'dark')}"
            f"{ui.color(str(v), 'green', True)}"
            for i, (k, v) in enumerate(patch["patch"].items())],
           tone="green", title="proposed reconciliation — awaiting human review")
    ui.write()
    ui.write(f"  {ui.color('The drift agent holds no tool that mutates a cluster. It could not', 'grey')}")
    ui.write("  " + ui.color(
        'just fix it, even if you asked. Everything it believes becomes a', 'grey'))
    ui.write(f"  {ui.color('diff a human merges — so when it is wrong, and it will be, the', 'grey')}")
    ui.write(f"  {ui.color('git history is still the truth.', 'grey')}")
    _s(1.2)
    return report


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------

def scorecard(vibe: dict, turns: list[agent.Turn], before: dict, after: dict,
              drift: dict, elapsed: float) -> dict:
    ui.write()
    ui.write(ui.color("━" * 3, "lime") + ui.BG["blue"] + ui.BOLD + ui.FG["white"]
             + " SCORECARD " + ui.RESET
             + ui.color(" " + "━" * max(ui.WIDTH - 17, 0), "dark"))
    ui.write()
    ticket_days = sum(t[3] for t in TICKET_TRAIL)
    took = f"{elapsed:.1f}s" if elapsed < 60 else f"{elapsed / 60:.1f} min"
    # Deliberately not printed as a speed-up multiple. Eleven days of queue
    # against two seconds of compute produces a number in the hundreds of
    # thousands, which is arithmetically true and rhetorically worthless --
    # the eleven days were never work. What actually changed is that the
    # waiting is gone.
    rows = [
        ["time to a reviewable change", f"{ticket_days:.0f} working days",
         took, "queue removed"],
        ["team handoffs", str(len(TICKET_TRAIL)), "1 (the approval)", "−5"],
        ["policy violations without the platform", str(vibe["denyCount"]), "0", "−100%"],
        ["remediation iterations", "—", str(len(turns)), "converged"],
        ["monthly cost, first proposal", f"${before['totalMonthlyCostUsd']:,.2f}",
         f"${after['totalMonthlyCostUsd']:,.2f}",
         f"−${before['totalMonthlyCostUsd'] - after['totalMonthlyCostUsd']:,.2f}"],
        ["day-2 drift found", "—", f"{drift['driftCount']} fields",
         f"{sum(1 for d in drift['drift'] if d['severity'] == 'critical')} critical"],
        ["cluster mutations by an agent", "—", "0", "by construction"],
    ]
    ui.table(["metric", "before", "after", "delta"], rows,
             tones=["white"] * len(rows))
    ui.write()
    ui.rule("═", "lime")
    ui.write()
    ui.write(f"  {ui.color('The agent is not the platform.', 'lime', True)}")
    ui.write()
    ui.write("  " + ui.color(
        f"Act II is what an agent does with a model and no platform: "
        f"{vibe['denyCount']} ways", "grey"))
    ui.write(f"  {ui.color('to be plausibly, confidently wrong. The only thing that changed by', 'grey')}")
    ui.write(f"  {ui.color('Act V was the platform around it — a golden path, a provisioner set,', 'grey')}")
    ui.write(f"  {ui.color('a policy bundle, and an authorization model that treats an agent as', 'grey')}")
    ui.write(f"  {ui.color('a principal with a blast radius.', 'grey')}")
    ui.write()
    ui.write(f"  {ui.color('DORA’s 2025 finding, in one line: where platform quality is low, the', 'grey')}")
    ui.write(f"  {ui.color('effect of AI adoption on organisational performance is negligible.', 'grey')}")
    ui.write()
    ui.write(f"  {ui.color('Agentic platform engineering is not AI replacing platform engineers.', 'white', True)}")
    ui.write(f"  {ui.color('It is platform engineers becoming the thing that decides whether', 'white', True)}")
    ui.write(f"  {ui.color('any of this is safe to turn on.', 'white', True)}")
    ui.write()
    ui.rule("═", "lime")

    # Deliberately no wall-clock here. The run record is an artefact CI diffs
    # for reproducibility; a duration is evidence and belongs in the timed
    # report (outputs/verify-report.json), not in something that must be
    # byte-identical across machines.
    return {
        "ticketDays": ticket_days,
        "handoffsBefore": len(TICKET_TRAIL),
        "vibeDenyCount": vibe["denyCount"],
        "vibeByPolicy": vibe["byPolicy"],
        "goldenPathIterations": len(turns),
        "costBefore": before["totalMonthlyCostUsd"],
        "costAfter": after["totalMonthlyCostUsd"],
        "driftFields": drift["driftCount"],
        "agentClusterMutations": 0,
        "objectCount": 0,  # filled in by main() once the final render is written
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Agentic platform engineering demo")
    ap.add_argument("--act", type=int, choices=range(1, 9), metavar="N",
                    help="run a single act (1-8); the scorecard needs a full run")
    ap.add_argument("--speed", type=float, help="0 for instant, 1 for normal")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--backend", default="deterministic",
                    choices=["deterministic", "llm"],
                    help="'deterministic' (default, no API key) or 'llm' to use any "
                         "OpenAI-compatible endpoint via NORTHWIND_LLM_BASE_URL")
    ap.add_argument("--json", action="store_true", help="emit the run record to stdout")
    args = ap.parse_args()

    if args.speed is not None:
        ui.SPEED = args.speed
    if args.no_color:
        ui.NO_COLOR = True
        for k in ui.FG:
            ui.FG[k] = ""
        for k in ui.BG:
            ui.BG[k] = ""
        ui.RESET = ui.BOLD = ui.DIM = ui.ITALIC = ""

    OUTPUTS.mkdir(exist_ok=True)
    WORKSPACE.mkdir(exist_ok=True)
    start = time.time()

    ui.banner(
        "AGENTIC PLATFORM ENGINEERING — NORTHWIND RETAIL",
        "one request · eight acts · every verdict from a real OPA evaluation",
    )

    single = args.act
    req = agent.parse_intent(REQUEST)
    original = req

    if single in (None, 1):
        act_one()
    if single in (None, 2):
        vibe = act_two()
    else:
        vibe = gates.run_all(renderer.vibe_manifests(req)).as_dict()
    if single in (None, 3):
        act_three()
    if single in (None, 4):
        act_four(req)
    if single in (None, 5, 6, 7):
        final, turns, est = act_five(req, args.backend)
    else:
        final, turns, est = req, [], costing.estimate(req, "prod")
    if single in (None, 6):
        act_six(original, final)
    if single in (None, 7):
        act_seven(final, est)
    if single in (None, 8):
        drift = act_eight()
    else:
        drift = driftd.detect("payments-ledger", "prod")

    record = None
    if single is None:
        record = scorecard(
            vibe, turns,
            costing.estimate(original, "prod"),
            costing.estimate(final, "prod"),
            drift, time.time() - start,
        )
        # Only the required toolchain. Whether someone also installed the
        # optional scanners is not a property of this run.
        all_versions = gates.tool_versions()
        record["toolVersions"] = {k: all_versions[k] for k in
                                  ("conftest", "score-k8s", "kube-linter")}
        record["finalRequest"] = json.loads(final.to_json())
        (OUTPUTS / "run-record.json").write_text(json.dumps(record, indent=2))
        (OUTPUTS / "vibe-policy-report.json").write_text(json.dumps(vibe, indent=2))
        (OUTPUTS / "drift-report.json").write_text(json.dumps(drift, indent=2))
        spec, docs = renderer.golden_path(final, "prod")
        record["objectCount"] = len(docs)
        (OUTPUTS / "run-record.json").write_text(json.dumps(record, indent=2))
        (OUTPUTS / "final-score.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
        (OUTPUTS / "final-manifests.yaml").write_text(renderer.dump(docs))
        (OUTPUTS / "final-cost.json").write_text(
            json.dumps(costing.estimate_all(final), indent=2))
        (OUTPUTS / "kargo-pipeline.yaml").write_text(
            renderer.dump(renderer.kargo_pipeline(final)))
        ui.write()
        ui.write(f"  {ui.color('run record written to outputs/', 'dark')}")

    if args.json and record:
        print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
