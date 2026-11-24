"""Configuration and logging for warp-rotating-proxy."""

from __future__ import annotations

import os
import shutil
import sys
import time


def resolve_binary(env_var: str, binary_name: str, extra_candidates: list[str]) -> str:
    """Resolve binary path from env override, common install locations, or PATH."""
    override = os.environ.get(env_var, "").strip()
    if override:
        return os.path.expanduser(override)
    for raw in extra_candidates:
        cand = os.path.expanduser(raw)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    found = shutil.which(binary_name)
    if found:
        return found
    return os.path.expanduser(extra_candidates[0]) if extra_candidates else binary_name


LISTEN_HOST = os.environ.get("WARP_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WARP_PROXY_PORT", "40001"))

WARP_SOCKS_HOST = os.environ.get("WARP_SOCKS_HOST", "127.0.0.1")
WARP_SOCKS_PORT = int(os.environ.get("WARP_SOCKS_PORT", "40000"))
WARP_PROTOCOL = os.environ.get("WARP_PROTOCOL", "MASQUE")
WARP_CLI_BIN = resolve_binary(
    "WARP_CLI_BIN",
    "warp-cli",
    [
        "/usr/bin/warp-cli",
        "/usr/local/bin/warp-cli",
        "/bin/warp-cli",
        "/opt/homebrew/bin/warp-cli",
        "/home/linuxbrew/.linuxbrew/bin/warp-cli",
    ],
)

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
