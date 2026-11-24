# warp-rotating-proxy

Lightweight, zero-dependency Python 3 HTTP/HTTPS `CONNECT` proxy backed by Cloudflare WARP (`warp-cli` MASQUE proxy mode) with an HTTP webhook to rotate IPv6 `/64` identities on demand.

Useful for any tool, container, or API client (`curl`, `HTTP_PROXY`/`HTTPS_PROXY`, [`opencode-openai-bridge`](https://github.com/naobyprawira/opencode-openai-bridge), etc.) that needs a standard HTTP proxy with rotatable Cloudflare WARP egress.

## Architecture

```
Client / Bridge (HTTP_PROXY=http://127.0.0.1:40001)
   │  (HTTP CONNECT / forward + POST /_rotate)
   ▼
warp_rotating_proxy.py      (Python 3 stdlib: HTTP proxy + rotation webhook)
   │  (SOCKS5h tunnel)
   ▼
warp-cli MASQUE proxy       (socks5h://127.0.0.1:40000) ──▶ Internet (Cloudflare WARP)
```

## Prerequisites

- Python 3.10+
- `curl`
- [Cloudflare WARP CLI](https://developers.cloudflare.com/warp-client/) (`warp-cli`, registered and able to run `warp-cli --accept-tos mode proxy` / `tunnel protocol set MASQUE`)

## Installation

```bash
bash install.sh
```

## Endpoints & Usage

- `CONNECT host:port` — standard HTTPS tunnel through WARP SOCKS5.
- `GET/POST/... http://...` — plain HTTP forward proxy through WARP SOCKS5.
- `GET /health` (or `/_health`) — proxy & WARP status.
- `GET /_status` — current egress IP, IPv6 `/64` prefix, and spent-prefix cache size.
- `POST /_rotate` (optional `?force=1`) — rotates WARP keys (`tunnel rotate-keys`) and falls back to `registration delete/new` if WARP keeps returning the same/spent `/64` prefix.

```bash
# Check proxy health and current WARP egress IP
curl -s http://127.0.0.1:40001/_status

# Route HTTPS requests through the HTTP proxy
HTTPS_PROXY=http://127.0.0.1:40001 curl -s https://cloudflare.com/cdn-cgi/trace

# Rotate WARP IP on demand (e.g., on HTTP 429)
curl -s -X POST http://127.0.0.1:40001/_rotate
```

### Using with `opencode-openai-bridge`

Configure [`opencode-openai-bridge`](https://github.com/naobyprawira/opencode-openai-bridge) with:

```ini
Environment=BRIDGE_UPSTREAM_PROXY=http://127.0.0.1:40001
Environment=BRIDGE_UPSTREAM_ROTATE_URL=http://127.0.0.1:40001/_rotate
```

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `WARP_PROXY_HOST` | `127.0.0.1` | HTTP proxy listen host |
| `WARP_PROXY_PORT` | `40001` | HTTP proxy & control listen port |
| `WARP_SOCKS_HOST` | `127.0.0.1` | `warp-cli` SOCKS5 host |
| `WARP_SOCKS_PORT` | `40000` | `warp-cli` SOCKS5 port |
| `WARP_PROTOCOL` | `MASQUE` | WARP tunnel protocol (`MASQUE` required on `warp-cli 2026.8+`) |
| `WARP_ROTATE_COOLDOWN` | `15` | Minimum seconds between non-forced rotations |
| `WARP_BAD_PREFIX_TTL` | `1800` | How long rotated-from IPv6 `/64` prefixes are avoided (seconds) |
| `WARP_ROTATE_TOKEN` | *(empty)* | Optional Bearer token required for `POST /_rotate` |
| `WARP_PAUSE_FILE` | *(empty)* | Optional lock/pause file path touched while rotating |

## Testing

```bash
python3 -m unittest discover -s tests -v
```
