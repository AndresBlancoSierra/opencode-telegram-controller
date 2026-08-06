#!/usr/bin/env bash
# Installs the OpenCode Telegram Controller as a systemd user service.
#
# Steps performed:
#   1. Installs deploy/opencode-telegram-controller.service into
#      ~/.config/systemd/user/.
#   2. Creates ~/.config/opencode-telegram-controller/.env from the project
#      .env file if it does not already exist (secrets stay out of the repo).
#   3. Enables and starts the service.
#
# Usage:
#   scripts/install-service.sh
#
# Prerequisites:
#   - .venv exists in the project root (run: uv sync)
#   - config/projects.yaml exists (copy from config/projects.yaml.example)
#   - OTC_TELEGRAM_BOT_TOKEN and OTC_ALLOWED_USER_IDS are configured

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="opencode-telegram-controller"
UNIT_SRC="$PROJECT_DIR/deploy/$SERVICE_NAME.service"
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/$SERVICE_NAME"
UNIT_DST="$UNIT_DIR/$SERVICE_NAME.service"

if [[ ! -f "$PROJECT_DIR/.venv/bin/otc" ]]; then
    echo "ERROR: $PROJECT_DIR/.venv/bin/otc not found." >&2
    echo "       Run 'uv sync' in the project first." >&2
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/config/projects.yaml" ]]; then
    echo "ERROR: config/projects.yaml not found." >&2
    echo "       Copy config/projects.yaml.example to config/projects.yaml and adjust." >&2
    exit 1
fi

mkdir -p "$UNIT_DIR" "$CONFIG_DIR"

echo "Installing unit file to $UNIT_DST"
cp "$UNIT_SRC" "$UNIT_DST"

if [[ ! -f "$CONFIG_DIR/.env" ]]; then
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        echo "Creating $CONFIG_DIR/.env from project .env"
        cp "$PROJECT_DIR/.env" "$CONFIG_DIR/.env"
    else
        echo "NOTE: no .env found; $CONFIG_DIR/.env not created."
        echo "      Configure OTC_TELEGRAM_BOT_TOKEN and OTC_ALLOWED_USER_IDS there."
    fi
else
    echo "Keeping existing $CONFIG_DIR/.env"
fi

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
systemctl --user --no-pager status "$SERVICE_NAME" || true

echo ""
echo "Service installed. Useful commands:"
echo "  systemctl --user status $SERVICE_NAME"
echo "  systemctl --user restart $SERVICE_NAME"
echo "  journalctl --user -u $SERVICE_NAME -f"
