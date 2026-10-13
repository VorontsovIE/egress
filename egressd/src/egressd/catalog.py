"""Route catalog (spec section 13).

Maps the agent-facing **stable route ID** to the Mihomo node name and to
route metadata.

Stable route ID format (spec 13):

    <provider>:<stable-id>        e.g.  blanc:fi-helsinki-03
    direct                         synthetic builtin route

The stable-id is the adapter's clean node name (``stable_slug`` of the display
name), which is deterministic and independent of mutable display-name
punctuation/case (spec 13: "Stable route ID не должен зависеть только от
изменяемого display name").

The Mihomo node name is the provider tag (``override.additional-prefix``,
spec 11.1) applied to the clean name:

    mihomo name  =  <prefix><clean>          e.g.  blanc-fi-helsinki-03

So the catalog is a pure, deterministic mapping that egressd can build from
the provider files it controls (no dependency on Mihomo's async provider
loading). Health/latency are enriched from Mihomo at runtime when available
(best-effort -- the catalog structure is testable offline).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DIRECT = "direct"
DIRECT_MIHOMO = "DIRECT"


@dataclass
class RouteInfo:
    """Agent-facing route object (spec section 13)."""

    id: str
    provider: str
    display_name: str
    protocol: str = "unknown"
    country: str | None = None
    city: str | None = None
    # runtime-enriched (best-effort from mihomo / probes)
    healthy: bool | None = None
    latency_ms: int | None = None
    exit_ip: str | None = None
    exit_country: str | None = None
    asn: int | None = None
    last_test_at: str | None = None
    benchmark_mbps: float | None = None
    benchmark_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "country": self.country,
            "city": self.city,
            "protocol": self.protocol,
            "healthy": self.healthy,
            "latency_ms": self.latency_ms,
            "exit_ip": self.exit_ip,
            "exit_country": self.exit_country,
            "asn": self.asn,
            "last_test_at": self.last_test_at,
            "benchmark_mbps": self.benchmark_mbps,
            "benchmark_at": self.benchmark_at,
        }


def _guess_geo(display: str) -> tuple[str | None, str | None]:
    """Best-effort (country, city) from a display name.

    The adapter's stable names start with the 2-letter country code in the
    fixtures ("FI - Helsinki 1" -> "FI-Helsinki-1"). This is a heuristic for
    catalog classification/explanation only (spec 14.2); it is NOT a source of
    truth -- real exit-country verification comes from a probe + GEO data
    (spec 14.3).
    """
    parts = [p for p in display.replace("-", " ").split() if p]
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        cc = parts[0].upper()
        city = " ".join(parts[1:]) if len(parts) > 1 else None
        return cc, city or None
    return None, None


class RouteCatalog:
    """Deterministic route ID <-> mihomo name mapping + metadata.

    Built from the provider files egressd controls:

        providers : {provider_name: [clean_node_name, ...]}
        prefixes  : {provider_name: mihomo_additional_prefix}   (e.g. "blanc-")

    ``direct`` is always present as a synthetic route.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, RouteInfo] = {}
        self._mihomo_to_id: dict[str, str] = {}
        self._id_to_mihomo: dict[str, str] = {}
        self._prefixes: dict[str, str] = {}

    # ---- construction -----------------------------------------------------
    def build(
        self,
        providers: dict[str, list[str]],
        prefixes: dict[str, str] | None = None,
        protocols: dict[str, dict[str, str]] | None = None,
    ) -> "RouteCatalog":
        """Rebuild the catalog. ``protocols`` maps provider -> {clean: type}."""
        prefixes = prefixes or {}
        protocols = protocols or {}
        by_id: dict[str, RouteInfo] = {}
        mihomo_to_id: dict[str, str] = {}
        id_to_mihomo: dict[str, str] = {}

        # synthetic direct route
        by_id[DIRECT] = RouteInfo(
            id=DIRECT, provider="builtin", display_name="Direct", protocol="direct"
        )
        mihomo_to_id[DIRECT_MIHOMO] = DIRECT
        id_to_mihomo[DIRECT] = DIRECT_MIHOMO

        for prov, clean_names in providers.items():
            prefix = prefixes.get(prov, f"{prov}-")
            self._prefixes[prov] = prefix
            for clean in clean_names:
                rid = f"{prov}:{clean}"
                display = clean
                cc, city = _guess_geo(clean)
                by_id[rid] = RouteInfo(
                    id=rid,
                    provider=prov,
                    display_name=display,
                    protocol=protocols.get(prov, {}).get(clean, "unknown"),
                    country=cc,
                    city=city,
                )
                mihomo_name = f"{prefix}{clean}"
                mihomo_to_id[mihomo_name] = rid
                id_to_mihomo[rid] = mihomo_name

        self._by_id = by_id
        self._mihomo_to_id = mihomo_to_id
        self._id_to_mihomo = id_to_mihomo
        return self

    # ---- lookups ----------------------------------------------------------
    def route_id(self, provider: str, clean: str) -> str:
        return f"{provider}:{clean}"

    def mihomo_name(self, route_id: str) -> str:
        """route ID -> mihomo node name (for PUT /proxies/<group>).

        The mihomo name carries the provider tag (``additional-prefix``) and uses
        the clean node name -- e.g. route ``blanc:FI-Helsinki-1`` ->
        ``blanc-FI-Helsinki-1``. The route ID itself (with the ``:``) is NOT a
        valid mihomo node name, so this must be an explicit map, not a fallback.
        """
        return self._id_to_mihomo.get(route_id, route_id)

    def from_mihomo(self, name: str) -> str | None:
        """mihomo node name -> route ID (None if unknown)."""
        return self._mihomo_to_id.get(name)

    def get(self, route_id: str) -> RouteInfo | None:
        return self._by_id.get(route_id)

    def exists(self, route_id: str) -> bool:
        return route_id in self._by_id

    def all_route_ids(self) -> list[str]:
        return list(self._by_id.keys())

    def all(self) -> list[RouteInfo]:
        return list(self._by_id.values())

    def providers(self) -> list[str]:
        return sorted({r.provider for r in self._by_id.values() if r.provider != "builtin"})

    def filter(
        self,
        country: str | None = None,
        provider: str | None = None,
    ) -> list[RouteInfo]:
        out = []
        for r in self._by_id.values():
            if r.id == DIRECT:
                # direct is always a candidate when no provider filter is set
                if provider is not None and provider != "builtin":
                    continue
            if provider is not None and r.provider != provider:
                continue
            if country is not None and (r.country or "").upper() != country.upper():
                continue
            out.append(r)
        return out

    # ---- runtime enrichment (best-effort, from mihomo) -------------------
    def enrich(self, mihomo_nodes: dict[str, dict[str, Any]]) -> None:
        """Fold mihomo's live node view into the catalog.

        ``mihomo_nodes`` maps mihomo node name -> {healthy, latency_ms, ...}.
        Unknown names are ignored (they are not in the catalog).
        """
        for name, meta in mihomo_nodes.items():
            rid = self._mihomo_to_id.get(name)
            if rid is None:
                continue
            r = self._by_id[rid]
            if "healthy" in meta:
                r.healthy = bool(meta["healthy"])
            if meta.get("latency_ms") is not None:
                r.latency_ms = int(meta["latency_ms"])
            if meta.get("last_test_at"):
                r.last_test_at = meta["last_test_at"]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {rid: r.to_dict() for rid, r in self._by_id.items()}
