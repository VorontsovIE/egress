"""Route ranking for ``egressctl best`` (spec section 26).

The ranking is **transparent**: every candidate is scored with an explicit
formula and the result is returned with a per-route ``explanation`` so the
agent (and a human) can see exactly why a route won.

Default ranking (spec 26), in priority order:

  1. filters: country / provider (hard filter);
  2. health: a route is a candidate only if it is known-healthy or has no
     health data yet (never-tested routes are candidates; known-unhealthy
     routes are excluded);
  3. domain reachability (only when ``--domain`` is given): the route must
     have a **fresh, successful** probe for that domain, or it must be in the
     on-demand probed shortlist (the caller supplies those probe outcomes);
  4. freshness: a stale health sample is worth less than a fresh one;
  5. latency: lower measured delay wins;
  6. optional throughput score (``--speed-aware``): recent benchmark mbps;
  7. GEO verification/confidence: verified exit country beats a display-name
     heuristic beats unknown.

The score is a weighted sum of normalized components so the formula is
deterministic and inspectable::

    score = (w_health   * health_score
           + w_fresh    * freshness_score
           + w_latency  * latency_score
           + w_through  * throughput_score      (0 unless speed-aware)
           + w_geo      * geo_score)

with weights (speed-aware off):  health 40, freshness 20, latency 30, geo 10,
and (speed-aware on):            health 35, freshness 15, latency 25,
                                 throughput 15, geo 10.

All normalized components are in [0, 1]; a missing measurement scores 0 for
that component (a route that has never been tested is ranked by what it does
have, and ties fall back to the stable route ID).
"""
from __future__ import annotations

from datetime import datetime
import time
from typing import Any

# component weights (must sum to 100 within each mode)
WEIGHTS_DEFAULT = {
    "health": 40.0,
    "freshness": 20.0,
    "latency": 30.0,
    "throughput": 0.0,
    "geo": 10.0,
}
WEIGHTS_SPEED_AWARE = {
    "health": 35.0,
    "freshness": 15.0,
    "latency": 25.0,
    "throughput": 15.0,
    "geo": 10.0,
}

# a health sample older than this is treated as fully stale
FRESHNESS_WINDOW_S = 3600.0
# a domain probe is "fresh" for best --domain if it is younger than this
PROBE_FRESH_S = 900.0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def health_score(healthy: bool | None, tested_at: float | None, now: float) -> float:
    """1.0 for known-healthy, 0.5 for never-tested, 0.0 for known-unhealthy."""
    if healthy is True:
        return 1.0
    if healthy is None:
        return 0.5
    return 0.0


def freshness_score(tested_at: float | str | None, now: float) -> float:
    """1.0 for a sample taken now, 0.0 for a sample at or beyond the window."""
    if tested_at is None:
        return 0.0
    if isinstance(tested_at, str):
        try:
            tested_at = datetime.fromisoformat(tested_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    age = max(0.0, now - tested_at)
    return _clamp01(1.0 - age / FRESHNESS_WINDOW_S)


def latency_score(latency_ms: int | None, best_ms: int | None) -> float:
    """1.0 for the fastest measured route, 0.0 for the unmeasured.

    Normalization is the ratio ``best_ms / latency_ms`` (relative speed): the
    fastest route in the field scores 1.0, a route twice as slow scores 0.5.
    ``best_ms`` is the minimum latency across the candidate set (the caller
    computes it so the normalization is relative to the current field, not to
    an absolute constant).
    """
    if latency_ms is None or best_ms is None or best_ms <= 0:
        return 0.0
    if latency_ms <= 0:
        return 1.0
    return _clamp01(best_ms / latency_ms)


def throughput_score(mbps: float | None, best_mbps: float | None) -> float:
    """1.0 for the fastest benchmark, 0.0 for the slowest / unmeasured."""
    if mbps is None or best_mbps is None or best_mbps <= 0:
        return 0.0
    return _clamp01(mbps / best_mbps)


def geo_score(route: Any, geo_cache: dict[str, Any] | None = None) -> float:
    """GEO verification confidence (spec 26 tier 7).

    1.0 -- exit country verified (probe + GEO data: catalog enrichment or a
           geo_cache entry for this route);
    0.5 -- display-name heuristic only (catalog ``country`` from the node
           name; not a verification, spec 14.2);
    0.0 -- unknown.
    """
    if getattr(route, "exit_country", None) or (
        geo_cache is not None and geo_cache.get(route.id)
    ):
        return 1.0
    if getattr(route, "country", None):
        return 0.5
    return 0.0


def is_candidate(
    route: Any,
    now: float,
    domain: str | None = None,
    fresh_probes: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    """Apply the hard candidate filters (spec 26 tiers 1-3).

    Returns ``(ok, reason)`` where ``reason`` is set when the route is
    excluded (for the explanation).
    """
    if route.healthy is False:
        return False, "unhealthy"
    if domain and fresh_probes is not None:
        p = fresh_probes.get(route.id)
        if p is None:
            return False, f"no fresh probe for {domain}"
        if not p.get("reachable"):
            return False, f"probe for {domain} failed ({p.get('error')})"
    return True, None


def rank_routes(
    routes: list[Any],
    now: float | None = None,
    domain: str | None = None,
    fresh_probes: dict[str, dict[str, Any]] | None = None,
    speed_aware: bool = False,
    max_latency_ms: int | None = None,
    geo_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank ``routes`` and return the full transparent result.

    ``routes`` are catalog :class:`~egressd.catalog.RouteInfo` objects (already
    filtered by country/provider by the caller). ``fresh_probes`` maps route id
    -> probe outcome dict (``reachable``, ``error``, ``ts``) for the requested
    domain; when ``domain`` is set, only routes with a fresh successful probe
    (or an on-demand probe supplied by the caller) are candidates.

    Returns::

        {
          "best": route_id | None,
          "count": N,
          "candidates": [
            {"rank": 1, "route": id, "score": 87.3,
             "components": {"health": 40.0, "freshness": 12.5, ...},
             "latency_ms": 42, "healthy": true, "throughput_mbps": 81.2,
             "geo": "verified|heuristic|unknown",
             "explanation": "healthy(40.0) + fresh(12.5) + latency(28.0) + geo(5.0)"},
            ...
          ],
          "excluded": [{"route": id, "reason": "..."}, ...],
          "formula": {"weights": {...}, "freshness_window_s": 3600.0},
        }
    """
    now = now if now is not None else time.time()
    weights = WEIGHTS_SPEED_AWARE if speed_aware else WEIGHTS_DEFAULT

    candidates: list[Any] = []
    excluded: list[dict[str, str]] = []
    for r in routes:
        ok, reason = is_candidate(r, now, domain=domain, fresh_probes=fresh_probes)
        if max_latency_ms is not None and r.latency_ms is not None \
                and r.latency_ms > max_latency_ms:
            ok, reason = False, f"latency {r.latency_ms}ms > max {max_latency_ms}ms"
        if ok:
            candidates.append(r)
        else:
            excluded.append({"route": r.id, "reason": reason or "filtered"})

    best_ms = min((r.latency_ms for r in candidates
                   if r.latency_ms is not None and r.latency_ms > 0),
                  default=None)
    best_mbps = max((r.benchmark_mbps for r in candidates
                     if r.benchmark_mbps is not None and r.benchmark_mbps > 0),
                    default=None)

    scored: list[dict[str, Any]] = []
    for r in candidates:
        comps = {
            "health": weights["health"] * health_score(r.healthy, r.last_test_at, now),
            "freshness": weights["freshness"] * freshness_score(r.last_test_at, now),
            "latency": weights["latency"] * latency_score(r.latency_ms, best_ms),
            "throughput": weights["throughput"] * throughput_score(
                r.benchmark_mbps, best_mbps),
            "geo": weights["geo"] * geo_score(r, geo_cache),
        }
        score = sum(comps.values())
        geo_label = ("verified" if geo_score(r, geo_cache) >= 1.0
                     else "heuristic" if geo_score(r, geo_cache) >= 0.5
                     else "unknown")
        explanation = " + ".join(
            f"{name}({value:.1f})" for name, value in comps.items() if value > 0
        ) or "no measurable components"
        scored.append({
            "route": r.id,
            "provider": r.provider,
            "country": r.country,
            "score": round(score, 2),
            "components": {k: round(v, 2) for k, v in comps.items()},
            "healthy": r.healthy,
            "latency_ms": r.latency_ms,
            "throughput_mbps": r.benchmark_mbps,
            "geo": geo_label,
            "explanation": explanation,
        })

    # deterministic order: score desc, then stable route id asc
    scored.sort(key=lambda s: (-s["score"], s["route"]))
    for i, s in enumerate(scored, 1):
        s["rank"] = i

    return {
        "best": scored[0]["route"] if scored else None,
        "count": len(scored),
        "candidates": scored,
        "excluded": excluded,
        "formula": {
            "weights": weights,
            "freshness_window_s": FRESHNESS_WINDOW_S,
            "probe_fresh_s": PROBE_FRESH_S,
            "speed_aware": speed_aware,
        },
    }
