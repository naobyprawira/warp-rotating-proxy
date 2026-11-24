"""Configuration and logging for warp-rotating-proxy."""

from __future__ import annotations

import os
import sys
import time


LISTEN_HOST = os.environ.get("WARP_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WARP_PROXY_PORT", "40001"))

WARP_SOCKS_HOST = os.environ.get("WARP_SOCKS_HOST", "127.0.0.1")
WARP_SOCKS_PORT = int(os.environ.get("WARP_SOCKS_PORT", "40000"))
WARP_PROTOCOL = os.environ.get("WARP_PROTOCOL", "MASQUE")
WARP_CLI_BIN = os.environ.get("WARP_CLI_BIN", "warp-cli")

ROTATE_COOLDOWN_SEC = float(os.environ.get("WARP_ROTATE_COOLDOWN", "15"))
BAD_PREFIX_TTL_SEC = float(os.environ.get("WARP_BAD_PREFIX_TTL", "1800"))
CONNECT_TIMEOUT_SEC = float(os.environ.get("WARP_CONNECT_TIMEOUT", "15"))

TRACE_URL = os.environ.get("WARP_TRACE_URL", "https://cloudflare.com/cdn-cgi/trace")
PAUSE_FILE = os.environ.get("WARP_PAUSE_FILE", "").strip()
ROTATE_TOKEN = os.environ.get("WARP_ROTATE_TOKEN", "").strip()


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] {msg}\n")
    sys.stderr.flush()
