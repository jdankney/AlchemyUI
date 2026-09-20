#!/usr/bin/env bash
# AlchemyUI deployment. Idempotent -- safe to re-run after every `git pull`.
#
# Sets up one systemd *user* unit and the `alchemyui` CLI that controls it.
# Scaffolds .env from the committed example. Nothing machine-specific is
# committed; this script is what fills in this checkout's paths.
#
#   ./deploy/install.sh
#
# ComfyUI itself is NOT managed here. It has its own launch flags and its own
# lifetime, and pretending otherwise would make this script the wrong place to
# debug a GPU problem.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

# --- 1. linger, so the unit runs without an active login session -------------
if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  say "enabling linger (needs sudo once)"
  sudo loginctl enable-linger "$USER"
fi

# --- 2. python env -----------------------------------------------------------
say "syncing python env"
( cd "$REPO" && uv sync --frozen )

# --- 3. local config (gitignored) --------------------------------------------
if [ ! -f "$REPO/.env" ]; then
  cp "$REPO/.env.example" "$REPO/.env"
  warn "created .env from the example -- check COMFYUI_URL and PORT"
fi
# shellcheck disable=SC1090
PORT="$(grep -E '^PORT=' "$REPO/.env" | tail -1 | cut -d= -f2 | tr -d ' ')"
PORT="${PORT:-8300}"

# --- 4. the workflow registry must be valid before we start anything ---------
# Node IDs drift when a graph is rebuilt in the ComfyUI editor. Catching that
# here is the whole point of shipping the check as a command.
say "validating the workflow registry"
( cd "$REPO" && ./.venv/bin/alchemyui validate )

# --- 5. unit -----------------------------------------------------------------
say "installing the systemd user unit (repo = $REPO, port = $PORT)"
mkdir -p "$UNIT_DIR"
sed -e "s#@REPO@#${REPO}#g" -e "s#@PORT@#${PORT}#g" \
  "$REPO/deploy/systemd/alchemyui.service" > "$UNIT_DIR/alchemyui.service"
systemctl --user daemon-reload
systemctl --user enable --now alchemyui.service

# --- 6. the `alchemyui` CLI --------------------------------------------------
# Symlinked, not copied, so a `git pull` plus a re-run always gives the latest.
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
chmod +x "$REPO/deploy/alchemyui-cli.sh"
ln -sf "$REPO/deploy/alchemyui-cli.sh" "$BIN/alchemyui-ctl"
say "installed: alchemyui-ctl up|down|restart|status|logs  (make sure $BIN is on PATH)"
warn "note: the CLI is 'alchemyui-ctl'; 'alchemyui' itself is the app's own"
warn "      command (alchemyui validate), installed into $REPO/.venv/bin"

# --- 7. sanity ---------------------------------------------------------------
for _ in 1 2 3 4 5 6 7 8; do
  curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/healthz" && break
  sleep 1
done
echo
say "done. Status:  alchemyui-ctl status"
echo "  AlchemyUI is at  http://127.0.0.1:${PORT}"
echo
echo "  Single user, bound to localhost, DEV_MODE=1 -- there is no auth."
echo "  To share over NetBird: register a second client in Llamacracy's Dex,"
echo "  run an oauth2-proxy in front, and unset DEV_MODE (see docs/DECISIONS.md)."
