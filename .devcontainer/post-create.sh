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
# Only the entry points that exist. This used to chmod ./scripts/verify.sh,
# which has never been in this repository -- under `set -e` that aborted the
# bootstrap right here, before a single binary was fetched and before verify
# ran, so a codespace came up with none of the tools and no complaint about it.
# The badge at the top of the README pointed straight at it.
# tests/test_docs.py now fails if this line names a path that is not there.
chmod +x ./run.sh ./bin/setup.sh
./bin/setup.sh --all || ./bin/setup.sh

echo
./bin/setup.sh --check

echo
cyan "Running the unit tests..."
if ./run.sh test >/tmp/test.log 2>&1; then
  tail -3 /tmp/test.log
else
  printf '\033[38;5;203m%s\033[0m\n' "unit tests reported failures — see /tmp/test.log"
fi

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
  │    ./run.sh verify           15 checks that none of it is faked  │
  │    ./run.sh test             81 unit tests, stdlib unittest      │
│    ./run.sh tools <identity> what each agent identity may call   │
  │    ./run.sh site             the showcase page on :8080          │
  │    ./run.sh mcp              the platform MCP server on :8099    │
  │                                                                  │
  │  Identities to try with `tools`:                                 │
  │    platform-agent  drift-agent  cost-reviewer  release-manager   │
  └──────────────────────────────────────────────────────────────────┘

BANNER
