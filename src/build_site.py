#!/usr/bin/env python3
"""
Render index.html from site/index.template.html + real gate results.

The showcase page embeds recorded conftest output rather than reimplementing
Rego in JavaScript, which means the page can go stale if a policy changes. This
script is what keeps it honest: it regenerates the playground data from a live
evaluation and re-renders the page.

    python3 src/build_playground.py && python3 src/build_site.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "site" / "index.template.html"
OUT = ROOT / "index.html"
DATA = ROOT / "outputs" / "playground.json"
REPORT = ROOT / "outputs" / "verify-report.json"


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

    html = (TEMPLATE.read_text()
            .replace("__PLAYGROUND__", json.dumps(slim, separators=(",", ":")))
            .replace("__REPORT__", json.dumps(slim_report, separators=(",", ":"))))
    OUT.write_text(html)
    print(f"  wrote {OUT.relative_to(ROOT)}  ({len(html) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
