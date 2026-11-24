"""Unit and loopback integration tests for SOCKS5 tunneling and HTTP proxy routes."""

from __future__ import annotations

import base64
import http.client
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from warp_proxy import server as wp
from warp_proxy import socks5 as s5


def _start_echo_server() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def run() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=_handle_echo, args=(conn,), daemon=True).start()

    def _handle_echo(conn: socket.socket) -> None:
        try:
            data = conn.recv(4096)
            if data:
                conn.sendall(b"ECHO:" + data)
        finally:
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return srv, port


def _start_mock_socks5(target_port: int) -> tuple[socket.socket, int, list[tuple[str, int]]]:
    seen_targets: list[tuple[str, int]] = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def run() -> None:
        while True:
            try:
                cli, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=_handle_cli, args=(cli,), daemon=True).start()

    def _handle_cli(cli: socket.socket) -> None:
        try:
            ver, nmethods = cli.recv(2)
            cli.recv(nmethods)
            cli.sendall(b"\x05\x00")
            hdr = cli.recv(4)
            atyp = hdr[3]
            if atyp == 3:
                dlen = cli.recv(1)[0]
                host = cli.recv(dlen).decode("utf-8")
            else:
                host = "127.0.0.1"
            dport = struct.unpack("!H", cli.recv(2))[0]
            seen_targets.append((host, dport))
            remote = socket.create_connection(("127.0.0.1", target_port), timeout=5)
            cli.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + struct.pack("!H", target_port))
            s5.relay_sockets(cli, remote)
        except OSError:
            try:
                cli.close()
            except OSError:
                pass

    threading.Thread(target=run, daemon=True).start()
    return srv, port, seen_targets


class TestProxy(unittest.TestCase):
    def test_parse_connect_authority(self) -> None:
        self.assertEqual(wp.parse_connect_authority("opencode.ai:443"), ("opencode.ai", 443))
        self.assertEqual(wp.parse_connect_authority("example.com"), ("example.com", 443))
        self.assertEqual(wp.parse_connect_authority("[2a09:bac5::1]:8443"), ("2a09:bac5::1", 8443))

    def test_http_connect_tunnels_through_socks5_and_control_endpoints(self) -> None:
        echo_srv, echo_port = _start_echo_server()
        socks_srv, socks_port, seen_targets = _start_mock_socks5(echo_port)
        proxy_srv = wp.ReusableThreadingHTTPServer(("127.0.0.1", 0), wp.ProxyHandler)
        proxy_port = proxy_srv.server_address[1]
        threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()

        try:
            with patch.object(wp, "WARP_SOCKS_HOST", "127.0.0.1"), patch.object(
                wp, "WARP_SOCKS_PORT", socks_port
            ), patch.object(wp.ROTATOR, "is_connected", return_value=True), patch.object(
                wp.ROTATOR,
                "rotate",
                return_value={"ok": True, "rotated": True, "old_ip": "1.1.1.1", "new_ip": "2.2.2.2"},
            ) as mock_rot:
                # 1. Test /health
                c1 = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
                c1.request("GET", "/health")
                r1 = c1.getresponse()
                d1 = json.loads(r1.read().decode())
                c1.close()
                self.assertEqual(r1.status, 200)
                self.assertTrue(d1["warp_connected"])
                self.assertFalse(d1["proxy_auth_enabled"])
                self.assertFalse(d1["rotate_auth_enabled"])

                # 2. Test POST /_rotate?force=1
                c2 = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
                c2.request("POST", "/_rotate?force=1")
                r2 = c2.getresponse()
                d2 = json.loads(r2.read().decode())
                c2.close()
                self.assertEqual(r2.status, 200)
                self.assertTrue(d2["rotated"])
                mock_rot.assert_called_once_with(force=True)

                # 3. Test HTTP CONNECT tunnel end-to-end
                raw = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
                raw.sendall(b"CONNECT mock.opencode.ai:443 HTTP/1.1\r\nHost: mock.opencode.ai:443\r\n\r\n")
                resp_hdr = raw.recv(4096)
                self.assertIn(b"200 Connection Established", resp_hdr)
                raw.sendall(b"ping-warp")
                echoed = raw.recv(4096)
                raw.close()
                self.assertEqual(echoed, b"ECHO:ping-warp")
                self.assertEqual(seen_targets, [("mock.opencode.ai", 443)])
        finally:
            proxy_srv.shutdown()
            proxy_srv.server_close()
            socks_srv.close()
            echo_srv.close()

    def test_proxy_auth_and_rotate_x_api_key_and_plain_http_strip(self) -> None:
        echo_srv, echo_port = _start_echo_server()
        socks_srv, socks_port, seen_targets = _start_mock_socks5(echo_port)
        proxy_srv = wp.ReusableThreadingHTTPServer(("127.0.0.1", 0), wp.ProxyHandler)
        proxy_port = proxy_srv.server_address[1]
        threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()

        good_creds = base64.b64encode(b"warpuser:warppass").decode("ascii")
        bad_creds = base64.b64encode(b"warpuser:wrong").decode("ascii")

        try:
            with patch.object(wp, "WARP_SOCKS_HOST", "127.0.0.1"), patch.object(
                wp, "WARP_SOCKS_PORT", socks_port
            ), patch.object(wp, "PROXY_USER", "warpuser"), patch.object(
                wp, "PROXY_PASS", "warppass"
            ), patch.object(
                wp, "ROTATE_TOKEN", "rot-secret"
            ), patch.object(
                wp.ROTATOR, "is_connected", return_value=True
            ), patch.object(
                wp.ROTATOR,
                "rotate",
                return_value={"ok": True, "rotated": True, "old_ip": "1.1.1.1", "new_ip": "2.2.2.2"},
            ) as mock_rot:
                # 1. /health remains accessible and reports auth flags enabled
                c_health = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
                c_health.request("GET", "/health")
                r_health = c_health.getresponse()
                d_health = json.loads(r_health.read().decode())
                c_health.close()
                self.assertEqual(r_health.status, 200)
                self.assertTrue(d_health["proxy_auth_enabled"])
                self.assertTrue(d_health["rotate_auth_enabled"])

                # 2. CONNECT without Proxy-Authorization -> 407
                raw_noauth = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
                raw_noauth.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
                resp_noauth = raw_noauth.recv(4096)
                raw_noauth.close()
                self.assertIn(b"407 Proxy Authentication Required", resp_noauth)
                self.assertIn(b"Proxy-Authenticate: Basic", resp_noauth)

                # 3. CONNECT with wrong credentials -> 407
                raw_bad = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
                raw_bad.sendall(
                    f"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: Basic {bad_creds}\r\n\r\n".encode(
                        "ascii"
                    )
                )
                resp_bad = raw_bad.recv(4096)
                raw_bad.close()
                self.assertIn(b"407 Proxy Authentication Required", resp_bad)

                # 4. CONNECT with valid credentials -> 200
                raw_ok = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
                raw_ok.sendall(
                    f"CONNECT auth.example.com:443 HTTP/1.1\r\nHost: auth.example.com:443\r\nProxy-Authorization: Basic {good_creds}\r\n\r\n".encode(
                        "ascii"
                    )
                )
                resp_ok = raw_ok.recv(4096)
                self.assertIn(b"200 Connection Established", resp_ok)
                raw_ok.sendall(b"secret-ping")
                self.assertEqual(raw_ok.recv(4096), b"ECHO:secret-ping")
                raw_ok.close()

                # 5. Plain HTTP forward without auth -> 407; with valid auth -> strips Proxy-Authorization
                c_plain_noauth = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
                c_plain_noauth.request("GET", "http://plain.example.com/test")
                r_plain_noauth = c_plain_noauth.getresponse()
                r_plain_noauth.read()
                c_plain_noauth.close()
                self.assertEqual(r_plain_noauth.status, 407)

                raw_plain = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
                raw_plain.sendall(
                    (
                        f"GET http://plain.example.com/hello HTTP/1.1\r\n"
                        f"Host: plain.example.com\r\n"
                        f"Proxy-Authorization: Basic {good_creds}\r\n"
                        f"X-Custom: keep-me\r\n\r\n"
                    ).encode("ascii")
                )
                echoed_plain = raw_plain.recv(4096)
                raw_plain.close()
                self.assertTrue(echoed_plain.startswith(b"ECHO:GET /hello HTTP/1.1"))
                self.assertNotIn(b"Proxy-Authorization", echoed_plain)
                self.assertIn(b"X-Custom: keep-me", echoed_plain)
                self.assertEqual(
                    seen_targets,
                    [("auth.example.com", 443), ("plain.example.com", 80)],
                )

                # 6. POST /_rotate requires ROTATE_TOKEN and accepts X-API-Key
                c_rot_unauth = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
                c_rot_unauth.request("POST", "/_rotate")
                r_rot_unauth = c_rot_unauth.getresponse()
                r_rot_unauth.read()
                c_rot_unauth.close()
                self.assertEqual(r_rot_unauth.status, 401)

                c_rot_xkey = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
                c_rot_xkey.request("POST", "/_rotate", headers={"X-API-Key": "rot-secret"})
                r_rot_xkey = c_rot_xkey.getresponse()
                d_rot_xkey = json.loads(r_rot_xkey.read().decode())
                c_rot_xkey.close()
                self.assertEqual(r_rot_xkey.status, 200)
                self.assertTrue(d_rot_xkey["rotated"])
                mock_rot.assert_called_once_with(force=False)
        finally:
            proxy_srv.shutdown()
            proxy_srv.server_close()
            socks_srv.close()
            echo_srv.close()


if __name__ == "__main__":
    unittest.main()
