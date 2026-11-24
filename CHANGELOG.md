# Changelog

All notable changes to `warp-rotating-proxy` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-24

### Added
- Pure Python 3 stdlib HTTP `CONNECT` & plain-HTTP forward proxy backed by `warp-cli` SOCKS5 (`socks5_tunnel.py`, `warp_rotating_proxy.py`).
- WARP MASQUE connection manager & `/64` IPv6 prefix-deduplicated rotation webhook `POST /_rotate` (`warp_rotator.py`).
- Health & status endpoints (`/health`, `/_status`).
- Portable `systemd` user unit, `install.sh`, and unit/loopback integration test suite (`tests/test_proxy.py`, `tests/test_rotator.py`).
