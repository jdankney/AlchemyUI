#!/usr/bin/env bash
# AlchemyUI control. Installed on PATH as `alchemyui-ctl` by deploy/install.sh
# (symlinked there, so `git pull` + a re-run always gives you the latest).
#
#   alchemyui-ctl up        start the app
#   alchemyui-ctl down      stop it
#   alchemyui-ctl restart   restart it
#   alchemyui-ctl status    unit state + a reachability check on both ends
#   alchemyui-ctl logs      follow the journal
#
# Deliberately one unit, not Llamacracy's four: there is no auth edge and no
# model server to supervise here. ComfyUI keeps its own lifetime.
set -euo pipefail

UNIT=alchemyui.service
REPO="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
PORT="$(grep -E '^PORT=' "$REPO/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ' || true)"
PORT="${PORT:-8300}"
COMFY="$(grep -E '^COMFYUI_URL=' "$REPO/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' ' || true)"
COMFY="${COMFY:-http://127.0.0.1:8188}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

usage() {
  cat <<EOF
AlchemyUI control.

Usage: alchemyui-ctl <command>

  up                start the app
  down              stop the app
  restart           restart the app
  status            unit state, plus whether the app and ComfyUI answer
  logs              follow the journal
  validate          re-check the workflow registry against its templates

Unit: $UNIT
EOF
}

cmd_status() {
  local state
  state="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
  if [ "$state" = "active" ]; then
    printf '  \033[1;32m●\033[0m %-20s active\n' "$UNIT"
  else
    printf '  \033[1;31m●\033[0m %-20s %s\n' "$UNIT" "${state:-unknown}"
  fi
  if curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:${PORT}/healthz"; then
    echo "  app:     ok    http://127.0.0.1:${PORT}"
  else
    warn "app did not answer on ${PORT}"
  fi
  # ComfyUI is not ours to start, but a red light here explains most failures.
  if curl -sf -o /dev/null --max-time 3 "${COMFY}/system_stats"; then
    echo "  comfyui: ok    ${COMFY}"
  else
    warn "ComfyUI is not answering at ${COMFY} -- nothing will generate until it is"
  fi
}

case "${1:-}" in
  up) say "starting $UNIT"; systemctl --user start "$UNIT"; sleep 2; cmd_status ;;
  down) say "stopping $UNIT"; systemctl --user stop "$UNIT"; cmd_status ;;
  restart) say "restarting $UNIT"; systemctl --user restart "$UNIT"; sleep 2; cmd_status ;;
  status) cmd_status ;;
  logs) exec journalctl --user -u "$UNIT" -f ;;
  validate) exec "$REPO/.venv/bin/alchemyui" validate "${@:2}" ;;
  -h|--help|help|"") usage ;;
  *) echo "unknown command: $1" >&2; usage; exit 1 ;;
esac
