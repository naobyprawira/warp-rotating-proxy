"""Minimal stdlib SOCKS5 client and bidirectional TCP socket relay."""

from __future__ import annotations

import selectors
import socket
import struct


class Socks5Error(OSError):
    """Raised when the upstream SOCKS5 handshake or CONNECT fails."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise Socks5Error("Unexpected EOF from SOCKS5 proxy")
        buf.extend(chunk)
    return bytes(buf)


def open_socks5_tunnel(
    socks_host: str,
    socks_port: int,
    target_host: str,
    target_port: int,
    timeout: float = 15.0,
) -> socket.socket:
    """Connect to target_host:target_port through a SOCKS5 proxy with remote DNS."""
    host_bytes = target_host.encode("idna")
    if len(host_bytes) > 255:
        raise Socks5Error(f"Target hostname too long: {target_host}")

    sock = socket.create_connection((socks_host, socks_port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        # Greeting: VER=5, NMETHODS=1, METHOD=0x00 (No auth)
        sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2)
        if ver != 5 or method != 0:
            raise Socks5Error(f"Unsupported SOCKS5 auth response: ver={ver} method={method}")

        # CONNECT request: VER=5, CMD=1 (CONNECT), RSV=0, ATYP=3 (DOMAINNAME)
        req = (
            b"\x05\x01\x00\x03"
            + bytes([len(host_bytes)])
            + host_bytes
            + struct.pack("!H", target_port)
        )
        sock.sendall(req)

        hdr = _recv_exact(sock, 4)
        if hdr[0] != 5 or hdr[1] != 0:
            raise Socks5Error(f"SOCKS5 CONNECT failed with reply code {hdr[1]}")

        atyp = hdr[3]
        if atyp == 1:  # IPv4
            _recv_exact(sock, 4 + 2)
        elif atyp == 3:  # Domain
            dlen = _recv_exact(sock, 1)[0]
            _recv_exact(sock, dlen + 2)
        elif atyp == 4:  # IPv6
            _recv_exact(sock, 16 + 2)
        else:
            raise Socks5Error(f"Unknown SOCKS5 ATYP {atyp}")

        sock.settimeout(None)
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise


def relay_sockets(
    sock_a: socket.socket, sock_b: socket.socket, bufsize: int = 65536
) -> None:
    """Bidirectionally forward bytes between two connected TCP sockets."""
    sel = selectors.DefaultSelector()
    sock_a.setblocking(False)
    sock_b.setblocking(False)
    sel.register(sock_a, selectors.EVENT_READ, sock_b)
    sel.register(sock_b, selectors.EVENT_READ, sock_a)

    active = {sock_a, sock_b}
    try:
        while len(active) == 2:
            events = sel.select(timeout=60.0)
            if not events:
                continue
            for key, _ in events:
                src: socket.socket = key.fileobj  # type: ignore[assignment]
                dst: socket.socket = key.data
                try:
                    data = src.recv(bufsize)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""

                if not data:
                    try:
                        sel.unregister(src)
                    except Exception:
                        pass
                    active.discard(src)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue

                view = memoryview(data)
                sent = 0
                dst.setblocking(True)
                try:
                    while sent < len(data):
                        sent += dst.send(view[sent:])
                except OSError:
                    return
                finally:
                    dst.setblocking(False)
    finally:
        sel.close()
        for s in (sock_a, sock_b):
            try:
                s.close()
            except OSError:
                pass
