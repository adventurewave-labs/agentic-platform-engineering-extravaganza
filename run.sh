#!/usr/bin/env bash
# Agentic Platform Engineering Extravaganza — orchestrator.
#
#   ./run.sh setup        fetch the pinned upstream binaries (conftest, score-k8s, ...)
#   ./run.sh demo         run the full eight-act demo
#   ./run.sh act N        run one act (1-8)
#   ./run.sh mcp          start the platform MCP server on :8099 (streamable HTTP)
#   ./run.sh tools [id]   print the MCP tool list a given identity would receive
#   ./run.sh gate [file]  run the policy bundle against a manifest file
#   ./run.sh drift        run the day-2 drift agent
#   ./run.sh site         serve the showcase page on :8080
#   ./run.sh verify       run the 15 acceptance checks against real tool output
#   ./run.sh test         run the unit tests (stdlib unittest, no extra deps)
#   ./run.sh record       rebuild the asciinema casts and GIFs
#   ./run.sh versions     print the exact upstream tool versions in play
#
# Requires Python 3.10+ and PyYAML. Everything else is fetched by `setup`.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
export PATH="$ROOT/bin:$PATH"
export PYTHONPATH="$ROOT/src:${PYTHONPATH:-}"

# --- preflight ---------------------------------------------------------------
# A missing dependency should say what to do about it, not raise a traceback
# out of an import three files deep.

preflight() {
  if ! command -v "$PY" >/dev/null 2>&1; then
    echo "error: no '$PY' on PATH. This needs Python 3.10 or newer." >&2
    exit 1
  fi
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "error: Python 3.10+ required; found $("$PY" -V 2>&1)." >&2
    echo "       In a codespace or dev container this is already handled." >&2
    exit 1
  fi
  if ! "$PY" -c 'import yaml' 2>/dev/null; then
    echo "PyYAML is missing — it is the only Python dependency this repo has." >&2
    # Install into the *same* interpreter that will import it. Using a bare
    # `pip3` here is the classic way to install a package the running Python
    # cannot see.
    if [ "${NORTHWIND_AUTO_INSTALL:-1}" = "1" ] && "$PY" -m pip --version >/dev/null 2>&1; then
      echo "Installing it into $("$PY" -c 'import sys; print(sys.executable)')" >&2
      echo "  (set NORTHWIND_AUTO_INSTALL=0 to disable)" >&2
      "$PY" -m pip install --quiet --disable-pip-version-check \
        -r "$ROOT/requirements.txt" 2>/dev/null \
        || "$PY" -m pip install --quiet --disable-pip-version-check \
             --break-system-packages -r "$ROOT/requirements.txt" \
        || { echo "  install failed — run: $PY -m pip install PyYAML" >&2; exit 1; }
      "$PY" -c 'import yaml' 2>/dev/null \
        || { echo "  PyYAML still not importable by $PY" >&2; exit 1; }
    else
      echo "  run: $PY -m pip install -r requirements.txt" >&2
      exit 1
    fi
  fi
}

need_tools() {
  preflight
  local missing=0
  for t in conftest score-k8s kube-linter; do
    [ -x "$ROOT/bin/$t" ] || command -v "$t" >/dev/null 2>&1 || { missing=1; }
  done
  if [ "$missing" = 1 ]; then
    echo "Missing required binaries. Running ./bin/setup.sh ..." >&2
    bash "$ROOT/bin/setup.sh"
  fi
}

cmd_setup()    { bash "$ROOT/bin/setup.sh" "$@"; }
cmd_versions() { need_tools; "$PY" "$ROOT/src/gates.py"; }

cmd_demo() {
  need_tools
  "$PY" "$ROOT/src/goldenpath.py" "$@"
}

cmd_act() {
  need_tools
  local n="${1:?usage: ./run.sh act N}"
  shift || true
  "$PY" "$ROOT/src/goldenpath.py" --act "$n" "$@"
}

cmd_mcp() {
  local port="${1:-8099}"
  echo "Northwind platform MCP server on http://127.0.0.1:${port}/mcp"
  echo "  identity: ${NORTHWIND_IDENTITY:-platform-agent}"
  echo
  echo "  claude mcp add --transport http northwind http://127.0.0.1:${port}/mcp"
  echo
  "$PY" "$ROOT/src/platform_mcp.py" --http "$port"
}

cmd_tools() {
  "$PY" "$ROOT/src/platform_mcp.py" --list-tools --identity "${1:-platform-agent}"
}

cmd_gate() {
  need_tools
  local target="${1:-}"
  if [ -z "$target" ]; then
    echo "usage: ./run.sh gate <manifests.yaml>"
    echo "example: ./run.sh gate outputs/final-manifests.yaml"
    exit 1
  fi
  local conftest="$ROOT/bin/conftest"
  [ -x "$conftest" ] || conftest="$(command -v conftest)"
  "$conftest" test --policy "$ROOT/policy" "$target"
}

cmd_drift()   { "$PY" "$ROOT/src/driftd.py" "${@:-payments-ledger}"; }
cmd_site()    { cd "$ROOT" && echo "http://localhost:8080" && "$PY" -m http.server 8080 --bind "${NORTHWIND_BIND:-127.0.0.1}"; }
cmd_record()  { need_tools; "$PY" "$ROOT/src/build_casts.py" "$@"; }
cmd_verify()  { need_tools; "$PY" "$ROOT/src/build_report.py"; }
cmd_test()    { preflight; PYTHONPATH="$ROOT/src:$ROOT/tests" \
                "$PY" -m unittest discover -s "$ROOT/tests" -t "$ROOT" "$@"; }

cmd_help() {
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

case "${1:-demo}" in
  setup)    shift; cmd_setup "$@" ;;
  demo)     shift; cmd_demo "$@" ;;
  act)      shift; cmd_act "$@" ;;
  mcp)      shift; cmd_mcp "$@" ;;
  tools)    shift; cmd_tools "$@" ;;
  gate)     shift; cmd_gate "$@" ;;
  drift)    shift; cmd_drift "$@" ;;
  site)     shift; cmd_site "$@" ;;
  verify)   shift; cmd_verify "$@" ;;
  test)     shift; cmd_test "$@" ;;
  record)   shift; cmd_record "$@" ;;
  versions) shift; cmd_versions "$@" ;;
  help|-h|--help) cmd_help ;;
  *) echo "unknown command: $1"; echo; cmd_help; exit 1 ;;
esac
