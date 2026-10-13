"""Client configuration discovery and validation (spec section 29).

Discovery order:
  1. explicit ``--config PATH``
  2. ``VIBELAB_EGRESS_CONFIG``
  3. ``/etc/vibelab-egress/client.yaml``

The client config carries NO secrets: tokens and proxy credentials live in
separate files referenced by path (spec section 37).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/vibelab-egress/client.yaml"
ENV_CONFIG = "VIBELAB_EGRESS_CONFIG"

CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._\-]{1,64}")
ENVIRONMENTS = ("host", "gvisor-container", "container", "vm")


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
    tun_backend: str = "none"
    extra: dict = field(default_factory=dict)

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
        if self.tun_backend not in ("none", "host-helper"):
            raise ConfigError(f"tun_backend must be none|host-helper: {self.tun_backend!r}")

    # ---- secrets (read from files, never stored in the yaml) ----
    def api_token(self) -> str:
        return self._read_secret(self.api_token_file, "api token")

    def default_proxy_auth(self) -> tuple[str, str]:
        """Return (username, password) from the default-proxy auth file.

        Format: ``user:password`` on a single line.
        """
        raw = self._read_secret(self.default_proxy_auth_file, "default-proxy auth")
        if ":" not in raw:
            raise ConfigError(
                f"default-proxy auth file {self.default_proxy_auth_file!r} "
                "must contain 'user:password'"
            )
        user, _, password = raw.partition(":")
        return user, password

    def tun_helper_socket(self) -> str:
        """Path of the local strict/TUN helper UNIX socket (A6).

        Resolution: ``tun_helper_socket`` in the config, else the
        ``VIBELAB_EGRESS_TUN_HELPER_SOCKET`` env var, else the default. Only
        meaningful when ``tun_backend == host-helper``.
        """
        return (
            str(self.extra.get("tun_helper_socket"))
            or os.environ.get("VIBELAB_EGRESS_TUN_HELPER_SOCKET")
            or "/run/vibelab-egress/tun-helper.sock"
        )

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
        tun_backend=str(data.get("tun_backend", "none")),
        extra={k: v for k, v in data.items() if k not in REQUIRED_KEYS and k != "tun_backend"},
    )
    cfg.validate()
    return cfg
