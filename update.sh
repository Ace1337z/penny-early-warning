#!/usr/bin/env bash
#
# Penny Stock Early-Warning System updater.
#
# Downloads the newest archive (or a requested tag), extracts it over the
# install directory (configuration, databases and bar files are untouched),
# reinstalls dependencies, runs the offline self-test, restarts the service and
# keeps the previous version for rollback.
#
# Usage:
#   bash update.sh                 # update to the configured version (main by default)
#   bash update.sh v1.1.0          # update to a tag
#   bash update.sh --rollback      # restore the previous version
#
set -euo pipefail

INSTALL_DIR="${PENNY_INSTALL_DIR:-/opt/penny}"
PENNY_URL="${PENNY_URL:-https://github.com/Ace1337z/penny-early-warning}"
PENNY_VERSION=""
ROLLBACK=0

for arg in "$@"; do
    case "$arg" in
        --rollback) ROLLBACK=1 ;;
        v*) PENNY_VERSION="$arg" ;;
        *) INSTALL_DIR="$arg" ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

PENNY="$INSTALL_DIR/.venv/bin/penny"
[[ -x "$PENNY" ]] || fail "penny is not installed at $INSTALL_DIR"

SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

if [[ "$ROLLBACK" == "1" ]]; then
    info "rolling back to the previous version"
    "$PENNY" --config "$INSTALL_DIR/config.env" rollback --install-dir "$INSTALL_DIR"
    if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl restart penny || true
    fi
    info "rollback complete"
    exit 0
fi

# 1. Back up the learning state before touching anything.
info "taking a pre-update backup"
"$PENNY" --config "$INSTALL_DIR/config.env" backup now --kind pre-update || \
    warn "pre-update backup failed; continuing (the updater keeps a code rollback)"

# 2. Download and install the new version.
ARGS=(update --install-dir "$INSTALL_DIR")
[[ -n "$PENNY_VERSION" ]] && ARGS+=(--version "$PENNY_VERSION")
[[ -n "${GITHUB_TOKEN:-}" ]] && ARGS+=(--private)
info "downloading the new version"
"$PENNY" --config "$INSTALL_DIR/config.env" "${ARGS[@]}"

# 3. Restart.
if command -v systemctl >/dev/null 2>&1; then
    info "restarting the service"
    $SUDO systemctl restart penny
    sleep 2
    $SUDO systemctl --no-pager status penny | head -n 12 || true
fi

info "update complete. If anything is wrong: bash update.sh --rollback"
