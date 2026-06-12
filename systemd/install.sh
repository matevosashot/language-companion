#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_NAME="german-teacher-bot.service"
UNIT_SRC="$SCRIPT_DIR/${UNIT_NAME}.in"
UNIT_DST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DST_DIR/$UNIT_NAME"

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
    echo "ERROR: $REPO/.venv/bin/python not found. Create a venv and install requirements.txt first." >&2
    exit 1
fi
if [[ ! -f "$REPO/german-teacher-bot.py" ]]; then
    echo "ERROR: $REPO/german-teacher-bot.py not found." >&2
    exit 1
fi
if [[ ! -f "$REPO/.env" ]]; then
    echo "WARNING: $REPO/.env not found — the bot will fail to start without the bot token." >&2
fi

mkdir -p "$UNIT_DST_DIR"
sed "s|@REPO@|$REPO|g" "$UNIT_SRC" > "$UNIT_DST"
echo "Installed $UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo
systemctl --user --no-pager status "$UNIT_NAME" || true

echo
echo "Logs:    journalctl --user -u $UNIT_NAME -f"
echo "Stop:    systemctl --user stop $UNIT_NAME"
echo "Disable: systemctl --user disable --now $UNIT_NAME"

if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    echo
    echo "NOTE: linger is not enabled — the service stops when you log out."
    echo "      To keep it running across logouts, run:"
    echo "          sudo loginctl enable-linger $USER"
fi
