#!/usr/bin/env bash
# Fetch the pinned upstream binaries this demo runs on.
#
# Nothing here is vendored, wrapped or reimplemented. These are the official
# release artefacts, at pinned versions, from the projects' own GitHub
# releases. That is the whole point: the policy verdicts in this demo come from
# the same conftest you would run in your own CI.
#
#   conftest      Apache-2.0   open-policy-agent/conftest     required
#   score-k8s     Apache-2.0   score-spec/score-k8s           required
#   kube-linter   Apache-2.0   stackrox/kube-linter           required
#   opa           Apache-2.0   open-policy-agent/opa          optional
#   score-compose Apache-2.0   score-spec/score-compose       optional
#   trivy         Apache-2.0   aquasecurity/trivy             optional
#   opentofu      MPL-2.0      opentofu/opentofu              optional
#   agg           Apache-2.0   asciinema/agg                  optional (GIF rendering)
#
# Usage:
#   ./bin/setup.sh              # required tools only
#   ./bin/setup.sh --all        # everything, including the optional scanners
#   ./bin/setup.sh --check      # report what is present and at what version

set -euo pipefail

BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFTEST_VERSION="0.69.0"
SCORE_K8S_VERSION="0.16.0"
SCORE_COMPOSE_VERSION="0.45.0"
KUBE_LINTER_VERSION="0.8.3"
OPA_VERSION="1.19.1"
TRIVY_VERSION="0.74.0"
TOFU_VERSION="1.12.6"
AGG_VERSION="1.9.0"

case "$(uname -s)" in
  Linux)  OS=linux;  OS_TITLE=Linux ;;
  Darwin) OS=darwin; OS_TITLE=Darwin ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

case "$(uname -m)" in
  x86_64|amd64) ARCH=amd64; ARCH_ALT=x86_64 ;;
  arm64|aarch64) ARCH=arm64; ARCH_ALT=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

info() { printf '  \033[38;5;44m%-16s\033[0m %s\n' "$1" "$2"; }
warn() { printf '  \033[38;5;214m%-16s\033[0m %s\n' "$1" "$2"; }

# Deliberately only checks bin/, not $PATH. A system conftest of some other
# version silently replacing the pinned one would quietly invalidate the
# reproducibility this repo claims. Delete bin/<tool> to force a re-fetch.
have() { [ -x "$BIN_DIR/$1" ]; }

fetch_tar() { # name url member
  local name="$1" url="$2" member="${3:-}"
  if have "$name"; then info "$name" "already present, skipping"; return; fi
  local tmp; tmp="$(mktemp -d)"
  if ! curl -fsSL "$url" -o "$tmp/a.tgz"; then
    warn "$name" "download failed: $url"; rm -rf "$tmp"; return 1
  fi
  if [ -n "$member" ]; then
    tar -xzf "$tmp/a.tgz" -C "$BIN_DIR" "$member"
  else
    tar -xzf "$tmp/a.tgz" -C "$BIN_DIR"
  fi
  chmod +x "$BIN_DIR/${member:-$name}" 2>/dev/null || true
  rm -rf "$tmp"
  info "$name" "installed"
}

fetch_bin() { # name url
  local name="$1" url="$2"
  if have "$name"; then info "$name" "already present, skipping"; return; fi
  if ! curl -fsSL "$url" -o "$BIN_DIR/$name"; then
    warn "$name" "download failed: $url"; rm -f "$BIN_DIR/$name"; return 1
  fi
  chmod +x "$BIN_DIR/$name"
  info "$name" "installed"
}

install_required() {
  echo "Required:"
  fetch_tar conftest \
    "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_${OS_TITLE}_${ARCH_ALT}.tar.gz" \
    conftest
  fetch_tar score-k8s \
    "https://github.com/score-spec/score-k8s/releases/download/${SCORE_K8S_VERSION}/score-k8s_${SCORE_K8S_VERSION}_${OS}_${ARCH}.tar.gz" \
    score-k8s
  fetch_tar kube-linter \
    "https://github.com/stackrox/kube-linter/releases/download/v${KUBE_LINTER_VERSION}/kube-linter-${OS}.tar.gz"
  chmod +x "$BIN_DIR/kube-linter" 2>/dev/null || true
}

install_optional() {
  echo
  echo "Optional:"
  fetch_bin opa \
    "https://github.com/open-policy-agent/opa/releases/download/v${OPA_VERSION}/opa_${OS}_${ARCH}_static" || true
  fetch_tar score-compose \
    "https://github.com/score-spec/score-compose/releases/download/${SCORE_COMPOSE_VERSION}/score-compose_${SCORE_COMPOSE_VERSION}_${OS}_${ARCH}.tar.gz" \
    score-compose || true
  local trivy_arch="64bit"; [ "$ARCH" = "arm64" ] && trivy_arch="ARM64"
  fetch_tar trivy \
    "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_${OS_TITLE}-${trivy_arch}.tar.gz" \
    trivy || true
  fetch_tar tofu \
    "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_${OS}_${ARCH}.tar.gz" \
    tofu || true
  local agg_target="${ARCH_ALT}-unknown-linux-gnu"
  [ "$OS" = "darwin" ] && agg_target="${ARCH_ALT}-apple-darwin"
  fetch_bin agg \
    "https://github.com/asciinema/agg/releases/download/v${AGG_VERSION}/agg-${agg_target}" || true
}

check() {
  printf '%-16s %s\n' "TOOL" "VERSION"
  for t in conftest score-k8s kube-linter opa score-compose trivy tofu agg; do
    if [ -x "$BIN_DIR/$t" ]; then
      case "$t" in
        opa|kube-linter) v="$("$BIN_DIR/$t" version 2>&1 | head -1)" ;;
        agg) v="$("$BIN_DIR/$t" --version 2>&1 | head -1)" ;;
        *) v="$("$BIN_DIR/$t" --version 2>&1 | head -1)" ;;
      esac
      printf '%-16s %s\n' "$t" "$v"
    else
      printf '%-16s %s\n' "$t" "not installed"
    fi
  done
}

case "${1:-}" in
  --check) check ;;
  --all)   install_required; install_optional; echo; check ;;
  *)       install_required; echo
           echo "  Optional scanners not installed. Run './bin/setup.sh --all' for"
           echo "  Trivy, OpenTofu, standalone OPA and the asciinema GIF renderer." ;;
esac
