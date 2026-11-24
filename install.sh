#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${WARP_PROXY_STATE_DIR:-$HOME/.local/share/warp-rotating-proxy}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/warp-rotating-proxy.service"

mkdir -p "$DEST" "$UNIT_DIR"

for f in \
  proxy_config.py \
  socks5_tunnel.py \
  warp_rotator.py \
  warp_rotating_proxy.py; do
  install -m 755 "$REPO_DIR/$f" "$DEST/$f"
done

sed "s|%h/.local/share/warp-rotating-proxy|$DEST|g" \
  "$REPO_DIR/systemd/warp-rotating-proxy.service" > "$UNIT_FILE"

systemctl --user daemon-reload
systemctl --user enable warp-rotating-proxy.service
systemctl --user restart warp-rotating-proxy.service

echo "Installed warp-rotating-proxy to $DEST"
systemctl --user --no-pager status warp-rotating-proxy.service || true
