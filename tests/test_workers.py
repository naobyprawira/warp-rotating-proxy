"""Unit tests for warp_proxy.workers multi-worker fleet management."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from unittest.mock import patch

from warp_proxy import workers


class TestWorkers(unittest.TestCase):
    def test_build_worker_specs_marks_standby_and_on_demand(self) -> None:
        specs = workers.build_worker_specs(
            standby=3, max_workers=5, base_port=40011, prefix="w"
        )
        self.assertEqual(len(specs), 5)
        self.assertEqual([s.container for s in specs], ["w-1", "w-2", "w-3", "w-4", "w-5"])
        self.assertEqual([s.port for s in specs], [40011, 40012, 40013, 40014, 40015])
        self.assertEqual([s.on_demand for s in specs], [False, False, False, True, True])
        self.assertEqual(
            specs[3].to_bridge_dict(),
            {
                "proxy": "http://127.0.0.1:40014",
                "rotate": "http://127.0.0.1:40014/_rotate",
                "token": "",
                "container": "w-4",
                "on_demand": True,
            },
        )

    def test_validate_counts_rejects_invalid_ranges(self) -> None:
        with self.assertRaises(ValueError):
            workers.validate_counts(0, 5, 40011)
        with self.assertRaises(ValueError):
            workers.validate_counts(4, 3, 40011)
        with self.assertRaises(ValueError):
            workers.validate_counts(1, 5, 65533)

    def test_generate_sudoers_content_lists_exact_containers(self) -> None:
        text = workers.generate_sudoers_content(
            "naoby", "/usr/bin/docker", "warp-worker", 2
        )
        self.assertIn("naoby ALL=(root) NOPASSWD:", text)
        self.assertIn("/usr/bin/docker start warp-worker-1", text)
        self.assertIn("/usr/bin/docker stop warp-worker-1", text)
        self.assertIn("/usr/bin/docker inspect warp-worker-1", text)
        self.assertIn("/usr/bin/docker start warp-worker-2", text)
        self.assertNotIn("*", text)

        with self.assertRaises(ValueError):
            workers.generate_sudoers_content("bad user", "/usr/bin/docker", "w", 1)
        with self.assertRaises(ValueError):
            workers.generate_sudoers_content("naoby", "docker", "w", 1)

    def test_env_command_outputs_bridge_manifest(self) -> None:
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = workers.main(
                ["env", "--standby", "2", "--max", "4", "--base-port", "40100", "--prefix", "ww"]
            )
        self.assertEqual(rc, 0)
        lines = buf.getvalue().strip().splitlines()
        self.assertTrue(lines[0].startswith("BRIDGE_UPSTREAM_WORKERS="))
        manifest = json.loads(lines[0].split("=", 1)[1])
        self.assertEqual(len(manifest), 4)
        self.assertFalse(manifest[1]["on_demand"])
        self.assertTrue(manifest[2]["on_demand"])
        self.assertEqual(lines[1], "BRIDGE_WORKERS_STANDBY=2")
        self.assertEqual(lines[2], "BRIDGE_WORKERS_MAX=4")

    def test_provision_reconciles_creates_starts_standby_and_preregisters(self) -> None:
        calls: list[list[str]] = []

        class FakeProc:
            def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd: list[str], **kwargs: object) -> FakeProc:
            calls.append(cmd)
            if len(cmd) >= 2 and cmd[1] == "inspect":
                return FakeProc(returncode=1)
            return FakeProc(returncode=0)

        with patch.object(workers, "run_cmd", side_effect=fake_run), patch.object(
            workers, "wait_worker_ready", return_value=(True, {"warp_connected": True})
        ):
            rc = workers.main(
                [
                    "provision",
                    "--standby",
                    "2",
                    "--max",
                    "4",
                    "--base-port",
                    "40201",
                    "--prefix",
                    "testw",
                    "--pre-register",
                ]
            )

        self.assertEqual(rc, 0)
        created_cmds = [c for c in calls if len(c) >= 2 and c[1] == "create"]
        self.assertEqual(len(created_cmds), 4)
        # Standby containers get restart=unless-stopped, on-demand get restart=no
        self.assertIn("unless-stopped", created_cmds[0])
        self.assertIn("unless-stopped", created_cmds[1])
        self.assertIn("no", created_cmds[2])
        self.assertIn("no", created_cmds[3])
        started = sorted(c[2] for c in calls if len(c) >= 3 and c[1] == "start")
        stopped = sorted(c[2] for c in calls if len(c) >= 3 and c[1] == "stop")
        self.assertEqual(started, ["testw-1", "testw-2", "testw-3", "testw-4"])
        self.assertEqual(stopped, ["testw-3", "testw-4"])


if __name__ == "__main__":
    unittest.main()
