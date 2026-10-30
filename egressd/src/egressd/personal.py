"""Private provider inventory shared by the public API and config generator.

Only egressd writes this directory.  Provider IDs are derived from the
authenticated client ID, never accepted from a request body.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import yaml

ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_PROVIDERS = 32
ALLOWED_TYPES = {"vless", "vmess", "trojan", "ss", "hysteria2", "tuic",
                 "wireguard", "socks5", "http"}
FORBIDDEN_FIELDS = {"dialer-proxy", "proxy", "interface-name", "routing-mark",
                    "ip-version", "plugin-path"}


def state_dir() -> Path:
    return Path(os.environ.get("EGRESSD_PERSONAL_DIR", "/var/lib/egressd/personal"))


def provider_id(client_id: str, alias: str) -> str:
    if not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id) or \
            not ALIAS_RE.fullmatch(alias):
        raise ValueError("invalid personal provider name")
    result = f"p-{client_id}-{alias}"
    if len(result) > 99:
        raise ValueError("personal provider name is too long")
    return result


def metadata_path(client_id: str, alias: str) -> Path:
    return state_dir() / f"{provider_id(client_id, alias)}.json"


def load_metadata(provider: str) -> dict | None:
    try:
        value = json.loads((state_dir() / f"{provider}.json").read_text())
        return value if isinstance(value, dict) and value.get("provider_id") == provider else None
    except (OSError, ValueError):
        return None


def list_owned(client_id: str) -> list[dict]:
    result = []
    try:
        for path in state_dir().glob("*.json"):
            value = load_metadata(path.stem)
            if value and value.get("client_id") == client_id:
                result.append(value)
    except OSError:
        pass
    return sorted(result, key=lambda x: x["alias"])


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix=".personal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_metadata(value: dict) -> None:
    atomic_bytes(state_dir() / f"{value['provider_id']}.json",
                 json.dumps(value, sort_keys=True).encode())


def _public_addresses(host: str) -> list[str]:
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        if host.lower() in {"localhost", "host.docker.internal"} or host.endswith(".local"):
            return []
        try:
            ips = [ipaddress.ip_address(item[4][0]) for item in
                   socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
        except (OSError, ValueError):
            return []
    return [str(ip) for ip in ips] if ips and all(ip.is_global for ip in ips) else []


def _public_host(host: str) -> bool:
    return bool(_public_addresses(host))


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or \
            parsed.username or parsed.password or len(url) > 4096 or \
            not _public_host(parsed.hostname):
        raise ValueError("subscription URL must use a public HTTP(S) endpoint")


def fetch_url(url: str) -> bytes:
    """Fetch with a public DNS answer pinned for each TCP connection."""
    for _ in range(6):
        validate_url(url)
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        addresses = _public_addresses(host)
        if not addresses:
            raise ValueError("subscription address is not public")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = addresses[0]

        class PinnedHTTP(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.create_connection((address, port), timeout=30)

        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                raw = socket.create_connection((address, port), timeout=30)
                self.sock = self._context.wrap_socket(raw, server_hostname=host)

        conn = (PinnedHTTPS(host, port, timeout=30, context=ssl.create_default_context())
                if parsed.scheme == "https" else PinnedHTTP(host, port, timeout=30))
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.request("GET", path, headers={"User-Agent": "egress/1"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                target = response.getheader("Location")
                if not target:
                    raise ValueError("subscription redirect has no target")
                url = urljoin(url, target)
                continue
            if response.status != 200:
                raise ValueError("subscription HTTP status")
            data = response.read(MAX_SOURCE_BYTES + 1)
            if len(data) > MAX_SOURCE_BYTES:
                raise ValueError("subscription too large")
            return data
        finally:
            conn.close()
    raise ValueError("too many subscription redirects")


def validate_yaml(text: str) -> tuple[str, int, int]:
    if len(text.encode()) > MAX_SOURCE_BYTES:
        raise ValueError("personal provider too large")
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError("personal provider needs a proxies list")
    proxies = data["proxies"]
    if not proxies:
        raise ValueError("personal provider needs at least one node")
    seen = set()
    accepted = []
    skipped = 0
    resolved: dict[str, list[str]] = {}
    for proxy in proxies:
        if not isinstance(proxy, dict) or proxy.get("type") not in ALLOWED_TYPES:
            raise ValueError("unsupported personal proxy type")
        if FORBIDDEN_FIELDS.intersection(proxy):
            raise ValueError("unsupported personal proxy option")
        name, host = proxy.get("name"), proxy.get("server")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or name in seen:
            raise ValueError("invalid personal proxy name")
        if not isinstance(host, str):
            raise ValueError("personal proxy needs a public server")
        try:
            port = int(proxy.get("port"))
        except (ValueError, TypeError):
            port = 0
        if not 1 <= port <= 65535:
            raise ValueError("invalid personal proxy port")
        if host not in resolved:
            resolved[host] = _public_addresses(host)
        addresses = resolved[host]
        if not addresses:
            skipped += 1
            continue
        seen.add(name)
        # Mihomo must never resolve a personal server name again at dial time:
        # a later DNS answer could point at a host/private address. Preserve
        # the original hostname for protocols that use it as TLS SNI.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if proxy["type"] in {"vless", "vmess", "trojan"} and \
                    not proxy.get("servername"):
                proxy["servername"] = host
            elif proxy["type"] in {"hysteria2", "tuic"} and not proxy.get("sni"):
                proxy["sni"] = host
            elif proxy["type"] == "http" and proxy.get("tls") and not proxy.get("sni"):
                proxy["sni"] = host
            proxy["server"] = addresses[0]
        accepted.append(proxy)
    if not accepted:
        raise ValueError("personal provider needs at least one public node")
    normalized = yaml.safe_dump({"proxies": accepted}, sort_keys=False)
    if len(normalized.encode()) > MAX_SOURCE_BYTES:
        raise ValueError("personal provider too large")
    return normalized, len(accepted), skipped
