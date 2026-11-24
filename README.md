# warp-rotating-proxy

Lightweight, zero-dependency Python 3 HTTP/HTTPS `CONNECT` proxy backed by Cloudflare WARP (`warp-cli` MASQUE proxy mode) with an HTTP webhook to rotate IPv6 `/64` identities on demand.

Useful for any tool, container, or API client (`curl`, `HTTP_PROXY`/`HTTPS_PROXY`, [`opencode-openai-bridge`](https://github.com/naobyprawira/opencode-openai-bridge), etc.) that needs a standard HTTP proxy with rotatable Cloudflare WARP egress.

## Architecture

```
Client / Bridge (HTTP_PROXY=http://127.0.0.1:40001)
   │  (HTTP CONNECT / forward + POST /_rotate)
   ▼
warp_proxy.server           (Python 3 stdlib: HTTP proxy + rotation webhook)
   │  (SOCKS5h tunnel)
   ▼
warp-cli MASQUE proxy       (socks5h://127.0.0.1:40000) ──▶ Internet (Cloudflare WARP)
```

## Prerequisites

- Python 3.10+ (zero third-party `pip` or `curl` dependencies)
- [Cloudflare WARP CLI](https://developers.cloudflare.com/warp-client/) (`warp-cli`, auto-detected via `WARP_CLI_BIN`, `/usr/bin/warp-cli`, `/usr/local/bin/warp-cli`, `/opt/homebrew/bin/warp-cli`, or `PATH`, able to run `warp-cli --accept-tos mode proxy` / `tunnel protocol set MASQUE`). Unregistered setups are automatically registered via `warp-cli --accept-tos registration new` on startup.

## Installation (Host / `systemd`)

```bash
bash install.sh
```

`install.sh` auto-detects `python3` and `warp-cli`, prompts interactively if any binary cannot be found, and writes the resolved paths into `~/.config/systemd/user/warp-rotating-proxy.service`. You can also pass explicit options:

```bash
bash install.sh \
  --dest ~/.local/share/warp-rotating-proxy \
  --python /usr/bin/python3 \
  --warp-cli /usr/bin/warp-cli \
  --host 127.0.0.1 \
  --port 40001 \
  --socks-host 127.0.0.1 \
  --socks-port 40000 \
  --non-interactive
```

Run `bash install.sh --help` for all options.

## Docker Deployment (VPS / Docker Host)

The repository includes a self-contained [`Dockerfile`](Dockerfile) and [`docker-compose.yml`](docker-compose.yml) that bundle `dbus`, `cloudflare-warp` (`warp-svc` + `warp-cli`), and the Python proxy inside a single container:

```bash
docker compose up -d --build
```

> [!NOTE]
> Inside Docker, `warp-svc` creates its tunnel inside the **container's own network namespace**—it does not modify your host VPS network or route other host traffic through WARP.
> Because `cloudflare-warp` requires `/dev/net/tun` and `CAP_NET_ADMIN`, Docker deployment works on a VPS/VM/dedicated server that permits TUN devices, whereas unprivileged shared PaaS containers (such as Render free/standard web services or Heroku dynos) block `NET_ADMIN` and `/dev/net/tun`.

Persistent registration state is stored in the `warp_state` volume (`/var/lib/cloudflare-warp`), and `docker-compose.yml` binds port `40001` to `127.0.0.1` by default.

## Endpoints & Usage

- `CONNECT host:port` — standard HTTPS tunnel through WARP SOCKS5.
- `GET/POST/... http://...` — plain HTTP forward proxy through WARP SOCKS5 (`Proxy-Authorization` headers are stripped before forwarding).
- `GET /health` (or `/_health`) — proxy & WARP status (`warp_connected`, `proxy_auth_enabled`, `rotate_auth_enabled`).
- `GET /_status` — current egress IP, IPv6 `/64` prefix, and spent-prefix cache size.
- `POST /_rotate` (optional `?force=1`) — rotates WARP keys (`tunnel rotate-keys`) and falls back to `registration delete/new` if WARP keeps returning the same/spent `/64` prefix. When `WARP_ROTATE_TOKEN` is set, accepts either `Authorization: Bearer <token>` or `X-API-Key: <token>`.

```bash
# Check proxy health and current WARP egress IP
curl -s http://127.0.0.1:40001/_status

# Route HTTPS requests through the HTTP proxy
HTTPS_PROXY=http://127.0.0.1:40001 curl -s https://cloudflare.com/cdn-cgi/trace

# Rotate WARP IP on demand (e.g., on HTTP 429)
curl -s -X POST http://127.0.0.1:40001/_rotate
```

### Using with `opencode-openai-bridge`

Configure [`opencode-openai-bridge`](https://github.com/naobyprawira/opencode-openai-bridge) on the same host (`systemd`):

```ini
Environment=BRIDGE_UPSTREAM_PROXY=http://127.0.0.1:40001
Environment=BRIDGE_UPSTREAM_ROTATE_URL=http://127.0.0.1:40001/_rotate
```

Or over a shared Docker Compose network (optionally with proxy Basic auth & rotate token):

```yaml
environment:
  BRIDGE_UPSTREAM_PROXY: "http://warpuser:warppass@warp-rotating-proxy:40001"
  BRIDGE_UPSTREAM_ROTATE_URL: "http://warp-rotating-proxy:40001/_rotate"
  BRIDGE_UPSTREAM_ROTATE_TOKEN: "your-rotate-token"
```

## Configuration (Environment Variables)

| Variable | Default | Description |
|---|---|---|
| `WARP_PROXY_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | HTTP proxy listen host |
| `WARP_PROXY_PORT` | `40001` (falls back to `PORT`) | HTTP proxy & control listen port |
| `WARP_SOCKS_HOST` | `127.0.0.1` | `warp-cli` SOCKS5 host |
| `WARP_SOCKS_PORT` | `40000` | `warp-cli` SOCKS5 port |
| `WARP_CLI_BIN` | *(auto-detected)* | Override path to `warp-cli` executable |
| `WARP_PROTOCOL` | `MASQUE` | WARP tunnel protocol (`MASQUE` required on `warp-cli 2026.8+`) |
| `WARP_ROTATE_COOLDOWN` | `15` | Minimum seconds between non-forced rotations |
| `WARP_BAD_PREFIX_TTL` | `1800` | How long rotated-from IPv6 `/64` prefixes are avoided (seconds) |
| `WARP_CONNECT_TIMEOUT` | `15` | SOCKS5 tunnel connect timeout in seconds |
| `WARP_PROXY_USER` | *(empty)* | Optional Basic auth username for HTTP `CONNECT` & plain-HTTP proxy |
| `WARP_PROXY_PASS` | *(empty)* | Optional Basic auth password for HTTP `CONNECT` & plain-HTTP proxy |
| `WARP_ROTATE_TOKEN` | *(empty)* | Optional secret for `POST /_rotate` (`Authorization: Bearer` or `X-API-Key`) |
| `WARP_PAUSE_FILE` | *(empty)* | Optional lock/pause file path touched while rotating |

## Testing

```bash
python3 -m unittest discover -v
```
