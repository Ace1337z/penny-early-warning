#!/usr/bin/env bash
#
# Penny Stock Early-Warning System installer.
#
# Usage (run directly, NOT piped into bash, so it can read your answers):
#
#   wget https://raw.githubusercontent.com/<USER>/penny-early-warning/main/install.sh
#   bash install.sh
#
# It is idempotent: running it again keeps existing values (shown masked;
# pressing Enter keeps them).
#
# Environment (non-interactive mode, skips the prompts):
#   PENNY_NONINTERACTIVE=1
#   PENNY_INSTALL_DIR=/opt/penny
#   PENNY_VERSION=main            # or a tag such as v1.0.0
#   PENNY_URL=https://github.com/<USER>/penny-early-warning
#   MOOMOO_API_KEY=... MOOMOO_PRIVATE_KEY_PATH=... FINVIZ_TOKEN=...
#   TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... AI_BASE_URL=... AI_KEY=... AI_MODELS=...
#   SEC_USER_AGENT="Name email" ...
#   GITHUB_TOKEN=...              # only for a private repository
#   PENNY_RESTORE_FROM=... PENNY_BACKUP_PASSPHRASE=...   # optional restore
#
set -euo pipefail

REPO_DEFAULT="https://github.com/Ace1337z/penny-early-warning"
INSTALL_DIR="${PENNY_INSTALL_DIR:-/opt/penny}"
PENNY_URL="${PENNY_URL:-$REPO_DEFAULT}"
PENNY_VERSION="${PENNY_VERSION:-main}"
SERVICE_NAME="penny"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# --- helpers ---------------------------------------------------------------
info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

ask() {  # ask <prompt> <var-name> <secret:0|1> <required:0|1>
    local prompt="$1" var="$2" secret="$3" required="$4" current input
    current="$(eval "printf '%s' \"\${$var:-}\"")"
    if [[ -n "$current" ]]; then
        if [[ "$secret" == "1" ]]; then
            prompt="$prompt [${current:0:4}******]"
        else
            prompt="$prompt [$current]"
        fi
    fi
    [[ "$required" == "0" ]] && prompt="$prompt (optional, Enter to skip)"
    if [[ "$secret" == "1" ]]; then
        read -r -s -p "$prompt: " input </dev/tty; echo
    else
        read -r -p "$prompt: " input </dev/tty
    fi
    if [[ -n "$input" ]]; then
        eval "$var=\$input"
    fi
}

confirm() {  # confirm <prompt> -> 0 yes / 1 no
    local answer
    read -r -p "$1 [y/N]: " answer </dev/tty
    [[ "$answer" =~ ^[Yy]$ ]]
}

need_root_or_sudo() {
    if [[ "$(id -u)" -ne 0 ]]; then
        if command -v sudo >/dev/null 2>&1; then
            SUDO="sudo"
        else
            warn "not root and no sudo: systemd installation will be skipped"
            SUDO=""
        fi
    else
        SUDO=""
    fi
}

# --- 1. credentials first --------------------------------------------------
info "Penny Stock Early-Warning System installer"
info "install directory: $INSTALL_DIR"
echo
info "Step 1/9: credentials (asked before anything is downloaded)"
echo "Secrets are read with hidden input and are never echoed."

if [[ "${PENNY_NONINTERACTIVE:-0}" == "1" || ! -t 0 ]]; then
    info "non-interactive mode: reading values from the environment"
    MOOMOO_API_KEY="${MOOMOO_API_KEY:-}"
    MOOMOO_PRIVATE_KEY_PATH="${MOOMOO_PRIVATE_KEY_PATH:-}"
    FINVIZ_TOKEN="${FINVIZ_TOKEN:-}"
    TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-}"
    TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
    AI_BASE_URL="${AI_BASE_URL:-}"
    AI_KEY="${AI_KEY:-}"
    AI_MODELS="${AI_MODELS:-}"
    SEC_USER_AGENT="${SEC_USER_AGENT:-}"
    ALPACA_KEY="${ALPACA_KEY:-}"; ALPACA_SECRET="${ALPACA_SECRET:-}"
    FINNHUB_KEY="${FINNHUB_KEY:-}"
    HALALSH_API_KEY="${HALALSH_API_KEY:-}"; MUSAFFA_API_KEY="${MUSAFFA_API_KEY:-}"
    BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"; BACKUP_REMOTE="${BACKUP_REMOTE:-}"
    GITHUB_TOKEN="${GITHUB_TOKEN:-}"
    PENNY_RESTORE_FROM="${PENNY_RESTORE_FROM:-}"
else
    ask "Moomoo AppKey (mover discovery, snapshots, short data; open.moomoo.com/dashboard)" \
        MOOMOO_API_KEY 1 1
    ask "Moomoo private key file path (leave empty to generate a new pair)" \
        MOOMOO_PRIVATE_KEY_PATH 0 0
    ask "Finviz Elite API token (universe, verification, news, filings)" FINVIZ_TOKEN 1 1
    ask "Telegram bot token (@BotFather)" TELEGRAM_TOKEN 1 1
    TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
    ask "AI gateway base URL" AI_BASE_URL 0 1
    ask "AI gateway key" AI_KEY 1 1
    ask "Active AI panel model ids (comma separated, e.g. a,b,c)" AI_MODELS 0 0
    ask "SEC contact as 'Name email' (required for SEC EDGAR)" SEC_USER_AGENT 0 1
    echo
    info "optional sources (press Enter to skip)"
    ask "Alpaca key id (candles fallback, news)" ALPACA_KEY 1 0
    ask "Alpaca secret" ALPACA_SECRET 1 0
    ask "Finnhub key (news)" FINNHUB_KEY 1 0
    ask "halal.sh API key (Shariah status)" HALALSH_API_KEY 1 0
    ask "Musaffa API key (Shariah status)" MUSAFFA_API_KEY 1 0
    echo
    ask "Backup passphrase (required for off-server backups)" BACKUP_PASSPHRASE 1 0
    ask "Off-server backup destination (rclone remote or a directory)" BACKUP_REMOTE 0 0
    if [[ -n "$BACKUP_PASSPHRASE" ]]; then
        echo
        warn "Store this passphrase somewhere safe: encrypted backups cannot be read without it."
        confirm "Have you stored the passphrase safely?" || fail "passphrase not confirmed"
    fi
    if [[ "$PENNY_URL" == *"github.com"* ]]; then
        ask "GitHub read-only token (only for a private repository)" GITHUB_TOKEN 1 0
    fi
    echo
    if confirm "Restore the learning state from an existing backup?"; then
        ask "Backup source (a path, an rclone remote, or 'latest')" PENNY_RESTORE_FROM 0 1
        ask "Backup passphrase" BACKUP_PASSPHRASE 1 1
    fi
fi

# --- 2. system packages ----------------------------------------------------
info "Step 2/9: system packages"
need_root_or_sudo
if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y >/dev/null
    $SUDO apt-get install -y python3 python3-venv python3-pip wget tar chrony >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y python3 python3-pip wget tar chrony >/dev/null
else
    warn "unknown package manager; ensure python3, venv, pip, wget and tar are installed"
fi
command -v chronyc >/dev/null 2>&1 && $SUDO chronyc makestep >/dev/null 2>&1 || true

# --- 3. download the source ------------------------------------------------
info "Step 3/9: downloading the source"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [[ "$PENNY_VERSION" == "main" ]]; then
    ARCHIVE="$PENNY_URL/archive/refs/heads/main.tar.gz"
else
    ARCHIVE="$PENNY_URL/archive/refs/tags/$PENNY_VERSION.tar.gz"
fi
WGET_OPTS=(-q -O "$TMP/src.tar.gz")
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    WGET_OPTS+=(--header="Authorization: token $GITHUB_TOKEN")
fi
wget "${WGET_OPTS[@]}" "$ARCHIVE" || fail "download failed: $ARCHIVE"
mkdir -p "$TMP/src"
tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1
$SUDO mkdir -p "$INSTALL_DIR"
$SUDO cp -r "$TMP/src/." "$INSTALL_DIR/"
$SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR" 2>/dev/null || true
cd "$INSTALL_DIR"

# --- 4. virtual environment ------------------------------------------------
info "Step 4/9: virtual environment and dependencies"
$PYTHON_BIN -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
PENNY="$INSTALL_DIR/.venv/bin/penny"
if [[ ! -x "$PENNY" ]]; then
    "$INSTALL_DIR/.venv/bin/pip" install --quiet -e "$INSTALL_DIR"
fi

# --- 5. configuration ------------------------------------------------------
info "Step 5/9: configuration file"
export PENNY_HOME="$INSTALL_DIR"
CONFIG_FILE="$INSTALL_DIR/config.env"
if [[ -f "$CONFIG_FILE" ]]; then
    info "existing configuration found; values are kept unless you overwrite them"
fi
export MOOMOO_API_KEY FINVIZ_TOKEN TELEGRAM_TOKEN TELEGRAM_CHAT_ID AI_BASE_URL AI_KEY
export AI_MODELS SEC_USER_AGENT ALPACA_KEY ALPACA_SECRET FINNHUB_KEY HALALSH_API_KEY
export MUSAFFA_API_KEY BACKUP_PASSPHRASE BACKUP_REMOTE MOOMOO_PRIVATE_KEY_PATH
"$PENNY" --config "$CONFIG_FILE" setup --non-interactive --skip-doctor
chmod 600 "$CONFIG_FILE" 2>/dev/null || true

# --- 6. helpers: key pair and chat id --------------------------------------
info "Step 6/9: Moomoo key pair and Telegram chat id"
if [[ -z "${MOOMOO_PRIVATE_KEY_PATH:-}" || ! -f "${MOOMOO_PRIVATE_KEY_PATH:-/nonexistent}" ]]; then
    KEY_PATH="$INSTALL_DIR/moomoo_private_key.pem"
    "$PENNY" --config "$CONFIG_FILE" keygen --algo ED25519 --out "$KEY_PATH" --set-config
    echo
    warn "Upload the public key printed above on open.moomoo.com/dashboard, then press Enter."
    read -r -p "" </dev/tty || true
fi
if [[ -z "${TELEGRAM_CHAT_ID:-}" && -t 0 ]]; then
    "$PENNY" --config "$CONFIG_FILE" detect-chat || \
        warn "set the chat id later with: penny set TELEGRAM_CHAT_ID <id>"
fi

# --- 7. systemd unit -------------------------------------------------------
info "Step 7/9: systemd service"
RUN_USER="${PENNY_USER:-$(id -un)}"
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    $SUDO cp "$INSTALL_DIR/deploy/penny.service" "/etc/systemd/system/$SERVICE_NAME.service"
    $SUDO sed -i "s|@USER@|$RUN_USER|g; s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        "/etc/systemd/system/$SERVICE_NAME.service"
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$SERVICE_NAME" >/dev/null
    info "service installed and enabled"
else
    warn "systemd not installed by the installer. To enable it later run:"
    echo "  sudo cp $INSTALL_DIR/deploy/penny.service /etc/systemd/system/penny.service"
    echo "  sudo sed -i 's|@USER@|$RUN_USER|g; s|@INSTALL_DIR@|$INSTALL_DIR|g' /etc/systemd/system/penny.service"
    echo "  sudo systemctl daemon-reload && sudo systemctl enable --now penny"
fi

# --- 8. self-test and live checker -----------------------------------------
info "Step 8/9: offline self-test, then the live checker"
"$PENNY" --config "$CONFIG_FILE" selftest || warn "self-test reported failures (see above)"
echo
"$PENNY" --config "$CONFIG_FILE" doctor --reenter || warn "doctor reported failures (see above)"

# --- 9. start --------------------------------------------------------------
info "Step 9/9: start the service"
if command -v systemctl >/dev/null 2>&1 && [[ -n "$SUDO" || "$(id -u)" -eq 0 ]]; then
    if [[ -n "${PENNY_RESTORE_FROM:-}" ]]; then
        info "restoring the learning state before the first run"
        "$PENNY" --config "$CONFIG_FILE" restore "$PENNY_RESTORE_FROM" --scope full --yes || \
            warn "restore failed; starting with an empty learning state"
    fi
    $SUDO systemctl restart "$SERVICE_NAME"
    sleep 2
    $SUDO systemctl --no-pager status "$SERVICE_NAME" | head -n 12 || true
    echo
    info "installed. Logs: journalctl -u $SERVICE_NAME -f"
else
    info "installed. Start it with: $PENNY --config $CONFIG_FILE run"
fi
