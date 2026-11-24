"""Unit tests for WARP /64 prefix deduplication, cooldown, and escalation."""

from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import patch

import warp_rotator as wr


class TestRotator(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
