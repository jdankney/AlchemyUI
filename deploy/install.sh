#!/usr/bin/env bash
# AlchemyUI deployment. Idempotent -- safe to re-run after every `git pull`.
#
# Sets up three systemd *user* units -- the app, its oauth2-proxy auth edge
# (started only once it is configured), and the router's CPU-only fallback
# (started only once .env says where its model is) -- and the CLI.
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
OAUTH2_PROXY_VERSION="v7.6.0"          # Llamacracy's pin

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
chmod 600 "$REPO/.env"          # it can hold ROUTER_PRIMARY_KEY
# A missing key is not an error: grep's non-zero status must not trip set -e.
envval() { { grep -E "^$1=" "$REPO/.env" || true; } | tail -1 | cut -d= -f2- | tr -d ' '; }
PORT="$(envval PORT)"
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
systemctl --user enable alchemyui.service
# restart, not --now: a re-run after `git pull` must serve the new code
systemctl --user restart alchemyui.service

# --- 5b. the router's local fallback -------------------------------------------
# Optional: without it, planning uses Llamacracy alone and falls back to the
# user's own words when Llamacracy is down.
GGUF="$(envval ROUTER_FALLBACK_GGUF)"
MMPROJ="$(envval ROUTER_FALLBACK_MMPROJ)"
LLAMA_SERVER="$(command -v llama-server || true)"
if [ -n "$GGUF" ] && [ -f "$GGUF" ] && [ -n "$MMPROJ" ] && [ -f "$MMPROJ" ] && [ -n "$LLAMA_SERVER" ]; then
  say "installing the router fallback (llama-server on the CPU, $(basename "$GGUF"))"
  sed -e "s#@REPO@#${REPO}#g" -e "s#@LLAMA_SERVER@#${LLAMA_SERVER}#g" \
    "$REPO/deploy/systemd/alchemyui-router.service" > "$UNIT_DIR/alchemyui-router.service"
  systemctl --user daemon-reload
  systemctl --user enable alchemyui-router.service
  systemctl --user restart alchemyui-router.service
else
  warn "router fallback not installed: set ROUTER_FALLBACK_GGUF and ROUTER_FALLBACK_MMPROJ"
  warn "  in .env to a vision GGUF and its mmproj (and have llama-server on PATH), then re-run."
fi

# --- 6. the `alchemyui` CLI --------------------------------------------------
# Symlinked, not copied, so a `git pull` plus a re-run always gives the latest.
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
chmod +x "$REPO/deploy/alchemyui-cli.sh"
ln -sf "$REPO/deploy/alchemyui-cli.sh" "$BIN/alchemyui-ctl"
say "installed: alchemyui-ctl up|down|restart|status|logs  (make sure $BIN is on PATH)"
warn "note: the CLI is 'alchemyui-ctl'; 'alchemyui' itself is the app's own"
warn "      command (alchemyui validate), installed into $REPO/.venv/bin"

# --- 7. auth edge: oauth2-proxy in front, Llamacracy's Dex behind ------------
BIN="$HOME/.local/bin"
if [ ! -x "$BIN/oauth2-proxy" ]; then
  say "installing oauth2-proxy $OAUTH2_PROXY_VERSION"
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l) arch=armv7 ;;
    *) warn "no oauth2-proxy build for $(uname -m); install it to $BIN by hand"; arch="" ;;
  esac
fi
if [ ! -x "$BIN/oauth2-proxy" ] && [ -n "${arch:-}" ]; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/o.tar.gz" \
    "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/${OAUTH2_PROXY_VERSION}/oauth2-proxy-${OAUTH2_PROXY_VERSION}.linux-${arch}.tar.gz"
  tar -xzf "$tmp/o.tar.gz" -C "$tmp" --strip-components=1
  install -m755 "$tmp/oauth2-proxy" "$BIN/oauth2-proxy"
  rm -rf "$tmp"
fi

AUTH_ENV="$REPO/deploy/oauth2-proxy.env"
# NetBird missing or its daemon down: no FQDN, not a failed install.
fqdn="$( { netbird status 2>/dev/null || true; } | awk '/^FQDN:/ {print $2}')"
if [ ! -f "$AUTH_ENV" ]; then
  cp "$REPO/deploy/oauth2-proxy.env.example" "$AUTH_ENV"
  chmod 600 "$AUTH_ENV"
  sed -i "s#^OAUTH2_PROXY_COOKIE_SECRET=.*#OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -base64 32 | tr -- '+/' '-_')#" "$AUTH_ENV"
  sed -i "s#^OAUTH2_PROXY_CLIENT_SECRET=.*#OAUTH2_PROXY_CLIENT_SECRET=$(openssl rand -hex 32)#" "$AUTH_ENV"
  warn "created deploy/oauth2-proxy.env with fresh secrets"
fi
chmod 600 "$AUTH_ENV"           # cookie and client secrets
# Placeholders are filled on every run, so the advice below ("re-run with
# ALCHEMYUI_DEX_FQDN=...") works after the file already exists.
if [ -n "$fqdn" ]; then sed -i "s#REPLACE_FQDN#${fqdn}#g" "$AUTH_ENV"; fi
if [ -n "${ALCHEMYUI_DEX_FQDN:-}" ]; then
  sed -i "s#REPLACE_DEX_FQDN#${ALCHEMYUI_DEX_FQDN}#g" "$AUTH_ENV"
fi
sed -i "s#REPLACE_PORT#${PORT}#g" "$AUTH_ENV"
sed -e "s#@REPO@#${REPO}#g" "$REPO/deploy/systemd/alchemyui-auth.service" \
  > "$UNIT_DIR/alchemyui-auth.service"
systemctl --user daemon-reload

if grep -q 'REPLACE_' "$AUTH_ENV"; then
  warn "deploy/oauth2-proxy.env still has REPLACE_ placeholders -- the auth edge stays off."
  warn "  re-run with ALCHEMYUI_DEX_FQDN=<your Dex host's NetBird FQDN> ./deploy/install.sh,"
  warn "  or edit them by hand, then re-run."
else
  secret="$(grep '^OAUTH2_PROXY_CLIENT_SECRET=' "$AUTH_ENV" | cut -d= -f2-)"
  redirect="$(grep '^OAUTH2_PROXY_REDIRECT_URL=' "$AUTH_ENV" | cut -d= -f2-)"
  say "register AlchemyUI in Llamacracy's Dex (deploy/dex/config.yaml on the Dex host),"
  say "under staticClients, then restart Dex there:"
  cat <<EOF

    - id: alchemyui
      name: AlchemyUI
      secret: ${secret}
      redirectURIs:
        - ${redirect}

EOF
  if [ "${ALCHEMYUI_START_AUTH:-0}" = "1" ]; then
    systemctl --user enable alchemyui-auth.service
    systemctl --user restart alchemyui-auth.service
    say "auth edge up -- friends reach AlchemyUI at  ${redirect%/oauth2/callback}"
  else
    warn "once Dex has the client, start the edge:  ALCHEMYUI_START_AUTH=1 ./deploy/install.sh"
  fi
  if grep -qE '^DEV_MODE=(1|true|yes|on)' "$REPO/.env"; then
    warn "DEV_MODE is still set in .env: behind oauth2-proxy, unset it, or every"
    warn "visitor is treated as the one dev identity."
  fi
fi

# --- 8. sanity ---------------------------------------------------------------
for _ in 1 2 3 4 5 6 7 8; do
  curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/healthz" && break
  sleep 1
done
echo
say "done. Status:  alchemyui-ctl status"
echo "  AlchemyUI is at  http://127.0.0.1:${PORT}"
echo
echo "  Locally: DEV_MODE=1 gives one fake identity, no login."
echo "  For friends over NetBird: the auth edge above, then unset DEV_MODE."
