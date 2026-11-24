"""Unit tests for WARP /64 prefix deduplication, cooldown, binary detection, and trace parsing."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from warp_proxy import config as cfg
from warp_proxy import rotator as wr


class TestRotator(unittest.TestCase):
    def test_resolve_binary_env_candidate_and_which(self) -> None:
        with patch.dict(os.environ, {"WARP_CLI_BIN": "/custom/warp-cli"}):
            self.assertEqual(
                cfg.resolve_binary("WARP_CLI_BIN", "warp-cli", ["/usr/bin/warp-cli"]),
                "/custom/warp-cli",
            )

        with tempfile.TemporaryDirectory() as td:
            fake_bin = os.path.join(td, "warp-cli")
            with open(fake_bin, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\n")
            os.chmod(fake_bin, 0o755)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    cfg.resolve_binary("WARP_CLI_BIN", "warp-cli", ["/nonexistent/warp-cli", fake_bin]),
                    fake_bin,
                )

        with patch.dict(os.environ, {}, clear=True), patch(
            "shutil.which", return_value="/opt/bin/warp-cli"
        ):
            self.assertEqual(
                cfg.resolve_binary("WARP_CLI_BIN", "warp-cli", ["/nonexistent/warp-cli"]),
                "/opt/bin/warp-cli",
            )

    def test_trace_egress_ip_uses_pure_python_tunnel(self) -> None:
        client_sock, server_sock = socket.socketpair()
        captured_req: list[bytes] = []

        def fake_server() -> None:
            try:
                captured_req.append(server_sock.recv(4096))
                server_sock.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n"
                    b"fl=123\nh=cloudflare.com\nip=2a09:bac5:3a00:9999::42\nts=1\n"
                )
            finally:
                server_sock.close()

        t = threading.Thread(target=fake_server, daemon=True)
        t.start()

        rot = wr.WarpRotator()
        with patch.object(wr, "TRACE_URL", "http://127.0.0.1:8080/cdn-cgi/trace"), patch.object(
            wr, "open_socks5_tunnel", return_value=client_sock
        ) as mock_tunnel:
            ip = rot.trace_egress_ip()

        t.join(timeout=2)
        self.assertEqual(ip, "2a09:bac5:3a00:9999::42")
        mock_tunnel.assert_called_once_with(
            wr.WARP_SOCKS_HOST, wr.WARP_SOCKS_PORT, "127.0.0.1", 8080, timeout=8.0
        )
        self.assertIn(b"GET /cdn-cgi/trace HTTP/1.1", captured_req[0])

    def test_ipv6_prefix64(self) -> None:
        self.assertEqual(
            wr.ipv6_prefix64("2a09:bac5:3a00:18be::277:4f"),
            "2a09:bac5:3a00:18be",
        )
        self.assertEqual(wr.ipv6_prefix64("1.2.3.4"), "1.2.3.4")

    def test_rotate_respects_cooldown_unless_forced(self) -> None:
        rot = wr.WarpRotator()
        rot._last_rotate_ts = time.monotonic()
        with patch.object(rot, "trace_egress_ip", return_value="2a09:bac5:3a00:1::1"), patch.object(
            rot, "_run_cli"
        ) as mock_cli:
            res = rot.rotate(force=False)
        self.assertTrue(res["cooldown"])
        self.assertFalse(res["rotated"])
        self.assertEqual(mock_cli.call_count, 0)

    def test_rotate_escalates_to_registration_reset_when_prefix_repeats(self) -> None:
        rot = wr.WarpRotator()
        ips = [
            "2a09:bac5:3a00:1111::1",  # old_ip
            "2a09:bac5:3a00:1111::2",  # same /64 after rotate-keys
            "2a09:bac5:3a00:2222::5",  # fresh /64 after registration reset
        ]
        cli_calls: list[tuple[str, ...]] = []

        def fake_cli(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
            cli_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "Connected", "")

        with patch.object(rot, "trace_egress_ip", side_effect=ips), patch.object(
            rot, "_run_cli", side_effect=fake_cli
        ), patch.object(rot, "is_connected", return_value=True), patch("time.sleep"):
            res = rot.rotate(force=True)

        self.assertTrue(res["ok"])
        self.assertTrue(res["rotated"])
        self.assertEqual(res["new_ip"], "2a09:bac5:3a00:2222::5")
        self.assertIn(("tunnel", "rotate-keys"), cli_calls)
        self.assertIn(("registration", "new"), cli_calls)

    def test_resolve_port_priority_and_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cfg.resolve_port(("WARP_PROXY_PORT", "PORT"), 40001), 40001)
        with patch.dict(os.environ, {"PORT": "18080"}, clear=True):
            self.assertEqual(cfg.resolve_port(("WARP_PROXY_PORT", "PORT"), 40001), 18080)
        with patch.dict(os.environ, {"WARP_PROXY_PORT": "41001", "PORT": "18080"}, clear=True):
            self.assertEqual(cfg.resolve_port(("WARP_PROXY_PORT", "PORT"), 40001), 41001)

    def test_is_registered_checks_cli_output(self) -> None:
        rot = wr.WarpRotator()
        ok_cp = subprocess.CompletedProcess(
            ("registration", "show"),
            0,
            "Account type: Free\nDevice ID: abc-123\n",
            "",
        )
        missing_cp = subprocess.CompletedProcess(
            ("registration", "show"),
            1,
            "",
            "Registration Missing",
        )
        with patch.object(rot, "_run_cli", return_value=ok_cp):
            self.assertTrue(rot.is_registered())
        with patch.object(rot, "_run_cli", return_value=missing_cp):
            self.assertFalse(rot.is_registered())

    def test_ensure_connected_auto_registers_when_unregistered(self) -> None:
        rot = wr.WarpRotator()
        cli_calls: list[tuple[str, ...]] = []

        def fake_cli(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
            cli_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "Success", "")

        with patch.object(rot, "is_connected", side_effect=[False, False, True]), patch.object(
            rot, "is_registered", return_value=False
        ), patch.object(rot, "_run_cli", side_effect=fake_cli), patch("time.sleep"):
            self.assertTrue(rot.ensure_connected())

        self.assertEqual(cli_calls[0], ("registration", "new"))
        self.assertIn(("mode", "proxy"), cli_calls)
        self.assertIn(("connect",), cli_calls)

    def test_ensure_connected_skips_registration_when_already_registered(self) -> None:
        rot = wr.WarpRotator()
        cli_calls: list[tuple[str, ...]] = []

        def fake_cli(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
            cli_calls.append(args)
            return subprocess.CompletedProcess(args, 0, "Success", "")

        with patch.object(rot, "is_connected", side_effect=[False, False, True]), patch.object(
            rot, "is_registered", return_value=True
        ), patch.object(rot, "_run_cli", side_effect=fake_cli), patch("time.sleep"):
            self.assertTrue(rot.ensure_connected())

        self.assertNotIn(("registration", "new"), cli_calls)
        self.assertIn(("connect",), cli_calls)

    def test_wait_until_ready_waits_for_rotation_finish(self) -> None:
        rot = wr.WarpRotator()
        rot._set_rotating(True)
        done: list[bool] = []

        def waiter() -> None:
            done.append(rot.wait_until_ready(timeout=2.0))

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.05)
        self.assertEqual(done, [])
        rot._set_rotating(False)
        t.join(timeout=2.0)
        self.assertEqual(done, [True])

    def test_mark_bad_prefix_evicts_oldest_when_cap_reached(self) -> None:
        rot = wr.WarpRotator()
        with patch.object(wr, "MAX_BAD_PREFIXES", 2):
            rot.mark_bad_prefix("2a09:bac5:3a00:1")
            rot.mark_bad_prefix("2a09:bac5:3a00:2")
            rot.mark_bad_prefix("2a09:bac5:3a00:3")
        self.assertEqual(
            list(rot._bad_prefixes.keys()),
            ["2a09:bac5:3a00:2", "2a09:bac5:3a00:3"],
        )


if __name__ == "__main__":
    unittest.main()
