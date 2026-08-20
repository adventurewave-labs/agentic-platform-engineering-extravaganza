#!/usr/bin/env bash
# Codespace / dev container bootstrap.
#
# Two things: the one Python dependency, and the pinned upstream binaries the
# gates actually run. Then it proves the checkout works by running the same
# verify suite CI runs, so a broken codespace announces itself immediately
# rather than at the first demo.

set -euo pipefail

cyan() { printf '\033[38;5;44m%s\033[0m\n' "$1"; }
dim()  { printf '\033[38;5;245m%s\033[0m\n' "$1"; }

cyan "Installing Python dependencies..."
pip install --no-cache-dir --disable-pip-version-check -q -r requirements.txt

cyan "Fetching the pinned upstream binaries..."
chmod +x ./run.sh ./bin/setup.sh ./scripts/verify.sh
./bin/setup.sh --all || ./bin/setup.sh

echo
./bin/setup.sh --check

echo
cyan "Verifying the checkout..."
if ./run.sh verify >/tmp/verify.log 2>&1; then
  tail -3 /tmp/verify.log
else
  printf '\033[38;5;203m%s\033[0m\n' "verify reported failures — see /tmp/verify.log"
fi

cat <<'BANNER'

  ┌──────────────────────────────────────────────────────────────────┐
  │  Agentic Platform Engineering Extravaganza — ready.              │
  │                                                                  │
  │    ./run.sh demo             the full run, eight acts            │
  │    ./run.sh act 5            just the policy gate + agent loop   │
  │    ./run.sh verify           ten checks that none of it is faked │
  │    ./run.sh tools <identity> what each agent identity may call   │
  │    ./run.sh site             the showcase page on :8080          │
  │    ./run.sh mcp              the platform MCP server on :8099    │
  │                                                                  │
  │  Identities to try with `tools`:                                 │
  │    platform-agent  drift-agent  cost-reviewer  release-manager   │
  └──────────────────────────────────────────────────────────────────┘

BANNER
