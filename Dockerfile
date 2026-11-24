FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    WARP_PROXY_HOST=0.0.0.0 \
    WARP_PROXY_PORT=40001 \
    WARP_SOCKS_HOST=127.0.0.1 \
    WARP_SOCKS_PORT=40000 \
    WARP_PROTOCOL=MASQUE

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dbus \
        gnupg \
        lsb-release \
        python3 \
    && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY \
    proxy_config.py \
    socks5_tunnel.py \
    warp_rotator.py \
    warp_rotating_proxy.py \
    docker-entrypoint.sh \
    ./

RUN chmod 755 /app/docker-entrypoint.sh /app/warp_rotating_proxy.py

VOLUME ["/var/lib/cloudflare-warp"]
EXPOSE 40001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import os, urllib.request; p = os.environ.get('WARP_PROXY_PORT') or os.environ.get('PORT') or '40001'; urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
