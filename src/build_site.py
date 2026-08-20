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

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "site" / "index.template.html"
OUT = ROOT / "index.html"
DATA = ROOT / "outputs" / "playground.json"
REPORT = ROOT / "outputs" / "verify-report.json"
I18N_ES = ROOT / "site" / "i18n.es.json"

KEY = re.compile(r'data-i18n="([^"]+)"')


def _norm(s: str) -> str:
    """Whitespace-insensitive fingerprint -- reflowing a paragraph is not drift."""
    return " ".join(s.split())


def _inner(html: str, key: str) -> str:
    """The inner HTML of the single element carrying data-i18n="key"."""
    at = html.index(f'data-i18n="{key}"')
    tag_open = html.rindex("<", 0, at)
    tag = html[tag_open + 1:].split(None, 1)[0]
    start = html.index(">", at) + 1
    depth, pos = 1, start
    while depth:
        nxt = re.compile(f"</?{re.escape(tag)}[ >]").search(html, pos)
        if not nxt:
            raise ValueError(f"unterminated <{tag}> for {key}")
        depth += -1 if nxt.group().startswith("</") else 1
        pos = nxt.end()
    return html[start:html.rindex("<", start, pos)]


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

    html = (template
            .replace("__PLAYGROUND__", json.dumps(slim, separators=(",", ":")))
            .replace("__REPORT__", json.dumps(slim_report, separators=(",", ":")))
            .replace("__I18N_ES__", json.dumps(slim_es, separators=(",", ":"),
                                               ensure_ascii=False)))
    OUT.write_text(html)
    print(f"  wrote {OUT.relative_to(ROOT)}  ({len(html) // 1024} KiB)"
          f"  · en + es, {len(slim_es) - 1} strings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
