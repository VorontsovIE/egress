"""Bounded subscription downloads through direct or isolated SOCKS transport."""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

MAX_BYTES = 4 * 1024 * 1024


def normalize_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid subscription URL")
    return value


def direct_fetch(url: str) -> bytes:
    with urlopen(Request(normalize_url(url)), timeout=30) as response:
        if response.status != 200:
            raise ValueError("subscription HTTP status")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("subscription too large")
    return data


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("SOCKS connection closed")
        data += chunk
    return data


def _socks_connect(proxy_host: str, proxy_port: int, user: str, password: str,
                   host: str, port: int) -> socket.socket:
    sock = socket.create_connection((proxy_host, proxy_port), timeout=30)
    try:
        sock.sendall(b"\x05\x01\x02")
        if _recv_exact(sock, 2) != b"\x05\x02":
            raise OSError("SOCKS authentication unavailable")
        username, secret = user.encode(), password.encode()
        if len(username) > 255 or len(secret) > 255:
            raise ValueError("SOCKS credentials too long")
        sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(secret)]) + secret)
        if _recv_exact(sock, 2) != b"\x01\x00":
            raise OSError("SOCKS authentication failed")
        hostname = host.encode("idna")
        if len(hostname) > 255:
            raise ValueError("subscription host too long")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hostname)]) + hostname + port.to_bytes(2, "big"))
        head = _recv_exact(sock, 4)
        if head[:2] != b"\x05\x00":
            raise OSError("SOCKS connect failed")
        length = {1: 4, 4: 16}.get(head[3])
        if head[3] == 3:
            length = _recv_exact(sock, 1)[0]
        if length is None:
            raise OSError("SOCKS address invalid")
        _recv_exact(sock, length + 2)
        return sock
    except Exception:
        sock.close()
        raise


def socks_fetch(url: str, proxy_host: str, proxy_port: int,
                user: str, password: str) -> bytes:
    """Fetch via one authenticated SOCKS slot, including bounded redirects."""
    url = normalize_url(url)
    for _ in range(4):
        target = urlsplit(url)
        host = target.hostname
        assert host is not None
        port = target.port or (443 if target.scheme == "https" else 80)
        sock = _socks_connect(proxy_host, proxy_port, user, password, host, port)
        try:
            if target.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nUser-Agent: egress/1\r\n\r\n"
            sock.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(sock)
            response.begin()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("subscription redirect missing location")
                url = normalize_url(urljoin(url, location))
                continue
            if response.status != 200:
                raise ValueError("subscription HTTP status")
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("subscription too large")
            return data
        finally:
            sock.close()
    raise ValueError("too many subscription redirects")
