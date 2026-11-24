# Changelog

All notable changes to `warp-rotating-proxy` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] - 2026-11-24

### Fixed
- Added `WarpRotator.wait_until_ready()` barrier and `rotating` status flag so concurrent `CONNECT` and plain-HTTP proxy requests wait cleanly for an active WARP rotation/reconnect instead of failing immediately with `502 WARP SOCKS5 tunnel error`.

## [0.2.1] - 2026-11-24

### Changed
- Reorganized repository into a standard `src/warp_proxy/` Python package (`__main__.py` entrypoint), `deploy/docker/`, `deploy/systemd/`, `tests/__init__.py`, and `pyproject.toml` (still zero third-party `pip` dependencies).
- Updated `Dockerfile` and root `install.sh` to run `python3 -m warp_proxy` with `PYTHONPATH` and clean up legacy flat files in `$WARP_PROXY_STATE_DIR` while preserving systemd drop-ins.

## [0.2.0] - 2026-11-24

### Added
- Containerized deployment via `Dockerfile`, `docker-entrypoint.sh`, `.dockerignore`, and `docker-compose.yml` bundling `dbus`, `cloudflare-warp` (`warp-svc` + `warp-cli`), and the Python proxy inside an isolated container network namespace.
- Automatic WARP device registration (`WarpRotator.is_registered()` + `registration new` inside `ensure_connected()`) for clean containers and fresh hosts.
- Optional Basic `Proxy-Authorization` protection (`WARP_PROXY_USER` / `WARP_PROXY_PASS`, returning `407 Proxy Authentication Required`) with `proxy-authorization` header stripping on plain-HTTP forwards.
- `X-API-Key` header support alongside `Authorization: Bearer` for `POST /_rotate` when `WARP_ROTATE_TOKEN` is set.
- `PORT` environment fallback for `WARP_PROXY_PORT` via `resolve_port()`, plus `proxy_auth_enabled` and `rotate_auth_enabled` fields in `/health`.

## [0.1.1] - 2026-11-24

### Added
- Automatic `WARP_CLI_BIN` path resolution (`resolve_binary()` in `proxy_config.py`) across env overrides, common install locations, and `PATH`.
- Portable `install.sh` CLI flags (`--dest`, `--python`, `--warp-cli`, `--host`, `--port`, `--socks-host`, `--socks-port`, `--non-interactive`) with auto-detection and interactive prompt fallback.

### Changed
- Replaced external `curl` subprocess in `WarpRotator.trace_egress_ip()` with pure Python stdlib SOCKS5 + TLS HTTP request, removing `curl` as a runtime dependency.

## [0.1.0] - 2026-11-24

### Added
- Pure Python 3 stdlib HTTP `CONNECT` & plain-HTTP forward proxy backed by `warp-cli` SOCKS5 (`socks5_tunnel.py`, `warp_rotating_proxy.py`).
- WARP MASQUE connection manager & `/64` IPv6 prefix-deduplicated rotation webhook `POST /_rotate` (`warp_rotator.py`).
- Health & status endpoints (`/health`, `/_status`).
- Portable `systemd` user unit, `install.sh`, and unit/loopback integration test suite (`tests/test_proxy.py`, `tests/test_rotator.py`).
