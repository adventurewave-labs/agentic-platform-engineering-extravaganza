#!/usr/bin/env python3
"""
Run the acceptance checks and emit a machine-readable report.

The showcase page publishes a pass/fail table. That table has to come from
somewhere real, so it comes from here: each check is executed, timed, and
recorded. `outputs/verify-report.json` is what the page renders and what
`./run.sh verify` corroborates.

    python3 src/build_report.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"


def check(fn):
    """Run one check, timing it. Returns (ok, detail, ms)."""
    start = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as exc:  # a check that explodes is a check that failed
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return ok, detail, round((time.perf_counter() - start) * 1000, 1)


# --- the checks --------------------------------------------------------------

def c_toolchain():
    import gates
    versions = gates.tool_versions()
    required = ["conftest", "score-k8s", "kube-linter"]
    missing = [t for t in required if versions.get(t, "").startswith("not installed")]
    return not missing, (
        f"conftest {versions['conftest'].split()[-1]}, "
        f"score-k8s {versions['score-k8s'].split()[1]}, "
        f"kube-linter {versions['kube-linter']}"
        if not missing else f"missing: {', '.join(missing)}")


def c_rego_compiles():
    """Compile the bundles with OPA itself, and fail on OPA's own exit code.

    The previous version of this check shelled out to
    `conftest test --policy policy /dev/null`, which looks like it compiles the
    bundle and does not: conftest selects a parser from the file extension
    before it ever loads Rego, so /dev/null killed it with "unknown parser" and
    the string it grepped for could never appear. It reported success against a
    bundle containing a syntax error. `opa check --strict` is the right tool --
    it does nothing but compile, and it exits non-zero when that fails.
    """
    binary = ROOT / "bin" / "opa"
    if not binary.exists():
        return False, "bin/opa not installed; run ./bin/setup.sh"
    policy = ROOT / "policy"
    proc = subprocess.run([str(binary), "check", "--strict", str(policy)],
                          capture_output=True, text=True)
    bundles = sorted(d.name for d in policy.iterdir()
                     if d.is_dir() and any(d.glob("*.rego")))
    if proc.returncode != 0:
        first = ((proc.stderr or proc.stdout).strip().splitlines() or [""])[0]
        return False, f"opa check failed: {first[:160]}"
    rules = sum(1 for f in policy.rglob("*.rego")
                for line in f.read_text().splitlines()
                if line.startswith(("deny contains", "warn contains")))
    return True, (f"{len(bundles)} bundles ({', '.join(bundles)}), "
                  f"{rules} rule bodies compile under `opa check --strict`")


def c_unguided_denied():
    import agent, gates, renderer
    req = agent.parse_intent(REQUEST)
    r = gates.run_all(renderer.vibe_manifests(req))
    # This used to accept anything >= 20 against an actual figure of 42. A
    # threshold with that much slack cannot tell "the policies fired" from
    # "half of them stopped firing", which is the only thing it is here to
    # notice. The committed run record is the number to hold it to.
    expected = json.loads((OUTPUTS / "run-record.json").read_text())["vibeDenyCount"]
    got = len(r.findings)
    if got != expected:
        return False, (f"{got} denials, run record says {expected} -- "
                       f"policies changed, or stopped firing")
    return True, f"{got} denials across {len(r.by_policy())} distinct policies"


def c_golden_converges():
    import agent, costing, gates, renderer
    req = agent.parse_intent(REQUEST)
    for i in range(1, 6):
        spec, docs = renderer.golden_path(req, "prod")
        est = costing.estimate(req, "prod")
        r = gates.run_all(docs, score=spec, cost=est)
        if r.passed:
            return True, (f"0 denials after {i} iterations, "
                          f"${est['totalMonthlyCostUsd']:,.2f}/mo")
        req, decisions = agent.remediate(req, r.findings, "prod")
        if not decisions:
            break
    return False, "did not converge"


def c_kube_linter_clean():
    import agent, gates, renderer
    req = agent.parse_intent(REQUEST)
    _, docs = renderer.golden_path(req, "prod")
    r = gates.kube_linter(docs)
    return r.deny_count == 0, (
        f"0 findings on {len(docs)} rendered objects"
        if r.deny_count == 0 else f"{r.deny_count} findings")


def c_no_output_patching():
    """The agent must only ever change inputs.

    The earlier version of this check inspected `{d.field for d in decisions}`
    -- the remediation layer's own account of what it changed -- and never
    looked at the rendered documents at all. A remediation step that quietly
    stamped every rendered object still passed it, because the decision records
    stayed clean. Self-reported provenance is not provenance.

    This asks the question four ways, none of which the agent can answer by
    describing itself:

      0. the request fields that actually moved are exactly the ones the
         decisions declare -- an undeclared change to a legitimate input is
         still a silent edit;
      1. no rendered document the agent was shown comes back mutated;
      2. every byte of the next artefact is reproducible from a ServiceRequest
         rebuilt out of nothing but its own declared scalar fields -- so no
         patch can ride along on the request object either;
      3. the artefact did in fact change, so the rest is not vacuous.
    """
    import copy
    import agent, gates, renderer
    req = agent.parse_intent(REQUEST)
    spec, docs = renderer.golden_path(req, "prod")
    r = gates.run_all(docs, score=spec)

    before = copy.deepcopy(docs)
    updated, decisions = agent.remediate(req, r.findings, "prod")

    fields = {d.field for d in decisions}
    if not fields <= set(vars(req).keys()):
        stray = ", ".join(sorted(fields - set(vars(req).keys())))
        return False, f"decision(s) name something that is not a Score input: {stray}"

    # 0. The inputs that actually moved are exactly the ones the agent declared.
    #    An undeclared input change is a silent edit even though the field is a
    #    legitimate one -- it is the difference between "changed the retention
    #    window and said so" and "also rewrote the description".
    moved = {k for k, v in vars(updated).items() if vars(req).get(k) != v}
    if moved != fields:
        undeclared = ", ".join(sorted(moved - fields)) or "none"
        phantom = ", ".join(sorted(fields - moved)) or "none"
        return False, (f"declared changes do not match actual ones "
                       f"(undeclared: {undeclared}; claimed but unchanged: {phantom})")

    # 1. The documents handed to the agent are exactly as they were.
    if renderer.dump(docs) != renderer.dump(before):
        return False, "remediate() mutated the rendered documents it was shown"

    # 2. Rebuild the request from its own scalar fields and re-render. Anything
    #    the agent attached to the object rather than declaring as an input is
    #    dropped by the rebuild, and the two renders diverge.
    carried = renderer.dump(renderer.golden_path(updated, "prod")[1])
    rebuilt = renderer.ServiceRequest(**{k: copy.deepcopy(v)
                                         for k, v in vars(updated).items()})
    fresh = renderer.dump(renderer.golden_path(rebuilt, "prod")[1])
    if carried != fresh:
        return False, ("the next artefact is not reproducible from the declared "
                       "inputs alone; state is riding on the request object")

    # 3. Guard the guard: if remediation stopped changing anything, the two
    #    comparisons above would hold trivially.
    if not decisions or carried == renderer.dump(docs):
        return False, "remediation produced no change; the comparisons prove nothing"

    return True, (f"{len(decisions)} change(s), all to Score inputs "
                  f"({', '.join(sorted(fields))}); {len(docs)} rendered objects "
                  f"unmutated and reproducible from those inputs alone")


def c_authz_agent_denied():
    import platform_mcp
    from catalog import resolve_identity
    s = platform_mcp.Server(resolve_identity("platform-agent"))
    call = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "platform.approve_promotion",
        "arguments": {"service": "payments-ledger", "stage": "prod"}}})
    listed = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in listed["result"]["tools"]}
    denied = call["result"].get("isError") is True
    withheld = "platform.approve_promotion" not in names
    if not (denied and withheld):
        return False, (
            "approve_promotion "
            + ("was refused" if denied else "WAS NOT REFUSED")
            + " and "
            + ("was withheld from tools/list" if withheld else "IS LISTED"))
    return True, f"{len(names)}/{len(platform_mcp.TOOLS)} tools listed; approve_promotion withheld and refused"


def c_authz_human_allowed():
    import platform_mcp
    from catalog import resolve_identity
    s = platform_mcp.Server(resolve_identity("release-manager"))
    call = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "platform.approve_promotion",
        "arguments": {"service": "payments-ledger", "stage": "prod"}}})
    if call["result"].get("isError") is True:
        text = (call["result"].get("content") or [{}])[0].get("text", "")
        return False, f"release-manager was refused: {text[:120]}"
    return True, "release-manager holds delivery:approve and the call succeeds"


def c_identity_scoping():
    import platform_mcp
    from catalog import resolve_identity
    counts = {}
    for name in ("platform-agent", "drift-agent", "cost-reviewer", "release-manager"):
        counts[name] = len(platform_mcp.Session(resolve_identity(name)).visible_tools())
    # "more than one distinct count" is satisfied by almost any wiring, working
    # or broken. What the check is really claiming is an ordering: a read-only
    # reviewer sees fewer tools than a general agent, which sees fewer than the
    # human who can approve a promotion -- and nobody but that human sees all.
    total = len(platform_mcp.TOOLS)
    order = ["cost-reviewer", "drift-agent", "platform-agent", "release-manager"]
    detail = " · ".join(f"{k} {counts[k]}/{total}" for k in order)
    ascending = all(counts[a] < counts[b] for a, b in zip(order, order[1:]))
    if not ascending:
        return False, f"tool visibility is not ordered by privilege: {detail}"
    if counts["release-manager"] != total:
        return False, f"the approving human cannot see every tool: {detail}"
    if max(counts[k] for k in order[:-1]) >= total:
        return False, f"a non-approving identity sees every tool: {detail}"
    return True, detail


def c_mcp_protocol():
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "report", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "scaffolder.list_templates", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
    ]
    proc = subprocess.run(
        [sys.executable, str(ROOT / "src" / "platform_mcp.py")],
        input="\n".join(json.dumps(m) for m in msgs),
        capture_output=True, text=True, timeout=120, cwd=ROOT)
    out = {json.loads(l).get("id"): json.loads(l)
           for l in proc.stdout.splitlines() if l.strip()}
    stages = {
        "initialize": out.get(1, {}).get("result", {}).get("protocolVersion") == "2025-06-18",
        "tools/list": len(out.get(2, {}).get("result", {}).get("tools", [])) > 0,
        "tools/call": out.get(3, {}).get("result", {}).get("isError") is False,
        "resources/list": len(out.get(4, {}).get("result", {}).get("resources", [])) > 0,
    }
    failed = [k for k, v in stages.items() if not v]
    if failed:
        return False, f"no valid response to: {', '.join(failed)}"
    return True, " · ".join(stages) + " over stdio"


def c_mcp_origin():
    """The local HTTP transport must reject cross-origin browsers."""
    import platform_mcp
    local = platform_mcp._origin_allowed("http://localhost:3000")
    foreign = platform_mcp._origin_allowed("https://evil.example")
    if not local:
        return False, "the guard rejects localhost; the local transport is unusable"
    if foreign:
        return False, "https://evil.example was ACCEPTED -- the DNS-rebinding guard is open"
    return True, "localhost accepted, foreign origin rejected (DNS-rebinding guard)"


def c_pci_rules_fire():
    """The PCI bundle must actually reject a non-compliant SQLInstance."""
    import gates
    bad = {
        "apiVersion": "platform.northwind.io/v1alpha1", "kind": "SQLInstance",
        "metadata": {"name": "probe", "labels": {
            "northwind.io/data-classification": "pci",
            "northwind.io/data-residency": "eu"}},
        "spec": {"parameters": {"storageEncrypted": False, "backupRetentionDays": 1,
                                "publiclyAccessible": True, "region": "us-east-1"}},
    }
    r = gates.evaluate_kubernetes([bad])
    ids = {f.policy_id for f in r.findings}
    return {"NW-PCI-003", "NW-PCI-004"} <= ids, \
        f"{len(r.findings)} denials on a deliberately non-compliant database"


def c_deterministic_render():
    """Rendering the same request twice must produce identical bytes.

    This is the claim the whole repository rests on, so it gets its own check
    rather than being assumed. score-k8s mints a fresh UUID per provision and
    derives an instance suffix from freshly-created state; renderer.py
    normalises both to content-derived values, and this is what proves it.
    """
    import agent, renderer
    req = agent.parse_intent(REQUEST)
    first = renderer.dump(renderer.golden_path(req, "prod")[1])
    second = renderer.dump(renderer.golden_path(req, "prod")[1])
    same = first == second
    return same, (f"two independent renders, {len(first.splitlines())} lines, "
                  f"byte-identical" if same else "renders differ")


def c_reproducible():
    """Every number the committed run record claims, recomputed from scratch.

    This used to compare the unguided finding count and its per-policy
    breakdown and stop there, while the README advertised it as checking the
    iteration count, the cost before and after, and the object count too. Those
    were recomputed elsewhere and printed, never asserted -- so a change that
    moved the cost would have left this green and the committed artefact
    stale. Now the check does what the sentence says.
    """
    import agent, costing, gates, renderer
    prev = json.loads((OUTPUTS / "run-record.json").read_text())
    req = agent.parse_intent(REQUEST)

    vibe = gates.run_all(renderer.vibe_manifests(req))
    final, docs, est = _deterministic_landing()
    turns = 0
    probe = agent.parse_intent(REQUEST)
    for turns in range(1, 7):
        spec, d = renderer.golden_path(probe, "prod")
        report = gates.run_all(d, score=spec, cost=costing.estimate(probe, "prod"))
        if report.passed:
            turns -= 1
            break
        probe, decisions = agent.remediate(probe, report.findings, "prod")
        if not decisions:
            break

    actual = {
        "vibeDenyCount": len(vibe.findings),
        "vibeByPolicy": vibe.by_policy(),
        "goldenPathIterations": turns + 1,
        "costBefore": costing.estimate(agent.parse_intent(REQUEST),
                                       "prod")["totalMonthlyCostUsd"],
        "costAfter": est["totalMonthlyCostUsd"],
        "objectCount": len(docs),
        # Every other property here is a scalar somebody chose to record. A
        # digest of the rendered bytes is the one that notices a change nobody
        # chose to look for -- a label dropped, a probe retimed, a field
        # reordered. Without it "reproduces exactly" meant "the six numbers we
        # happened to write down still match".
        "manifestSha256": hashlib.sha256(renderer.dump(docs).encode()).hexdigest(),
    }
    mismatched = [k for k, v in actual.items() if prev.get(k) != v]
    if mismatched:
        if mismatched == ["manifestSha256"]:
            return False, ("every recorded number still matches but the rendered "
                           "manifests differ byte for byte; regenerate "
                           "outputs/ with ./run.sh demo")
        return False, "run record drifted: " + ", ".join(
            f"{k} {prev.get(k)!r} != {v!r}" for k, v in actual.items()
            if k in mismatched)
    return True, (f"all {len(actual)} recorded properties reproduce "
                  f"({actual['vibeDenyCount']} denials, "
                  f"{actual['goldenPathIterations']} iterations, "
                  f"${actual['costBefore']:,.2f} -> ${actual['costAfter']:,.2f}, "
                  f"{actual['objectCount']} objects, "
                  f"manifests sha256:{actual['manifestSha256'][:12]})")



def _deterministic_landing():
    """Run the golden path with the deterministic reasoner to convergence.

    Returns (request, rendered docs, cost estimate) at the point it passed.
    Two checks need this -- T14 to compare the committed run record against a
    fresh run, T15 to compare the LLM code path against the default one -- and
    having one definition of "where the loop lands" means they cannot disagree
    about it.
    """
    import agent, costing, gates, renderer
    req = agent.parse_intent(REQUEST)
    docs, est = [], costing.estimate(req, "prod")
    for _ in range(6):
        spec, docs = renderer.golden_path(req, "prod")
        est = costing.estimate(req, "prod")
        if gates.run_all(docs, score=spec, cost=est).passed:
            return req, docs, est
        req, decisions = agent.remediate(req, gates.run_all(
            docs, score=spec, cost=est).findings, "prod")
        if not decisions:
            break
    return req, docs, est


def c_llm_backend_loop():
    """The `--backend llm` path must converge too, not just the default one.

    The README's load-bearing claim about the LLM backend is that the loop is
    unchanged and the outcome is the same -- "a good platform makes the model
    boring". Nothing exercised that until this check: every other check runs the
    deterministic reasoner, so `LLMBackend.remediate` could regress silently and
    verify would stay green.

    `tests/fake_llm.py` serves the two OpenAI-compatible endpoints the client
    calls and replays a recorded transcript, wrapped in prose the way a real
    model replies. That means this exercises the parts this repository actually
    owns -- availability probe, wire format, JSON extraction, field mapping,
    convergence -- without a key, a network call or a token of spend. It does
    not assert that any given model is good at the task, which is not this
    repository's claim to make.
    """
    import os
    sys.path.insert(0, str(ROOT / "tests"))
    import agent, costing, gates, renderer
    from fake_llm import serve

    with serve() as (base_url, handler):
        previous = {k: os.environ.get(k) for k in
                    ("NORTHWIND_LLM_BASE_URL", "NORTHWIND_LLM_MODEL", "NORTHWIND_LLM_API_KEY")}
        os.environ["NORTHWIND_LLM_BASE_URL"] = base_url
        os.environ["NORTHWIND_LLM_MODEL"] = "recorded-transcript"
        os.environ.pop("NORTHWIND_LLM_API_KEY", None)
        try:
            reason, label = agent.get_reasoner("llm")
            if "deterministic" in label:
                return False, f"fell back to the deterministic reasoner: {label}"
            req = agent.parse_intent(REQUEST)
            docs = []
            for i in range(1, 6):
                spec, docs = renderer.golden_path(req, "prod")
                est = costing.estimate(req, "prod")
                r = gates.run_all(docs, score=spec, cost=est)
                if r.passed:
                    # Converging is not the claim. The claim is that it lands
                    # in the *same place*, so compare the artefacts rather than
                    # printing them: the rendered manifests byte for byte, and
                    # the monthly total to the cent.
                    det_req, det_docs, det_cost = _deterministic_landing()
                    same_manifests = renderer.dump(docs) == renderer.dump(det_docs)
                    same_cost = (round(est["totalMonthlyCostUsd"], 2)
                                 == round(det_cost["totalMonthlyCostUsd"], 2))
                    if not handler.calls:
                        return False, "the model was never called"
                    if not same_manifests:
                        return False, "the LLM path rendered different manifests"
                    if not same_cost:
                        return False, (f"cost differs: LLM "
                                       f"${est['totalMonthlyCostUsd']:,.2f} vs "
                                       f"deterministic "
                                       f"${det_cost['totalMonthlyCostUsd']:,.2f}")
                    return True, (
                        f"same manifests and same "
                        f"${est['totalMonthlyCostUsd']:,.2f}/mo as the "
                        f"deterministic reasoner, after {i} iterations and "
                        f"{len(handler.calls)} model call(s)")
                req, decisions = reason(req, r.findings, "prod")
                if not decisions:
                    return False, f"the model proposed no change at iteration {i}"
            return False, "did not converge through the LLM backend"
        finally:
            for k, v in previous.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


REQUEST = ("service called payments-ledger PCI EU high volume postgres "
           "staging and prod")

CHECKS = [
    ("T01", "Pinned upstream toolchain present", c_toolchain),
    ("T02", "Policy bundle compiles under OPA", c_rego_compiles),
    ("T03", "PCI rules reject a non-compliant database", c_pci_rules_fire),
    ("T04", "Unguided manifests are rejected", c_unguided_denied),
    ("T05", "Golden path converges to zero denials", c_golden_converges),
    ("T06", "Agent fixes inputs, never rendered output", c_no_output_patching),
    ("T07", "Independent linter finds nothing", c_kube_linter_clean),
    ("T08", "Agent cannot approve production", c_authz_agent_denied),
    ("T09", "A human in the owning group can", c_authz_human_allowed),
    ("T10", "Tool visibility differs per identity", c_identity_scoping),
    ("T11", "MCP 2025-06-18 protocol conformance", c_mcp_protocol),
    ("T12", "MCP HTTP transport validates Origin", c_mcp_origin),
    ("T13", "Rendering twice is byte-identical", c_deterministic_render),
    ("T14", "Committed artefacts reproduce exactly", c_reproducible),
    ("T15", "The LLM backend converges too", c_llm_backend_loop),
]


def main() -> int:
    results, total = [], 0.0
    for tid, name, fn in CHECKS:
        ok, detail, ms = check(fn)
        total += ms
        results.append({"id": tid, "name": name, "passed": ok,
                        "detail": detail, "ms": ms})
        mark = "\033[38;5;84mPASS\033[0m" if ok else "\033[38;5;203mFAIL\033[0m"
        print(f"  {tid}  {mark}  {name:46} {ms:>8.1f}ms")
        print(f"        \033[38;5;245m{detail}\033[0m")
    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed · {total:,.0f}ms total")

    import gates
    OUTPUTS.mkdir(exist_ok=True)
    (OUTPUTS / "verify-report.json").write_text(json.dumps({
        "passed": passed, "total": len(results),
        "totalMs": round(total, 1),
        "toolVersions": gates.tool_versions(),
        "checks": results,
    }, indent=2))
    print(f"  wrote outputs/verify-report.json")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
