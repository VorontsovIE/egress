"""Mihomo configuration generator for vibelab-egress.

Produces the complete Mihomo config from declarative inputs:
provider definitions, client definitions, one-shot slot allocation and
hard-direct CIDRs.

Design (validated in PoC, see POC_REPORT.md):

* One default SOCKS listener with one authenticated user per client.
  ``IN-USER,<client>,<client>`` routes that user's traffic to a
  per-client ``select`` group. Each client therefore has an isolated
  selector; changing one client's default never touches another.
* One one-shot SOCKS listener with ``oneshot_slots`` pre-allocated users
  (``os_0..os_N-1``), each mapped to its own ``select`` group via
  ``IN-USER``. egressd allocates a slot per session and points the
  slot group at the requested route. Rule count is fixed (independent of
  route count), so the config is stable and concurrent sessions cannot
  race on a shared selector.
* Hard-direct CIDRs (LAN, Docker, operator-defined) are placed BEFORE the
  IN-USER rules so internal destinations stay DIRECT on every path,
  including one-shot.
* ``profile.store-selected: true`` (default) persists selector selections
  in the bbolt cache file and restores them on config reload, so provider
  inventory updates (hot reload) do not reset client defaults.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DIRECT = "DIRECT"

# Built-in hard-direct CIDRs that must never be proxied.
DEFAULT_HARD_DIRECT_CIDRS = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
]

# fake-ip range used by the built-in DNS resolver.
FAKE_IP_RANGE = "198.18.0.0/16"


def _safe_name(name: str) -> str:
    """Sanitize a proxy/provider name for use in group members."""
    return re.sub(r"[^A-Za-z0-9._\-]", "_", name)


@dataclass
class ProviderDef:
    """A proxy provider (subscription or local file)."""

    name: str
    type: str = "file"  # "file" | "http"
    path: str | None = None
    url: str | None = None
    prefix: str | None = None  # override.additional-prefix
    interval: int = 3600
    health_check: bool = False
    health_check_url: str = "https://www.gstatic.com/generate_204"
    header: dict[str, Any] | None = None
    filter: str | None = None
    exclude_filter: str | None = None
    exclude_type: str | None = None
    payload: list[dict[str, Any]] | None = None  # inline fallback nodes

    def to_config(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {"type": self.type}
        if self.type == "file":
            if not self.path:
                raise ValueError(f"provider {self.name}: file provider needs path")
            cfg["path"] = self.path
        elif self.type == "http":
            if not self.url:
                raise ValueError(f"provider {self.name}: http provider needs url")
            cfg["url"] = self.url
            cfg["interval"] = self.interval
            if self.header:
                cfg["header"] = self.header
        else:
            raise ValueError(f"provider {self.name}: unknown type {self.type}")
        if self.prefix:
            cfg["override"] = {"additional-prefix": self.prefix}
        if self.filter:
            cfg["filter"] = self.filter
        if self.exclude_filter:
            cfg["exclude-filter"] = self.exclude_filter
        if self.exclude_type:
            cfg["exclude-type"] = self.exclude_type
        if self.health_check:
            cfg["health-check"] = {
                "enable": True,
                "url": self.health_check_url,
                "interval": 300,
                "timeout": 5000,
                "lazy": True,
            }
        if self.payload:
            cfg["payload"] = self.payload
        return cfg


@dataclass
class ClientDef:
    """A client profile: one selector group + one default-listener user."""

    client_id: str
    proxy_user: str
    proxy_password: str
    description: str = ""


@dataclass
class OneShotSlot:
    """A pre-allocated one-shot listener user + selector group."""

    index: int
    user: str
    password: str

    @property
    def group(self) -> str:
        return f"os_{self.index}"


@dataclass
class EgressConfig:
    mihomo_secret: str
    clients: list[ClientDef]
    providers: list[ProviderDef]
    oneshot_slots: list[OneShotSlot] = field(default_factory=list)
    default_port: int = 18080
    oneshot_port: int = 18081
    # Where the SOCKS listeners bind INSIDE the process.
    #  - host / PoC: "127.0.0.1" (loopback only)
    #  - container:  "0.0.0.0" (the container netns is isolated; the host-side
    #    publish address in compose.yaml is what enforces "no public binds")
    listener_listen: str = "127.0.0.1"
    # Admin API must be reachable from egressd, which runs in its OWN
    # container on the compose network: 127.0.0.1 inside the mihomo
    # container is unreachable from there (2026-10-15 live run: mihomo
    # unreachable -> no version, all probes failed, selector switches
    # unverifiable). Port 9090 is never published (compose.yaml) and every
    # call is Bearer-authenticated with the mihomo secret, so 0.0.0.0 on
    # the private compose network is safe.
    controller: str = "0.0.0.0:9090"
    dns_listen: str = "127.0.0.1:1053"
    hard_direct_cidrs: list[str] = field(
        default_factory=lambda: list(DEFAULT_HARD_DIRECT_CIDRS)
    )
    # geox-url: update sources. Mihomo auto-detects GeoIP.dat / GeoSite.dat /
    # ASN.mmdb in its home dir and only downloads from these URLs when the
    # files are missing or on an explicit geo update. GEO is optional (spec
    # 14.3): if the download is unavailable mihomo still runs, just without
    # GEO data. NOTE: the upstream release tag is `latest` (a moving release
    # updated in place); the old `release` tag no longer exists (404).
    geo_ip_url: str = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat"
    geo_site_url: str = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat"
    geo_asn_url: str = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/GeoLite2-ASN.mmdb"
    geo_mode: bool = True  # True = memory mode (geodata-mode: true)
    # GEO auto-update (spec 14.1): Mihomo re-downloads the datasets from
    # geox-url on this interval (hours) when files are stale/missing.
    geo_auto_update: bool = True
    geo_update_interval: int = 24
    tun: dict[str, Any] | None = None
    udp: bool = True
    log_level: str = "info"
    dns_nameservers: list[str] = field(
        default_factory=lambda: ["223.5.5.5", "8.8.8.8"]
    )
    dns_fallback: list[str] = field(
        default_factory=lambda: ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"]
    )
    health_url: str = "https://www.gstatic.com/generate_204"
    static_proxies: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> None:
        seen = set()
        for c in self.clients:
            if c.client_id in seen:
                raise ValueError(f"duplicate client_id {c.client_id}")
            seen.add(c.client_id)
            if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", c.client_id):
                raise ValueError(f"invalid client_id {c.client_id!r}")
        for s in self.oneshot_slots:
            if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", s.user):
                raise ValueError(f"invalid oneshot user {s.user!r}")
        for p in self.providers:
            if not re.fullmatch(r"[A-Za-z0-9._\-]{1,64}", p.name):
                raise ValueError(f"invalid provider name {p.name!r}")
        names = {p.name for p in self.providers}
        if len(names) != len(self.providers):
            raise ValueError("duplicate provider names")
        if "DIRECT" in names:
            raise ValueError("provider name 'DIRECT' is reserved")
        for c in self.clients:
            if c.client_id in names:
                raise ValueError(f"client_id {c.client_id} collides with provider name")
        users = {c.proxy_user for c in self.clients}
        for s in self.oneshot_slots:
            if s.user in users or any(c.client_id == s.user for c in self.clients):
                raise ValueError(f"oneshot user {s.user} collides with a client user")

    def _group_def(self) -> dict[str, Any]:
        """A select group containing DIRECT + all provider nodes + statics.

        Provider nodes are pulled in via the ``use`` field (mihomo adds the
        provider's proxies as group members); static/DIRECT members go in
        ``proxies``.
        """
        members = [DIRECT] + [sp["name"] for sp in self.static_proxies]
        g: dict[str, Any] = {"type": "select", "proxies": members}
        if self.providers:
            g["use"] = [p.name for p in self.providers]
        return g

    def generate(self) -> dict[str, Any]:
        self.validate()
        cfg: dict[str, Any] = {
            "mixed-port": 0,  # disabled; we use named listeners
            "allow-lan": False,
            "mode": "rule",
            "log-level": self.log_level,
            "ipv6": False,
            "external-controller": self.controller,
            "secret": self.mihomo_secret,
            "profile": {
                "store-selected": True,
                "store-fake-ip": False,
            },
            "geox-url": {
                "geoip": self.geo_ip_url,
                "geosite": self.geo_site_url,
                "mmdb": self.geo_asn_url,
                "asn": self.geo_asn_url,
            },
            "geodata-mode": self.geo_mode,
            "geo-auto-update": self.geo_auto_update,
            "geo-update-interval": self.geo_update_interval,
            "dns": {
                "enable": True,
                "listen": self.dns_listen,
                "ipv6": False,
                "enhanced-mode": "fake-ip",
                "fake-ip-range": FAKE_IP_RANGE,
                "fake-ip-filter": [
                    "lan",
                    "lan.domain",
                    "*.local",
                    "localhost.pt",
                    "stun.*",
                ],
                "default-nameserver": self.dns_nameservers,
                "nameserver": self.dns_nameservers,
                "fallback": self.dns_fallback,
                "fallback-filter": {
                    "geoip": True,
                    "geoip-code": "CN",
                },
            },
            "listeners": [
                {
                    "name": "default-proxy",
                    "type": "socks",
                    "port": self.default_port,
                    "listen": self.listener_listen,
                    "udp": self.udp,
                    "users": [
                        {"username": c.proxy_user, "password": c.proxy_password}
                        for c in self.clients
                    ],
                },
                {
                    "name": "oneshot-proxy",
                    "type": "socks",
                    "port": self.oneshot_port,
                    "listen": self.listener_listen,
                    "udp": self.udp,
                    "users": [
                        {"username": s.user, "password": s.password}
                        for s in self.oneshot_slots
                    ],
                },
            ],
            # DIRECT/REJECT are built-in and auto-registered by the parser;
            # they must NOT be declared in `proxies`.
            "proxies": list(self.static_proxies),
            "proxy-providers": {p.name: p.to_config() for p in self.providers},
            "proxy-groups": [
                {"name": c.client_id, **self._group_def()}
                for c in self.clients
            ]
            + [
                {"name": s.group, **self._group_def()}
                for s in self.oneshot_slots
            ],
        }
        if self.tun:
            cfg["tun"] = self.tun
        cfg["rules"] = self._rules()
        return cfg

    def _rules(self) -> list[str]:
        rules: list[str] = []
        # 1. Hard-direct: internal destinations stay DIRECT on every path.
        rules.append("GEOIP,LAN,DIRECT,no-resolve")
        for cidr in self.hard_direct_cidrs:
            rules.append(f"IP-CIDR,{cidr},DIRECT,no-resolve")
        # 2. Client routing by inbound username.
        for c in self.clients:
            rules.append(f"IN-USER,{c.proxy_user},{c.client_id}")
        for s in self.oneshot_slots:
            rules.append(f"IN-USER,{s.user},{s.group}")
        # 3. Safe fallback for any other inbound (e.g. future TUN without
        #    an explicit rule): never implicit VPN.
        rules.append(f"MATCH,{DIRECT}")
        return rules


def default_oneshot_slots(count: int, secrets: list[str]) -> list[OneShotSlot]:
    if len(secrets) != count:
        raise ValueError("secrets length must equal slot count")
    return [
        OneShotSlot(index=i, user=f"os_{i}", password=secrets[i])
        for i in range(count)
    ]
