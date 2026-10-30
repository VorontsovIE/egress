"""Client configuration discovery and validation (spec section 29).

Discovery order:
  1. explicit ``--config PATH``
  2. ``EGRESS_CONFIG``
  3. host enrollment socket (per-invoking-UID default profile)
  4. ``/etc/egress/client.yaml``

The client config carries NO secrets: tokens and proxy credentials live in
separate files referenced by path (spec section 37).
"""
from __future__ import annotations

import os
import re
import json
import socket
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/egress/client.yaml"
DEFAULT_ENROLL_SOCKET = "/run/egress-enroll/enroll.sock"
ENV_CONFIG = "EGRESS_CONFIG"

CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._\-]{1,64}")
ENVIRONMENTS = ("host", "container", "vm")


class ConfigError(Exception):
    """Configuration problem (missing file, bad value, unreadable secret)."""


@dataclass
class ClientConfig:
    client_id: str
    control_url: str
    proxy_host: str
    default_proxy_port: int
    oneshot_proxy_port: int
    api_token_file: str
    default_proxy_auth_file: str
    environment: str
    tun_backend: str = "local-userns"
    tun_dns_servers: tuple[str, ...] = ("1.1.1.1", "1.0.0.1")
    extra: dict = field(default_factory=dict)
    _token_value: str | None = field(default=None, repr=False)
    _proxy_auth_value: str | None = field(default=None, repr=False)

    def validate(self) -> None:
        """Structural validation (local, no network)."""
        if not CLIENT_ID_RE.fullmatch(self.client_id):
            raise ConfigError(f"invalid client_id {self.client_id!r}")
        if self.environment not in ENVIRONMENTS:
            raise ConfigError(
                f"environment {self.environment!r} not in {ENVIRONMENTS}"
            )
        for name, port in (
            ("default_proxy_port", self.default_proxy_port),
            ("oneshot_proxy_port", self.oneshot_proxy_port),
        ):
            if not (0 < port < 65536):
                raise ConfigError(f"{name} out of range: {port}")
        if not self.control_url.startswith(("http://", "https://")):
            raise ConfigError(f"control_url must be http(s): {self.control_url!r}")
        if self.tun_backend not in ("none", "local-userns"):
            raise ConfigError(f"tun_backend must be none|local-userns: {self.tun_backend!r}")
        if not self.tun_dns_servers:
            raise ConfigError("tun_dns_servers must contain at least one IP address")
        for server in self.tun_dns_servers:
            try:
                ipaddress.ip_address(server)
            except ValueError as exc:
                raise ConfigError(f"invalid TUN DNS server {server!r}: expected IP address") from exc

    # ---- secrets (read from files, never stored in the yaml) ----
    def api_token(self) -> str:
        if self._token_value is not None:
            return self._token_value
        return self._read_secret(self.api_token_file, "api token")

    def default_proxy_auth(self) -> tuple[str, str]:
        """Return (username, password) from the default-proxy auth file.

        Format: ``user:password`` on a single line.
        """
        raw = self._proxy_auth_value if self._proxy_auth_value is not None else \
            self._read_secret(self.default_proxy_auth_file, "default-proxy auth")
        if ":" not in raw:
            raise ConfigError(
                f"default-proxy auth file {self.default_proxy_auth_file!r} "
                "must contain 'user:password'"
            )
        user, _, password = raw.partition(":")
        return user, password

    @staticmethod
    def _read_secret(path: str, what: str) -> str:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"{what} file not found: {path}")
        try:
            value = p.read_text().strip()
        except OSError as e:
            raise ConfigError(f"cannot read {what} file {path}: {e}") from e
        if not value:
            raise ConfigError(f"{what} file is empty: {path}")
        return value


def _parse_scalar(value: str) -> object:
    value = value.strip()
    if value and (value[0] in "\"'" and value[-1] == value[0]):
        return value[1:-1]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def parse_client_yaml(text: str) -> dict:
    """Minimal flat YAML parser (stdlib-only client: no PyYAML dependency).

    Supports the flat ``key: value`` schema used by client.yaml. Nested
    structures are not part of the client config schema.
    """
    out: dict = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        if ":" not in line:
            raise ConfigError(f"client.yaml line {lineno}: expected 'key: value'")
        key, _, value = line.partition(":")
        key = key.strip()
        if not CLIENT_ID_RE.fullmatch(key):
            raise ConfigError(f"client.yaml line {lineno}: bad key {key!r}")
        out[key] = _parse_scalar(value)
    return out


REQUIRED_KEYS = (
    "client_id",
    "control_url",
    "proxy_host",
    "default_proxy_port",
    "oneshot_proxy_port",
    "api_token_file",
    "default_proxy_auth_file",
    "environment",
)


def load_config(explicit_path: str | None = None) -> ClientConfig:
    """Discover and load the client config (spec section 29 order)."""
    if explicit_path:
        path = Path(explicit_path)
    elif os.environ.get(ENV_CONFIG):
        path = Path(os.environ[ENV_CONFIG])
    elif Path(os.environ.get("EGRESS_ENROLL_SOCKET", DEFAULT_ENROLL_SOCKET)).exists():
        return _load_host_socket()
    else:
        path = Path(DEFAULT_CONFIG_PATH)
    if not path.is_file():
        raise ConfigError(f"client config not found: {path}")
    data = parse_client_yaml(path.read_text())

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ConfigError(f"client config {path} missing keys: {', '.join(missing)}")

    cfg = ClientConfig(
        client_id=str(data["client_id"]),
        control_url=str(data["control_url"]).rstrip("/"),
        proxy_host=str(data["proxy_host"]),
        default_proxy_port=int(data["default_proxy_port"]),
        oneshot_proxy_port=int(data["oneshot_proxy_port"]),
        api_token_file=str(data["api_token_file"]),
        default_proxy_auth_file=str(data["default_proxy_auth_file"]),
        environment=str(data["environment"]),
        tun_backend=str(data.get("tun_backend", "local-userns")),
        tun_dns_servers=tuple(
            item.strip() for item in str(
                data.get("tun_dns_servers", "1.1.1.1,1.0.0.1")
            ).split(",") if item.strip()
        ),
        extra={k: v for k, v in data.items()
               if k not in REQUIRED_KEYS and k not in ("tun_backend", "tun_dns_servers")},
    )
    cfg.validate()
    return cfg


def _load_host_socket() -> ClientConfig:
    path = os.environ.get("EGRESS_ENROLL_SOCKET", DEFAULT_ENROLL_SOCKET)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(30)
            sock.connect(path)
            sock.sendall(b'{"op":"get"}')
            raw = sock.recv(4096)
        data = json.loads(raw)
        if not data.get("ok"):
            raise ConfigError("host enrollment unavailable")
        cfg = ClientConfig(
            client_id=data["client_id"], control_url="http://127.0.0.1:9091",
            proxy_host="127.0.0.1", default_proxy_port=1080,
            oneshot_proxy_port=1081, api_token_file="(local socket)",
            default_proxy_auth_file="(local socket)", environment="host",
            tun_backend=data["tun_backend"], _token_value=data["token"],
            _proxy_auth_value=data["proxy_auth"],
        )
        cfg.validate()
        return cfg
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError("host enrollment unavailable") from exc
