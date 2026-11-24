"""Cloudflare WARP MASQUE proxy supervision and IPv6 /64 rotation."""

from __future__ import annotations

import os
import ssl
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from warp_proxy.config import (
    BAD_PREFIX_TTL_SEC,
    PAUSE_FILE,
    ROTATE_COOLDOWN_SEC,
    TRACE_URL,
    WARP_CLI_BIN,
    WARP_PROTOCOL,
    WARP_SOCKS_HOST,
    WARP_SOCKS_PORT,
    log,
)
from warp_proxy.socks5 import open_socks5_tunnel


def ipv6_prefix64(ip: str) -> str:
    return ":".join(ip.split(":")[:4]) if ":" in ip else ip


class WarpRotator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_cond = threading.Condition()
        self._rotating = False
        self._last_rotate_ts: float = 0.0
        self._bad_prefixes: dict[str, float] = {}

    def _set_rotating(self, active: bool) -> None:
        with self._state_cond:
            self._rotating = active
            if not active:
                self._state_cond.notify_all()

    def is_rotating(self) -> bool:
        with self._state_cond:
            return self._rotating

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._state_cond:
            while self._rotating:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_cond.wait(remaining)
        return True

    def _run_cli(self, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [WARP_CLI_BIN, "--accept-tos", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _prune_bad_prefixes(self) -> None:
        now = time.monotonic()
        expired = [p for p, exp in self._bad_prefixes.items() if exp <= now]
        for p in expired:
            self._bad_prefixes.pop(p, None)

    def mark_bad_prefix(self, prefix: str) -> None:
        if prefix:
            self._bad_prefixes[prefix] = time.monotonic() + BAD_PREFIX_TTL_SEC

    def trace_egress_ip(self) -> str:
        sock = None
        try:
            parsed = urlsplit(TRACE_URL)
            scheme = (parsed.scheme or "https").lower()
            host = parsed.hostname or "cloudflare.com"
            default_port = 443 if scheme == "https" else 80
            port = parsed.port or default_port
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            host_header = host if port == default_port else f"{host}:{port}"

            raw_sock = open_socks5_tunnel(
                WARP_SOCKS_HOST, WARP_SOCKS_PORT, host, port, timeout=8.0
            )
            raw_sock.settimeout(8.0)
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(
                    raw_sock, server_hostname=host
                )
            else:
                sock = raw_sock

            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "User-Agent: warp-rotating-proxy\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(req)

            chunks: list[bytes] = []
            total = 0
            while total < 65536:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                total += len(data)

            raw_resp = b"".join(chunks).decode("utf-8", errors="replace")
            body = raw_resp.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw_resp else raw_resp
            for line in body.splitlines():
                if line.startswith("ip="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        return ""

    def is_registered(self) -> bool:
        try:
            r = self._run_cli("registration", "show", timeout=5.0)
            return r.returncode == 0 and (
                "Device ID:" in r.stdout or "Account type:" in r.stdout
            )
        except Exception:
            return False

    def is_connected(self) -> bool:
        try:
            st = self._run_cli("status", timeout=5.0).stdout
            return "Connected" in st
        except Exception:
            return False

    def status(self, include_ip: bool = True) -> dict[str, Any]:
        self._prune_bad_prefixes()
        connected = self.is_connected()
        ip = self.trace_egress_ip() if (connected and include_ip) else ""
        return {
            "warp_connected": connected,
            "rotating": self.is_rotating(),
            "socks_upstream": f"socks5h://{WARP_SOCKS_HOST}:{WARP_SOCKS_PORT}",
            "protocol": WARP_PROTOCOL,
            "egress_ip": ip or None,
            "prefix64": ipv6_prefix64(ip) if ip else None,
            "bad_prefixes_count": len(self._bad_prefixes),
        }

    def ensure_connected(self) -> bool:
        if self.is_connected():
            return True
        with self._lock:
            if self.is_connected():
                return True
            self._set_rotating(True)
            try:
                if not self.is_registered():
                    log("WARP not registered; registering new device...")
                    self._run_cli("registration", "new")
                    time.sleep(1.0)
                log("WARP not connected; configuring proxy mode and connecting...")
                self._run_cli("tunnel", "protocol", "set", WARP_PROTOCOL)
                self._run_cli("mode", "proxy")
                self._run_cli("proxy", "port", str(WARP_SOCKS_PORT))
                self._run_cli("connect")
                for _ in range(12):
                    time.sleep(1.0)
                    if self.is_connected():
                        return True
                return False
            finally:
                self._set_rotating(False)

    def _wait_connected_ip(self) -> str:
        for _ in range(12):
            time.sleep(1.5)
            if self.is_connected():
                cand = self.trace_egress_ip()
                if cand:
                    return cand
        return ""

    def rotate(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            self._prune_bad_prefixes()
            now = time.monotonic()
            if (
                not force
                and self._last_rotate_ts > 0
                and (now - self._last_rotate_ts) < ROTATE_COOLDOWN_SEC
            ):
                cur_ip = self.trace_egress_ip()
                return {
                    "ok": bool(cur_ip),
                    "rotated": False,
                    "cooldown": True,
                    "old_ip": cur_ip or None,
                    "new_ip": cur_ip or None,
                }

            old_ip = self.trace_egress_ip()
            old_p64 = ipv6_prefix64(old_ip)
            if old_p64:
                self.mark_bad_prefix(old_p64)
            log(f"Rotating WARP identity (old={old_ip or '?'})...")

            if PAUSE_FILE:
                try:
                    os.makedirs(os.path.dirname(PAUSE_FILE), exist_ok=True)
                    open(PAUSE_FILE, "w").close()
                except OSError:
                    pass

            new_ip = ""
            self._set_rotating(True)
            try:
                for step in range(3):
                    self._run_cli("disconnect")
                    if step == 0:
                        self._run_cli("tunnel", "rotate-keys")
                    else:
                        log(f"Performing WARP registration reset (step={step})")
                        self._run_cli("registration", "delete")
                        time.sleep(1.0)
                        self._run_cli("registration", "new")
                    self._run_cli("tunnel", "protocol", "set", WARP_PROTOCOL)
                    self._run_cli("mode", "proxy")
                    self._run_cli("proxy", "port", str(WARP_SOCKS_PORT))
                    self._run_cli("connect")
                    cand = self._wait_connected_ip()
                    if cand:
                        new_ip = cand
                        cand_p64 = ipv6_prefix64(cand)
                        if cand_p64 != old_p64 and cand_p64 not in self._bad_prefixes:
                            break
                        log(f"WARP returned spent/same /64 ({cand_p64}), retrying...")
            finally:
                self._set_rotating(False)
                if PAUSE_FILE:
                    try:
                        if os.path.exists(PAUSE_FILE):
                            os.remove(PAUSE_FILE)
                    except OSError:
                        pass

            self._last_rotate_ts = time.monotonic()
            log(f"WARP rotation complete (old={old_ip or '?'} -> new={new_ip or '?'})")
            return {
                "ok": bool(new_ip),
                "rotated": bool(new_ip and new_ip != old_ip),
                "cooldown": False,
                "old_ip": old_ip or None,
                "new_ip": new_ip or None,
            }


ROTATOR = WarpRotator()
