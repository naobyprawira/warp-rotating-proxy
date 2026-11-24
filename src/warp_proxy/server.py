#!/usr/bin/env python3
"""Standalone HTTP CONNECT & forward proxy over rotating Cloudflare WARP.

Exposes:
  - CONNECT host:port       (HTTPS / TLS tunnel through WARP SOCKS5)
  - GET/POST/... http://... (Plain HTTP forward proxy through WARP SOCKS5)
  - GET  /health | /_health (Health status JSON)
  - GET  /_status           (WARP connection & current egress IP JSON)
  - POST /_rotate           (Trigger WARP IP rotation; optional ?force=1)
"""

from __future__ import annotations

import base64
import json
import secrets
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from warp_proxy.config import (
    CONNECT_TIMEOUT_SEC,
    LISTEN_HOST,
    LISTEN_PORT,
    PROXY_PASS,
    PROXY_USER,
    ROTATE_TOKEN,
    WARP_SOCKS_HOST,
    WARP_SOCKS_PORT,
    log,
)
from warp_proxy.rotator import ROTATOR
from warp_proxy.socks5 import Socks5Error, open_socks5_tunnel, relay_sockets


def parse_connect_authority(target: str) -> tuple[str, int]:
    target = target.strip()
    if target.startswith("["):
        end = target.find("]")
        if end == -1:
            raise ValueError(f"Invalid IPv6 CONNECT target: {target}")
        host = target[1:end]
        rest = target[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") else 443
        return host, port
    if ":" in target:
        host, port_s = target.rsplit(":", 1)
        return host, int(port_s)
    return target, 443


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        log(f"{self.address_string()} - {format % args}")

    def _send_json(self, code: int, payload: Any) -> None:
        self.close_connection = True
        raw = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _rotate_authorized(self) -> bool:
        if not ROTATE_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            if secrets.compare_digest(auth[7:].strip(), ROTATE_TOKEN):
                return True
        xkey = self.headers.get("X-API-Key", "").strip()
        if xkey and secrets.compare_digest(xkey, ROTATE_TOKEN):
            return True
        return False

    def _proxy_authorized(self) -> bool:
        if not (PROXY_USER or PROXY_PASS):
            return True
        header = self.headers.get("Proxy-Authorization", "")
        if not header.startswith("Basic "):
            return False
        token = header[6:].strip()
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except Exception:
            return False
        if ":" not in decoded:
            return False
        user, pw = decoded.split(":", 1)
        return secrets.compare_digest(user, PROXY_USER) and secrets.compare_digest(
            pw, PROXY_PASS
        )

    def _send_proxy_auth_required(self) -> None:
        self.close_connection = True
        body = b'{"error":"Proxy authentication required"}'
        try:
            self.send_response(407, "Proxy Authentication Required")
            self.send_header("Proxy-Authenticate", 'Basic realm="warp-rotating-proxy"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_CONNECT(self) -> None:
        if not self._proxy_authorized():
            self._send_proxy_auth_required()
            return
        try:
            host, port = parse_connect_authority(self.path)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        try:
            remote = open_socks5_tunnel(
                WARP_SOCKS_HOST,
                WARP_SOCKS_PORT,
                host,
                port,
                timeout=CONNECT_TIMEOUT_SEC,
            )
        except (Socks5Error, OSError) as exc:
            self._send_json(502, {"error": f"WARP SOCKS5 tunnel error: {exc}"})
            return

        self.close_connection = True
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
        except OSError:
            remote.close()
            return

        relay_sockets(self.connection, remote)

    def _handle_control_get(self, path: str) -> bool:
        if path in ("/health", "/_health"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "listen": f"{LISTEN_HOST}:{LISTEN_PORT}",
                    "socks_upstream": f"{WARP_SOCKS_HOST}:{WARP_SOCKS_PORT}",
                    "warp_connected": ROTATOR.is_connected(),
                    "proxy_auth_enabled": bool(PROXY_USER or PROXY_PASS),
                    "rotate_auth_enabled": bool(ROTATE_TOKEN),
                },
            )
            return True
        if path == "/_status":
            self._send_json(200, ROTATOR.status(include_ip=True))
            return True
        return False

    def _forward_plain_http(self, method: str) -> None:
        if not self._proxy_authorized():
            self._send_proxy_auth_required()
            return
        parsed = urlsplit(self.path)
        if parsed.scheme not in ("http",) or not parsed.hostname:
            self._send_json(404, {"error": f"Unknown path or scheme: {self.path}"})
            return

        host = parsed.hostname
        port = parsed.port or 80
        req_path = parsed.path or "/"
        if parsed.query:
            req_path += "?" + parsed.query

        length = 0
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""

        try:
            remote = open_socks5_tunnel(
                WARP_SOCKS_HOST,
                WARP_SOCKS_PORT,
                host,
                port,
                timeout=CONNECT_TIMEOUT_SEC,
            )
        except (Socks5Error, OSError) as exc:
            self._send_json(502, {"error": f"WARP SOCKS5 forward error: {exc}"})
            return

        self.close_connection = True
        try:
            lines = [f"{method} {req_path} HTTP/1.1"]
            has_host = False
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in ("proxy-authorization", "proxy-connection", "connection"):
                    continue
                if lk == "host":
                    has_host = True
                lines.append(f"{k}: {v}")
            if not has_host:
                lines.append(f"Host: {parsed.netloc}")
            lines.append("Connection: close")
            head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", errors="replace")
            remote.sendall(head + body)

            while True:
                chunk = remote.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                remote.close()
            except OSError:
                pass

    def _handle_method(self, method: str) -> None:
        if self.path.startswith("/"):
            path, _, query = self.path.partition("?")
            if method == "GET" and self._handle_control_get(path):
                return
            if method == "POST" and path == "/_rotate":
                if not self._rotate_authorized():
                    self._send_json(401, {"error": "Unauthorized"})
                    return
                qs = parse_qs(query)
                force = qs.get("force", [""])[0].lower() in ("1", "true", "yes")
                res = ROTATOR.rotate(force=force)
                self._send_json(200 if res["ok"] else 503, res)
                return
            self._send_json(404, {"error": f"Unknown control endpoint: {path}"})
            return

        self._forward_plain_http(method)

    def do_GET(self) -> None:
        self._handle_method("GET")

    def do_POST(self) -> None:
        self._handle_method("POST")

    def do_PUT(self) -> None:
        self._handle_method("PUT")

    def do_DELETE(self) -> None:
        self._handle_method("DELETE")

    def do_HEAD(self) -> None:
        self._handle_method("HEAD")

    def do_OPTIONS(self) -> None:
        self._handle_method("OPTIONS")


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    ROTATOR.ensure_connected()
    server = ReusableThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)

    def handle_sig(signum: int, _frame: Any) -> None:
        log(f"Received signal {signum}, shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    log(
        f"WARP rotating HTTP proxy listening on http://{LISTEN_HOST}:{LISTEN_PORT} "
        f"(upstream socks5h://{WARP_SOCKS_HOST}:{WARP_SOCKS_PORT}, "
        f"proxy_auth={'on' if (PROXY_USER or PROXY_PASS) else 'off'}, "
        f"rotate_auth={'on' if ROTATE_TOKEN else 'off'})"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
