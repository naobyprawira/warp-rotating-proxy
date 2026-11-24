#!/usr/bin/env bash
set -euo pipefail

mkdir -p /run/dbus /run/cloudflare-warp /var/lib/cloudflare-warp
rm -f /run/dbus/pid /run/cloudflare-warp/warp_service

dbus-daemon --system --fork

WARP_SVC_BIN="${WARP_SVC_BIN:-$(command -v warp-svc || echo /usr/bin/warp-svc)}"
"$WARP_SVC_BIN" >/tmp/warp-svc.log 2>&1 &
WARP_SVC_PID=$!

cleanup() {
  if [[ -n "${PROXY_PID:-}" ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
  if kill -0 "$WARP_SVC_PID" 2>/dev/null; then
    kill "$WARP_SVC_PID" 2>/dev/null || true
    wait "$WARP_SVC_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT TERM INT

for _ in $(seq 1 30); do
  if [[ -S /run/cloudflare-warp/warp_service ]]; then
    break
  fi
  if ! kill -0 "$WARP_SVC_PID" 2>/dev/null; then
    echo "warp-svc exited early:" >&2
    cat /tmp/warp-svc.log >&2 || true
    exit 1
  fi
  sleep 0.5
done

if [[ ! -S /run/cloudflare-warp/warp_service ]]; then
  echo "Timed out waiting for /run/cloudflare-warp/warp_service" >&2
  cat /tmp/warp-svc.log >&2 || true
  exit 1
fi

python3 -m warp_proxy &
PROXY_PID=$!
wait "$PROXY_PID"
