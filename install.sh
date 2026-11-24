#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${WARP_PROXY_STATE_DIR:-$HOME/.local/share/warp-rotating-proxy}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/warp-rotating-proxy.service"

PYTHON_BIN="${PYTHON_BIN:-}"
WARP_CLI_BIN="${WARP_CLI_BIN:-}"
WARP_PROXY_HOST="${WARP_PROXY_HOST:-127.0.0.1}"
WARP_PROXY_PORT="${WARP_PROXY_PORT:-40001}"
WARP_SOCKS_HOST="${WARP_SOCKS_HOST:-127.0.0.1}"
WARP_SOCKS_PORT="${WARP_SOCKS_PORT:-40000}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

usage() {
  cat <<EOF
Usage: bash install.sh [options]

Options:
  --dest DIR              Install directory (default: $DEST)
  --python PATH           Python 3 binary path (auto-detected)
  --warp-cli PATH         warp-cli binary path (auto-detected)
  --host ADDR             HTTP proxy listen host (default: $WARP_PROXY_HOST)
  --port PORT             HTTP proxy listen port (default: $WARP_PROXY_PORT)
  --socks-host ADDR       WARP SOCKS5 host (default: $WARP_SOCKS_HOST)
  --socks-port PORT       WARP SOCKS5 port (default: $WARP_SOCKS_PORT)
  --non-interactive       Fail instead of prompting if a path is missing
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --warp-cli) WARP_CLI_BIN="$2"; shift 2 ;;
    --host) WARP_PROXY_HOST="$2"; shift 2 ;;
    --port) WARP_PROXY_PORT="$2"; shift 2 ;;
    --socks-host) WARP_SOCKS_HOST="$2"; shift 2 ;;
    --socks-port) WARP_SOCKS_PORT="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

detect_or_prompt() {
  local label="$1"
  local flag_hint="$2"
  local env_hint="$3"
  local current="$4"
  shift 4

  if [[ -n "$current" && -x "$current" ]]; then
    echo "$current"
    return 0
  fi

  local cand
  for cand in "$@"; do
    [[ -n "$cand" ]] || continue
    if [[ -x "$cand" ]]; then
      echo "$cand"
      return 0
    fi
  done

  if [[ "$NON_INTERACTIVE" != "1" && -t 0 ]]; then
    local entered=""
    while true; do
      read -rp "$label not auto-detected. Enter full path to $label: " entered
      entered="${entered/#\~/$HOME}"
      if [[ -n "$entered" && -x "$entered" ]]; then
        echo "$entered"
        return 0
      fi
      echo "Executable not found at '$entered'. Try again." >&2
    done
  fi

  echo "Error: $label not found. Pass --${flag_hint} or set ${env_hint}." >&2
  return 1
}

PYTHON_BIN="$(detect_or_prompt "python3" "python" "PYTHON_BIN" "$PYTHON_BIN" \
  "$(command -v python3 2>/dev/null || true)" \
  "/usr/bin/python3" "/usr/local/bin/python3")"

WARP_CLI_BIN="$(detect_or_prompt "warp-cli" "warp-cli" "WARP_CLI_BIN" "$WARP_CLI_BIN" \
  "$(command -v warp-cli 2>/dev/null || true)" \
  "/usr/bin/warp-cli" "/usr/local/bin/warp-cli" "/bin/warp-cli" "/opt/homebrew/bin/warp-cli")"

mkdir -p "$DEST/src/warp_proxy" "$UNIT_DIR"

rm -f \
  "$DEST/proxy_config.py" \
  "$DEST/socks5_tunnel.py" \
  "$DEST/warp_rotator.py" \
  "$DEST/warp_rotating_proxy.py"

for f in \
  __init__.py \
  __main__.py \
  config.py \
  rotator.py \
  server.py \
  socks5.py; do
  install -m 644 "$REPO_DIR/src/warp_proxy/$f" "$DEST/src/warp_proxy/$f"
done

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Cloudflare WARP Rotating HTTP Proxy
After=network-online.target warp-svc.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN -m warp_proxy
Environment=PYTHONPATH=$DEST/src
Environment=WARP_PROXY_HOST=$WARP_PROXY_HOST
Environment=WARP_PROXY_PORT=$WARP_PROXY_PORT
Environment=WARP_SOCKS_HOST=$WARP_SOCKS_HOST
Environment=WARP_SOCKS_PORT=$WARP_SOCKS_PORT
Environment=WARP_CLI_BIN=$WARP_CLI_BIN
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable warp-rotating-proxy.service
systemctl --user restart warp-rotating-proxy.service

echo "Installed warp-rotating-proxy to $DEST"
echo "  python3 : $PYTHON_BIN"
echo "  warp-cli: $WARP_CLI_BIN"
echo "  listen  : http://$WARP_PROXY_HOST:$WARP_PROXY_PORT"
systemctl --user --no-pager status warp-rotating-proxy.service || true
