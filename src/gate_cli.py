#!/usr/bin/env python3
"""
`./run.sh gate <manifests.yaml>` -- run the whole gate stack over a file.

This exists because of a documentation bug worth remembering. The showcase page
displayed `$ conftest test --policy policy/ manifests.yaml` beside a badge
reading "42 denials", and that command does not produce 42. It produces 32.
The other ten come from kube-linter, which the page never mentioned. The
command line was written by hand to look like the thing that made the number,
and it was not.

The fix was not to reword the caption. It was to make the repository actually
have a command that produces the number the page prints, so the caption can be
generated from a real invocation and checked by a test. That command is this
one: the same `gates.run_all` the demo, the scorecard and the playground
builder all call.

    python3 src/gate_cli.py outputs/unguided-manifests.yaml
    python3 src/gate_cli.py --json outputs/unguided-manifests.yaml

Exit status is 1 when anything denies, 0 when nothing does.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import gates  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="./run.sh gate", description=__doc__)
    ap.add_argument("manifests", help="a YAML file of Kubernetes objects")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    path = Path(args.manifests)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        print(f"no such file: {args.manifests}", file=sys.stderr)
        return 2

    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    report = gates.run_all(docs, include_scanners=True)

    if args.json:
        print(json.dumps({
            "file": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            "objects": len(docs),
            "denyCount": len(report.findings),
            "byPolicy": report.by_policy(),
            "denies": [f.as_dict() for f in report.findings],
        }, indent=2))
        return 0 if report.passed else 1

    for i, f in enumerate(report.findings, 1):
        print(f"{i:>3}. [{f.policy_id}] ({f.tool}) {f.message}")
    tools = sorted({f.tool for f in report.findings})
    print()
    print(f"{len(report.findings)} denials across {len(report.by_policy())} "
          f"distinct policies over {len(docs)} objects"
          + (f" ({', '.join(tools)})" if tools else ""))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
