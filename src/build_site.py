#!/usr/bin/env python3
"""
Render index.html from site/index.template.html + real gate results.

The showcase page embeds recorded conftest output rather than reimplementing
Rego in JavaScript, which means the page can go stale if a policy changes. This
script is what keeps it honest: it regenerates the playground data from a live
evaluation and re-renders the page.

    python3 src/build_playground.py && python3 src/build_site.py

It also holds the two languages together. Every element carrying a `data-i18n`
attribute must have an entry in site/i18n.es.json, and that entry records the
English it was translated from. Edit the English without touching the Spanish
and this build fails rather than shipping a page that argues one thing in one
language and something else in the other.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ansi2html  # noqa: E402
TEMPLATE = ROOT / "site" / "index.template.html"
OUT = ROOT / "index.html"
DATA = ROOT / "outputs" / "playground.json"
REPORT = ROOT / "outputs" / "verify-report.json"
I18N_ES = ROOT / "site" / "i18n.es.json"

KEY = re.compile(r'data-i18n="([^"]+)"')


def _norm(s: str) -> str:
    """Whitespace-insensitive fingerprint -- reflowing a paragraph is not drift."""
    return " ".join(s.split())


def _inner(markup: str, key: str) -> str:
    """The inner HTML of the single element carrying data-i18n="key"."""
    at = markup.index(f'data-i18n="{key}"')
    tag_open = markup.rindex("<", 0, at)
    tag = markup[tag_open + 1:].split(None, 1)[0]
    start = markup.index(">", at) + 1
    depth, pos = 1, start
    while depth:
        nxt = re.compile(f"</?{re.escape(tag)}[ >]").search(markup, pos)
        if not nxt:
            raise ValueError(f"unterminated <{tag}> for {key}")
        depth += -1 if nxt.group().startswith("</") else 1
        pos = nxt.end()
    return markup[start:markup.rindex("<", start, pos)]


def check_translations(html: str, es: dict) -> list[str]:
    """Return every way the two languages have drifted apart."""
    problems = []
    keys = KEY.findall(html)
    dupes = {k for k in keys if keys.count(k) > 1}
    problems += [f"{k}: data-i18n key used more than once" for k in sorted(dupes)]

    translated = {k for k in es if not k.startswith("_")}
    for k in sorted(set(keys) - translated):
        problems.append(f"{k}: in the page, missing from i18n.es.json")
    for k in sorted(translated - set(keys)):
        problems.append(f"{k}: in i18n.es.json, no longer in the page")

    for k in sorted(set(keys) & translated):
        entry = es[k]
        if not entry.get("es"):
            problems.append(f"{k}: no Spanish text")
            continue
        want, have = _norm(entry.get("en", "")), _norm(_inner(html, k))
        if want != have:
            problems.append(
                f"{k}: English changed and the Spanish was not revisited\n"
                f"       recorded: {want[:90]}\n"
                f"       in page:  {have[:90]}"
            )
    return problems



# ---------------------------------------------------------------------------
# Blocks the page used to carry as hand-written HTML
# ---------------------------------------------------------------------------
#
# Three blocks on the page were marked up by hand and badged "REAL OUTPUT" or
# "REAL RENDER": the Act V transcript, the per-identity tool list, and a
# rendered SQLInstance. Every value in them had been copied from a real run, so
# they were true when pasted -- and two had already drifted by the time anyone
# checked. The transcript had lost the indentation on its continuation lines,
# and the SQLInstance was showing 7 of its 13 parameters with the metadata
# deleted and the secret name replaced by "...".
#
# They are now produced here, at build time, by running the program. The badge
# describes the build step instead of asserting someone's care.

def _run(args: list[str]) -> str:
    """Run one of the repo's own commands and return its ANSI output."""
    env = dict(os.environ, NORTHWIND_SPEED="0", PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} exited {proc.returncode}; the page cannot show "
            f"output from a command that failed.\n{proc.stderr[-800:]}")
    return proc.stdout


def _highlight_yaml(text: str) -> str:
    """Colour a YAML document with the classes the page's stylesheet defines.

    Only the shapes real rendered manifests produce: `key:`, `- item`, scalars,
    and comments. Anything it does not recognise is escaped and left plain,
    which is the safe direction -- an unstyled line is still a true line.
    """
    out = []
    for line in text.rstrip("\n").split("\n"):
        indent = line[:len(line) - len(line.lstrip())]
        body = line[len(indent):]
        m = re.match(r"^(-\s+)?([A-Za-z0-9_.\-/]+):(\s*)(.*)$", body)
        if not m:
            out.append(indent + html.escape(body))
            continue
        dash, key, gap, value = m.groups()
        chunk = indent + (f'<span class="c">{html.escape(dash)}</span>' if dash else "")
        chunk += f'<span class="k">{html.escape(key)}</span>:{gap}'
        if value:
            if value in ("true", "false"):
                cls = "g"
            elif re.fullmatch(r"-?\d+(\.\d+)?", value):
                cls = "n"
            else:
                cls = "s"
            chunk += f'<span class="{cls}">{html.escape(value)}</span>'
        out.append(chunk)
    return "\n".join(out)


def build_stamp() -> str:
    """What the footer attests to, taken from the committed run record.

    This was a hand-typed date. A date is the one thing on this page that
    cannot be reproduced from committed state -- CI rebuilds index.html and
    diffs it against the commit, so any stamp read from the clock or from
    `git log` turns into a spurious failure the first time a merge commit
    carries a different date than the page was built with. It also attested to
    nothing in particular.

    The short digest of the rendered manifests is deterministic, is checked by
    T14, and says something a reader can act on: the page was built from that
    exact artefact.
    """
    record = json.loads((ROOT / "outputs" / "run-record.json").read_text())
    digest = record.get("manifestSha256", "")
    if not digest:
        raise RuntimeError(
            "outputs/run-record.json has no manifestSha256; run ./run.sh demo")
    return f"manifests sha256:{digest[:12]}"


def act5_transcript() -> str:
    return ansi2html.convert(_run(["src/goldenpath.py", "--acts", "5"]).rstrip("\n"))


def tools_for(identity: str) -> str:
    return ansi2html.convert(_run(["src/platform_mcp.py", "--list-tools", "--identity", identity]).rstrip("\n"))


def sqlinstance_render() -> str:
    """The SQLInstance exactly as outputs/final-manifests.yaml holds it."""
    manifests = ROOT / "outputs" / "final-manifests.yaml"
    docs = [d for d in yaml.safe_load_all(manifests.read_text()) if d]
    found = [d for d in docs if d.get("kind") == "SQLInstance"]
    if len(found) != 1:
        raise RuntimeError(
            f"expected exactly one SQLInstance in {manifests.name}, found {len(found)}")
    return _highlight_yaml(yaml.safe_dump(found[0], sort_keys=False))


def main() -> int:
    if not TEMPLATE.exists():
        print(f"missing {TEMPLATE.relative_to(ROOT)}", file=sys.stderr)
        return 1
    if not DATA.exists():
        print("missing outputs/playground.json — run src/build_playground.py first",
              file=sys.stderr)
        return 1
    if not REPORT.exists():
        print("missing outputs/verify-report.json — run src/build_report.py first",
              file=sys.stderr)
        return 1
    if not I18N_ES.exists():
        print(f"missing {I18N_ES.relative_to(ROOT)}", file=sys.stderr)
        return 1

    template = TEMPLATE.read_text()
    es = json.loads(I18N_ES.read_text())
    problems = check_translations(template, es)
    if problems:
        print("the English and the Spanish have drifted apart:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("\n  fix site/i18n.es.json, then re-run.", file=sys.stderr)
        return 1

    payload = json.loads(DATA.read_text())
    # Trim to what the page actually renders; the full report stays in outputs/.
    slim = {
        "stages": [
            {
                "key": s["key"],
                "label": s["label"],
                "detail": s["detail"],
                "denyCount": s["denyCount"],
                "findings": [
                    {"id": f["id"], "tool": f["tool"], "message": f["message"]}
                    for f in s["findings"]
                ],
            }
            for s in payload["stages"]
        ],
        "goldenPath": payload["goldenPath"],
        # The command the page prints above the denial list, recorded by
        # build_playground.py from the invocation that produced the count.
        "gateCommand": payload["gateCommand"],
    }
    report = json.loads(REPORT.read_text())
    slim_report = {
        "passed": report["passed"],
        "total": report["total"],
        "totalMs": report["totalMs"],
        "toolVersions": {k: v for k, v in report["toolVersions"].items()
                         if k in ("conftest", "score-k8s", "kube-linter")},
        "checks": [{"id": c["id"], "name": c["name"], "passed": c["passed"],
                    "detail": c["detail"], "ms": c["ms"]}
                   for c in report["checks"]],
    }

    # Only what the page needs at runtime; the _readme prose stays in the repo.
    slim_es = {k: {"es": v["es"]} for k, v in es.items() if not k.startswith("_")}
    slim_es["_dynamic"] = es["_dynamic"]

    page = (template
            .replace("__ACT5_TRANSCRIPT__", act5_transcript())
            .replace("__TOOLS_PLATFORM_AGENT__", tools_for("platform-agent"))
            .replace("__SQLINSTANCE_RENDER__", sqlinstance_render())
            .replace("__PLAYGROUND__", json.dumps(slim, separators=(",", ":")))
            .replace("__REPORT__", json.dumps(slim_report, separators=(",", ":")))
            .replace("__I18N_ES__", json.dumps(slim_es, separators=(",", ":"),
                                               ensure_ascii=False)))
    # Last, so it reaches the copy inside the injected Spanish payload too.
    page = page.replace("__BUILD_STAMP__", build_stamp())
    for placeholder in ("__BUILD_STAMP__", "__ACT5_TRANSCRIPT__", "__TOOLS_PLATFORM_AGENT__",
                        "__SQLINSTANCE_RENDER__", "__PLAYGROUND__", "__REPORT__"):
        if placeholder in page:
            print(f"{placeholder} was never substituted", file=sys.stderr)
            return 1
    OUT.write_text(page)
    print(f"  wrote {OUT.relative_to(ROOT)}  ({len(page) // 1024} KiB)"
          f"  · en + es, {len(slim_es) - 1} strings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
