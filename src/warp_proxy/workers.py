"""Provision and manage a fleet of containerized WARP rotating proxy workers.

Supports always-on standby workers (1..standby) and summon-on-event on-demand
workers (standby+1..max) with pre-registered state volumes for fast cold starts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerSpec:
    index: int
    container: str
    port: int
    volume: str
    on_demand: bool
    proxy_url: str
    rotate_url: str
    token: str = ""

    def to_bridge_dict(self) -> dict[str, Any]:
        return {
            "proxy": self.proxy_url,
            "rotate": self.rotate_url,
            "token": self.token,
            "container": self.container,
            "on_demand": self.on_demand,
        }


def _find_repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.abspath(os.path.join(here, "..", "..")),
        os.getcwd(),
    ]
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, "Dockerfile")):
            return cand
    return candidates[0]


def validate_counts(standby: int, max_workers: int, base_port: int) -> None:
    if standby < 1:
        raise ValueError(f"standby must be >= 1 (got {standby})")
    if max_workers < standby:
        raise ValueError(
            f"max_workers ({max_workers}) must be >= standby ({standby})"
        )
    if base_port < 1 or (base_port + max_workers - 1) > 65535:
        raise ValueError(
            f"Port range {base_port}..{base_port + max_workers - 1} out of 1..65535"
        )


def build_worker_specs(
    standby: int = 3,
    max_workers: int = 15,
    base_port: int = 40011,
    prefix: str = "warp-worker",
    rotate_token: str = "",
    proxy_user: str = "",
    proxy_pass: str = "",
) -> list[WorkerSpec]:
    validate_counts(standby, max_workers, base_port)
    auth = ""
    if proxy_user or proxy_pass:
        u = urllib.parse.quote(proxy_user, safe="")
        p = urllib.parse.quote(proxy_pass, safe="")
        auth = f"{u}:{p}@"
    specs: list[WorkerSpec] = []
    for idx in range(1, max_workers + 1):
        port = base_port + idx - 1
        specs.append(
            WorkerSpec(
                index=idx,
                container=f"{prefix}-{idx}",
                port=port,
                volume=f"{prefix}-{idx}-state",
                on_demand=idx > standby,
                proxy_url=f"http://{auth}127.0.0.1:{port}",
                rotate_url=f"http://127.0.0.1:{port}/_rotate",
                token=rotate_token,
            )
        )
    return specs


def resolve_docker_bin(explicit: str = "") -> str:
    if explicit:
        return explicit
    for cand in ("/usr/bin/docker", "/usr/local/bin/docker"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("docker") or "docker"


def _read_password(path: str | None) -> str | None:
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read().rstrip("\r\n") + "\n"


def run_cmd(
    cmd: list[str],
    *,
    use_sudo: bool = False,
    sudo_password: str | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    full_cmd = list(cmd)
    stdin_data: str | None = None
    if use_sudo:
        if sudo_password is not None:
            full_cmd = ["sudo", "-S", "-p", "", *full_cmd]
            stdin_data = sudo_password
        else:
            full_cmd = ["sudo", "-n", *full_cmd]
    return subprocess.run(
        full_cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def check_worker_health(
    port: int, timeout: float = 2.0
) -> tuple[bool, dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return False, {}
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not isinstance(data, dict):
                return False, {}
            return bool(data.get("warp_connected")), data
    except Exception:
        return False, {}


def fetch_worker_ip(port: int, timeout: float = 5.0) -> str:
    url = f"http://127.0.0.1:{port}/_status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return ""
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return str(data.get("egress_ip") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def wait_worker_ready(port: int, timeout: float = 45.0) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + max(1.0, timeout)
    last_data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        connected, data = check_worker_health(port, timeout=2.0)
        if data:
            last_data = data
        if connected:
            return True, data
        time.sleep(0.5)
    return False, last_data


def _inspect_container(
    docker_bin: str,
    name: str,
    *,
    use_sudo: bool,
    sudo_password: str | None,
) -> dict[str, Any] | None:
    res = run_cmd(
        [docker_bin, "inspect", name],
        use_sudo=use_sudo,
        sudo_password=sudo_password,
        timeout=15.0,
    )
    if res.returncode != 0:
        return None
    try:
        parsed = json.loads(res.stdout)
        if isinstance(parsed, list) and parsed:
            return parsed[0]
    except Exception:
        pass
    return None


def _container_matches_spec(info: dict[str, Any], spec: WorkerSpec, image: str) -> bool:
    cfg = info.get("Config") or {}
    host_cfg = info.get("HostConfig") or {}
    cur_image = str(cfg.get("Image") or "")
    if cur_image != image:
        return False
    expected_restart = "no" if spec.on_demand else "unless-stopped"
    cur_restart = str((host_cfg.get("RestartPolicy") or {}).get("Name") or "")
    if cur_restart != expected_restart:
        return False
    port_bindings = host_cfg.get("PortBindings") or {}
    bindings = port_bindings.get("40001/tcp") or []
    if not any(str(b.get("HostPort") or "") == str(spec.port) for b in bindings):
        return False
    return True


def _create_container(
    docker_bin: str,
    spec: WorkerSpec,
    image: str,
    *,
    warp_protocol: str,
    proxy_user: str,
    proxy_pass: str,
    use_sudo: bool,
    sudo_password: str | None,
) -> None:
    restart_policy = "no" if spec.on_demand else "unless-stopped"
    cmd = [
        docker_bin,
        "create",
        "--name",
        spec.container,
        "--restart",
        restart_policy,
        "--cap-add",
        "NET_ADMIN",
        "--device",
        "/dev/net/tun:/dev/net/tun",
        "-p",
        f"127.0.0.1:{spec.port}:40001",
        "-e",
        "WARP_PROXY_HOST=0.0.0.0",
        "-e",
        "WARP_PROXY_PORT=40001",
        "-e",
        "WARP_SOCKS_HOST=127.0.0.1",
        "-e",
        "WARP_SOCKS_PORT=40000",
        "-e",
        f"WARP_PROTOCOL={warp_protocol}",
        "-v",
        f"{spec.volume}:/var/lib/cloudflare-warp",
    ]
    if spec.token:
        cmd.extend(["-e", f"WARP_ROTATE_TOKEN={spec.token}"])
    if proxy_user:
        cmd.extend(["-e", f"WARP_PROXY_USER={proxy_user}"])
    if proxy_pass:
        cmd.extend(["-e", f"WARP_PROXY_PASS={proxy_pass}"])
    cmd.append(image)

    res = run_cmd(cmd, use_sudo=use_sudo, sudo_password=sudo_password, timeout=30.0)
    if res.returncode != 0:
        raise RuntimeError(
            f"Failed creating {spec.container}: {res.stderr.strip() or res.stdout.strip()}"
        )


def generate_sudoers_content(
    user: str,
    docker_bin: str,
    prefix: str,
    max_workers: int,
) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError(f"Invalid Linux username for sudoers: {user!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", prefix):
        raise ValueError(f"Invalid container prefix for sudoers: {prefix!r}")
    if not os.path.isabs(docker_bin):
        raise ValueError(f"docker_bin must be an absolute path: {docker_bin!r}")

    cmds: list[str] = []
    for idx in range(1, max_workers + 1):
        c = f"{prefix}-{idx}"
        cmds.append(f"{docker_bin} start {c}")
        cmds.append(f"{docker_bin} stop {c}")
        cmds.append(f"{docker_bin} inspect {c}")

    joined = ", \\\n    ".join(cmds)
    return (
        "# Managed by warp-rotating-proxy (python3 -m warp_proxy.workers)\n"
        f"{user} ALL=(root) NOPASSWD: \\\n    {joined}\n"
    )


def install_sudoers(
    user: str,
    docker_bin: str,
    prefix: str,
    max_workers: int,
    *,
    use_sudo: bool,
    sudo_password: str | None,
    dest_path: str = "/etc/sudoers.d/warp-workers",
) -> None:
    content = generate_sudoers_content(user, docker_bin, prefix, max_workers)
    fd, tmp_path = tempfile.mkstemp(prefix="warp-sudoers-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, 0o440)
        visudo = shutil.which("visudo") or "/usr/sbin/visudo"
        chk = run_cmd(
            [visudo, "-cf", tmp_path],
            use_sudo=use_sudo,
            sudo_password=sudo_password,
            timeout=10.0,
        )
        if chk.returncode != 0:
            raise RuntimeError(
                f"visudo validation failed: {chk.stderr.strip() or chk.stdout.strip()}"
            )
        inst = run_cmd(
            ["install", "-m", "0440", "-o", "root", "-g", "root", tmp_path, dest_path],
            use_sudo=use_sudo,
            sudo_password=sudo_password,
            timeout=10.0,
        )
        if inst.returncode != 0:
            raise RuntimeError(
                f"Failed installing {dest_path}: {inst.stderr.strip() or inst.stdout.strip()}"
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _start_and_wait(
    docker_bin: str,
    spec: WorkerSpec,
    *,
    use_sudo: bool,
    sudo_password: str | None,
    wait_timeout: float = 45.0,
) -> tuple[WorkerSpec, bool]:
    res = run_cmd(
        [docker_bin, "start", spec.container],
        use_sudo=use_sudo,
        sudo_password=sudo_password,
        timeout=30.0,
    )
    if res.returncode != 0:
        sys.stderr.write(
            f"[workers] Failed to start {spec.container}: {res.stderr.strip()}\n"
        )
        return spec, False
    ok, _ = wait_worker_ready(spec.port, timeout=wait_timeout)
    return spec, ok


def _stop_container(
    docker_bin: str,
    spec: WorkerSpec,
    *,
    use_sudo: bool,
    sudo_password: str | None,
) -> bool:
    res = run_cmd(
        [docker_bin, "stop", spec.container],
        use_sudo=use_sudo,
        sudo_password=sudo_password,
        timeout=30.0,
    )
    return res.returncode == 0


def cmd_provision(args: argparse.Namespace) -> int:
    specs = build_worker_specs(
        standby=args.standby,
        max_workers=args.max,
        base_port=args.base_port,
        prefix=args.prefix,
        rotate_token=args.rotate_token,
        proxy_user=args.proxy_user,
        proxy_pass=args.proxy_pass,
    )
    docker_bin = resolve_docker_bin(args.docker_bin)
    sudo_password = _read_password(args.sudo_password_file)
    use_sudo = args.sudo or (sudo_password is not None)

    if args.build:
        repo_dir = args.repo_dir or _find_repo_root()
        print(f"Building Docker image {args.image} from {repo_dir}...")
        res = run_cmd(
            [docker_bin, "build", "-t", args.image, repo_dir],
            use_sudo=use_sudo,
            sudo_password=sudo_password,
            timeout=600.0,
        )
        if res.returncode != 0:
            sys.stderr.write(res.stdout + "\n" + res.stderr + "\n")
            return res.returncode or 1

    created = 0
    recreated = 0
    kept = 0
    for spec in specs:
        info = _inspect_container(
            docker_bin, spec.container, use_sudo=use_sudo, sudo_password=sudo_password
        )
        if info is not None:
            if args.recreate or not _container_matches_spec(info, spec, args.image):
                run_cmd(
                    [docker_bin, "rm", "-f", spec.container],
                    use_sudo=use_sudo,
                    sudo_password=sudo_password,
                    timeout=30.0,
                )
                if args.reset_volumes:
                    run_cmd(
                        [docker_bin, "volume", "rm", "-f", spec.volume],
                        use_sudo=use_sudo,
                        sudo_password=sudo_password,
                        timeout=15.0,
                    )
                _create_container(
                    docker_bin,
                    spec,
                    args.image,
                    warp_protocol=args.warp_protocol,
                    proxy_user=args.proxy_user,
                    proxy_pass=args.proxy_pass,
                    use_sudo=use_sudo,
                    sudo_password=sudo_password,
                )
                recreated += 1
            else:
                kept += 1
        else:
            if args.reset_volumes:
                run_cmd(
                    [docker_bin, "volume", "rm", "-f", spec.volume],
                    use_sudo=use_sudo,
                    sudo_password=sudo_password,
                    timeout=15.0,
                )
            _create_container(
                docker_bin,
                spec,
                args.image,
                warp_protocol=args.warp_protocol,
                proxy_user=args.proxy_user,
                proxy_pass=args.proxy_pass,
                use_sudo=use_sudo,
                sudo_password=sudo_password,
            )
            created += 1

    print(
        f"Containers reconciled: created={created}, recreated={recreated}, kept={kept} "
        f"(standby={args.standby}, max={args.max})"
    )

    standby_specs = [s for s in specs if not s.on_demand]
    ondemand_specs = [s for s in specs if s.on_demand]

    print(f"Starting {len(standby_specs)} standby worker(s)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(standby_specs))) as ex:
        results = list(
            ex.map(
                lambda s: _start_and_wait(
                    docker_bin,
                    s,
                    use_sudo=use_sudo,
                    sudo_password=sudo_password,
                    wait_timeout=args.wait_timeout,
                ),
                standby_specs,
            )
        )
    standby_ready = sum(1 for _, ok in results if ok)
    print(f"Standby ready: {standby_ready}/{len(standby_specs)}")

    if args.pre_register and ondemand_specs:
        print(
            f"Pre-registering {len(ondemand_specs)} on-demand worker(s) in batches of 3..."
        )
        batch_size = 3
        prereg_ok = 0
        for start_i in range(0, len(ondemand_specs), batch_size):
            batch = ondemand_specs[start_i : start_i + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as ex:
                batch_res = list(
                    ex.map(
                        lambda s: _start_and_wait(
                            docker_bin,
                            s,
                            use_sudo=use_sudo,
                            sudo_password=sudo_password,
                            wait_timeout=args.wait_timeout,
                        ),
                        batch,
                    )
                )
            for s, ok in batch_res:
                if ok:
                    prereg_ok += 1
                else:
                    sys.stderr.write(
                        f"[workers] Warning: {s.container} did not report warp_connected within {args.wait_timeout}s\n"
                    )
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as ex:
                list(
                    ex.map(
                        lambda s: _stop_container(
                            docker_bin,
                            s,
                            use_sudo=use_sudo,
                            sudo_password=sudo_password,
                        ),
                        batch,
                    )
                )
        print(f"Pre-registered & parked on-demand workers: {prereg_ok}/{len(ondemand_specs)}")
    else:
        for s in ondemand_specs:
            info = _inspect_container(
                docker_bin, s.container, use_sudo=use_sudo, sudo_password=sudo_password
            )
            running = bool((info or {}).get("State", {}).get("Running"))
            if running:
                _stop_container(
                    docker_bin, s, use_sudo=use_sudo, sudo_password=sudo_password
                )

    if args.install_sudoers_user:
        install_sudoers(
            args.install_sudoers_user,
            docker_bin,
            args.prefix,
            args.max,
            use_sudo=use_sudo,
            sudo_password=sudo_password,
        )
        print(
            f"Installed passwordless docker worker sudoers rule for '{args.install_sudoers_user}' "
            "at /etc/sudoers.d/warp-workers"
        )

    if args.json:
        print(json.dumps([s.to_bridge_dict() for s in specs], indent=2))
    return 0 if standby_ready == len(standby_specs) else 1


def _select_specs(args: argparse.Namespace, specs: list[WorkerSpec]) -> list[WorkerSpec]:
    if getattr(args, "index", None):
        wanted = set(args.index)
        return [s for s in specs if s.index in wanted]
    if getattr(args, "standby_only", False):
        return [s for s in specs if not s.on_demand]
    if getattr(args, "on_demand_only", False):
        return [s for s in specs if s.on_demand]
    return specs


def cmd_start(args: argparse.Namespace) -> int:
    specs = build_worker_specs(
        standby=args.standby,
        max_workers=args.max,
        base_port=args.base_port,
        prefix=args.prefix,
    )
    targets = _select_specs(args, specs)
    docker_bin = resolve_docker_bin(args.docker_bin)
    sudo_password = _read_password(args.sudo_password_file)
    use_sudo = args.sudo or (sudo_password is not None)

    for s in targets:
        res = run_cmd(
            [docker_bin, "start", s.container],
            use_sudo=use_sudo,
            sudo_password=sudo_password,
            timeout=30.0,
        )
        if res.returncode != 0:
            sys.stderr.write(f"Failed starting {s.container}: {res.stderr.strip()}\n")
            return res.returncode or 1
        if args.wait:
            ok, _ = wait_worker_ready(s.port, timeout=args.wait_timeout)
            print(f"{s.container} (:{s.port}) -> {'ready' if ok else 'not-ready'}")
        else:
            print(f"Started {s.container} (:{s.port})")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    specs = build_worker_specs(
        standby=args.standby,
        max_workers=args.max,
        base_port=args.base_port,
        prefix=args.prefix,
    )
    targets = _select_specs(args, specs)
    docker_bin = resolve_docker_bin(args.docker_bin)
    sudo_password = _read_password(args.sudo_password_file)
    use_sudo = args.sudo or (sudo_password is not None)

    for s in targets:
        res = run_cmd(
            [docker_bin, "stop", s.container],
            use_sudo=use_sudo,
            sudo_password=sudo_password,
            timeout=30.0,
        )
        if res.returncode != 0:
            sys.stderr.write(f"Failed stopping {s.container}: {res.stderr.strip()}\n")
        else:
            print(f"Stopped {s.container}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    specs = build_worker_specs(
        standby=args.standby,
        max_workers=args.max,
        base_port=args.base_port,
        prefix=args.prefix,
    )
    docker_bin = resolve_docker_bin(args.docker_bin)
    sudo_password = _read_password(args.sudo_password_file)
    use_sudo = args.sudo or (sudo_password is not None)

    rows: list[dict[str, Any]] = []
    for s in specs:
        info = _inspect_container(
            docker_bin, s.container, use_sudo=use_sudo, sudo_password=sudo_password
        )
        exists = info is not None
        running = bool((info or {}).get("State", {}).get("Running"))
        connected, health = check_worker_health(s.port, timeout=1.5)
        ip = fetch_worker_ip(s.port, timeout=4.0) if (args.ip and connected) else ""
        row = {
            "index": s.index,
            "container": s.container,
            "role": "on-demand" if s.on_demand else "standby",
            "port": s.port,
            "exists": exists,
            "running": running,
            "warp_connected": connected,
            "rotating": bool(health.get("rotating")) if health else False,
            "egress_ip": ip or None,
        }
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    header = f"{'IDX':>3}  {'CONTAINER':<16}  {'ROLE':<9}  {'PORT':>5}  {'DOCKER':<8}  {'WARP':<12}"
    if args.ip:
        header += "  EGRESS_IP"
    print(header)
    for r in rows:
        d_state = "running" if r["running"] else ("stopped" if r["exists"] else "missing")
        w_state = "connected" if r["warp_connected"] else ("down" if not r["running"] else "connecting")
        line = f"{r['index']:>3}  {r['container']:<16}  {r['role']:<9}  {r['port']:>5}  {d_state:<8}  {w_state:<12}"
        if args.ip:
            line += f"  {r['egress_ip'] or '-'}"
        print(line)
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    specs = build_worker_specs(
        standby=args.standby,
        max_workers=args.max,
        base_port=args.base_port,
        prefix=args.prefix,
        rotate_token=args.rotate_token,
        proxy_user=args.proxy_user,
        proxy_pass=args.proxy_pass,
    )
    payload = [s.to_bridge_dict() for s in specs]
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(f"BRIDGE_UPSTREAM_WORKERS={json.dumps(payload, separators=(',', ':'))}")
        print(f"BRIDGE_WORKERS_STANDBY={args.standby}")
        print(f"BRIDGE_WORKERS_MAX={args.max}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m warp_proxy.workers",
        description="Provision and control multi-worker WARP Docker containers.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common_fleet(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--standby", type=int, default=3, help="Always-on standby workers (default: 3)")
        sp.add_argument("--max", type=int, default=15, help="Total max workers including on-demand (default: 15)")
        sp.add_argument("--base-port", type=int, default=40011, help="Host loopback start port (default: 40011)")
        sp.add_argument("--prefix", default="warp-worker", help="Container/volume name prefix (default: warp-worker)")
        sp.add_argument("--docker-bin", default="", help="Override docker binary path")
        sp.add_argument("--sudo", action="store_true", help="Invoke docker via sudo -n")
        sp.add_argument("--sudo-password-file", default="", help="Read sudo password from file (passed to sudo -S stdin)")

    sp_prov = sub.add_parser("provision", help="Build/create standby + on-demand worker containers")
    add_common_fleet(sp_prov)
    sp_prov.add_argument("--image", default="warp-rotating-proxy:latest", help="Docker image tag")
    sp_prov.add_argument("--repo-dir", default="", help="Path to warp-rotating-proxy Dockerfile repo")
    sp_prov.add_argument("--build", action="store_true", help="Build Docker image before provisioning")
    sp_prov.add_argument("--pre-register", action="store_true", help="Warm-register on-demand containers then stop them")
    sp_prov.add_argument("--recreate", action="store_true", help="Recreate existing containers")
    sp_prov.add_argument("--reset-volumes", action="store_true", help="Reset state volumes when recreating")
    sp_prov.add_argument("--warp-protocol", default="MASQUE", help="WARP tunnel protocol (default: MASQUE)")
    sp_prov.add_argument("--rotate-token", default="", help="Optional WARP_ROTATE_TOKEN")
    sp_prov.add_argument("--proxy-user", default="", help="Optional WARP_PROXY_USER")
    sp_prov.add_argument("--proxy-pass", default="", help="Optional WARP_PROXY_PASS")
    sp_prov.add_argument("--wait-timeout", type=float, default=45.0, help="Readiness timeout per worker batch (s)")
    sp_prov.add_argument("--install-sudoers-user", default="", help="Install NOPASSWD /etc/sudoers.d/warp-workers for user")
    sp_prov.add_argument("--json", action="store_true", help="Print JSON worker pool manifest at the end")
    sp_prov.set_defaults(func=cmd_provision)

    sp_start = sub.add_parser("start", help="Start worker containers")
    add_common_fleet(sp_start)
    sp_start.add_argument("--standby-only", action="store_true", help="Start only standby workers")
    sp_start.add_argument("--on-demand-only", action="store_true", help="Start only on-demand workers")
    sp_start.add_argument("--index", type=int, action="append", help="Specific worker index (1..max), repeatable")
    sp_start.add_argument("--wait", action="store_true", help="Wait for /health warp_connected")
    sp_start.add_argument("--wait-timeout", type=float, default=45.0, help="Readiness wait timeout (s)")
    sp_start.set_defaults(func=cmd_start)

    sp_stop = sub.add_parser("stop", help="Stop worker containers")
    add_common_fleet(sp_stop)
    sp_stop.add_argument("--standby-only", action="store_true", help="Stop only standby workers")
    sp_stop.add_argument("--on-demand-only", action="store_true", help="Stop only on-demand workers")
    sp_stop.add_argument("--index", type=int, action="append", help="Specific worker index (1..max), repeatable")
    sp_stop.set_defaults(func=cmd_stop)

    sp_stat = sub.add_parser("status", help="Show Docker + WARP health status of workers")
    add_common_fleet(sp_stat)
    sp_stat.add_argument("--ip", action="store_true", help="Also query /_status egress IP of connected workers")
    sp_stat.add_argument("--json", action="store_true", help="Output JSON")
    sp_stat.set_defaults(func=cmd_status)

    sp_env = sub.add_parser("env", help="Print BRIDGE_UPSTREAM_WORKERS configuration")
    add_common_fleet(sp_env)
    sp_env.add_argument("--rotate-token", default="", help="Optional WARP_ROTATE_TOKEN")
    sp_env.add_argument("--proxy-user", default="", help="Optional WARP_PROXY_USER")
    sp_env.add_argument("--proxy-pass", default="", help="Optional WARP_PROXY_PASS")
    sp_env.add_argument("--format", choices=("env", "json"), default="env", help="Output format")
    sp_env.set_defaults(func=cmd_env)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
