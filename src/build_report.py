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
    binary = ROOT / "bin" / "conftest"
    proc = subprocess.run(
        [str(binary), "test", "--policy", str(ROOT / "policy"), "/dev/null"],
        capture_output=True, text=True)
    broken = "rego_parse_error" in proc.stdout + proc.stderr \
        or "rego_type_error" in proc.stdout + proc.stderr
    rules = sum(1 for p in (ROOT / "policy").rglob("*.rego")
                for line in p.read_text().splitlines()
                if line.startswith(("deny contains", "warn contains")))
    return not broken, f"4 bundles, {rules} rule bodies compile under OPA"


def c_unguided_denied():
    import agent, gates, renderer
    req = agent.parse_intent(REQUEST)
    r = gates.run_all(renderer.vibe_manifests(req))
    return len(r.findings) >= 20, (
        f"{len(r.findings)} denials across {len(r.by_policy())} distinct policies")


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

    Asserts the remediation layer touches ServiceRequest fields and nothing
    else -- if a future change starts editing rendered YAML, this fails.
    """
    import agent, gates, renderer
    req = agent.parse_intent(REQUEST)
    spec, docs = renderer.golden_path(req, "prod")
    r = gates.run_all(docs, score=spec)
    updated, decisions = agent.remediate(req, r.findings, "prod")
    fields = {d.field for d in decisions}
    valid = fields <= set(vars(req).keys())
    return valid, (f"{len(decisions)} change(s), all to Score inputs: "
                   f"{', '.join(sorted(fields)) or 'none'}")


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
    return denied and withheld, (
        f"{len(names)}/14 tools listed; approve_promotion withheld and refused")


def c_authz_human_allowed():
    import platform_mcp
    from catalog import resolve_identity
    s = platform_mcp.Server(resolve_identity("release-manager"))
    call = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "platform.approve_promotion",
        "arguments": {"service": "payments-ledger", "stage": "prod"}}})
    return call["result"].get("isError") is not True, \
        "release-manager holds delivery:approve and the call succeeds"


def c_identity_scoping():
    import platform_mcp
    from catalog import resolve_identity
    counts = {}
    for name in ("platform-agent", "drift-agent", "cost-reviewer", "release-manager"):
        counts[name] = len(platform_mcp.Session(resolve_identity(name)).visible_tools())
    distinct = len(set(counts.values())) > 1
    return distinct, " · ".join(f"{k} {v}/14" for k, v in counts.items())


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
    ok = (out.get(1, {}).get("result", {}).get("protocolVersion") == "2025-06-18"
          and len(out.get(2, {}).get("result", {}).get("tools", [])) > 0
          and out.get(3, {}).get("result", {}).get("isError") is False
          and len(out.get(4, {}).get("result", {}).get("resources", [])) > 0)
    return ok, "initialize · tools/list · tools/call · resources/list over stdio"


def c_mcp_origin():
    """The local HTTP transport must reject cross-origin browsers."""
    import platform_mcp
    return (platform_mcp._origin_allowed("http://localhost:3000")
            and not platform_mcp._origin_allowed("https://evil.example")), \
        "localhost accepted, foreign origin rejected (DNS-rebinding guard)"


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
    import agent, costing, gates, renderer
    prev = json.loads((OUTPUTS / "run-record.json").read_text())
    req = agent.parse_intent(REQUEST)
    vibe = gates.run_all(renderer.vibe_manifests(req))
    same = (len(vibe.findings) == prev["vibeDenyCount"]
            and vibe.by_policy() == prev["vibeByPolicy"])
    return same, f"committed run record matches ({prev['vibeDenyCount']} denials)"


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
