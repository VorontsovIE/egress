"""Metrics DB / audit store (spec section 39, 40).

SQLite. Stores the entities the spec requires, **without raw secrets**
(section 39: "Provider/client raw secrets в DB не копировать"):

  * client profile metadata (no raw secrets)
  * route metadata
  * health/latency samples
  * domain probe samples
  * benchmark samples
  * provider update events
  * per-client default selection events
  * one-shot jobs/sessions with client_id
  * GEO verification cache
  * schema version

All writes are synchronous (sqlite3). The DB is small and append-mostly; a
single connection guarded by a re-entrant lock is sufficient for the control
plane's write rate.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# secret-looking keys that must never be persisted verbatim (defence in depth
# on top of adapters.common.redact).
_SECRET_KEY = re.compile(
    r"(private[-_]?key|preshared[-_]?key|uuid|password|secret|token|api[-_]?key|"
    r"client[-_]?id)", re.IGNORECASE
)


def _scrub(obj: Any) -> Any:
    """Recursively drop/replace secret-looking values before persisting."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY.search(k):
                out[k] = "***"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    environment TEXT,
    description TEXT,
    modes TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    provider TEXT,
    display_name TEXT,
    protocol TEXT,
    country TEXT,
    city TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS health_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    route_id TEXT,
    client_id TEXT,
    healthy INTEGER,
    latency_ms INTEGER,
    source TEXT
);
CREATE TABLE IF NOT EXISTS probe_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    route_id TEXT,
    client_id TEXT,
    domain TEXT,
    reachable INTEGER,
    status INTEGER,
    latency_ms INTEGER,
    error_class TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    route_id TEXT,
    client_id TEXT,
    mbps REAL,
    bytes INTEGER,
    ok INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS provider_update_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    provider TEXT,
    client_id TEXT,
    ok INTEGER,
    nodes_before INTEGER,
    nodes_after INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS default_selection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    client_id TEXT,
    from_route TEXT,
    to_route TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS oneshot_sessions (
    id TEXT PRIMARY KEY,
    client_id TEXT,
    route TEXT,
    mode TEXT,
    slot INTEGER,
    started_at REAL,
    ended_at REAL,
    end_reason TEXT
);
CREATE TABLE IF NOT EXISTS geo_cache (
    key TEXT PRIMARY KEY,
    route_id TEXT,
    exit_ip TEXT,
    exit_country TEXT,
    asn INTEGER,
    verified_at REAL
);
CREATE INDEX IF NOT EXISTS idx_health_route ON health_samples(route_id, ts);
CREATE INDEX IF NOT EXISTS idx_probe_route ON probe_samples(route_id, ts);
CREATE INDEX IF NOT EXISTS idx_bench_route ON benchmark_samples(route_id, ts);
CREATE INDEX IF NOT EXISTS idx_default_client ON default_selection_events(client_id, ts);
CREATE INDEX IF NOT EXISTS idx_oneshot_client ON oneshot_sessions(client_id, started_at);
"""


class MetricsDB:
    """Thin synchronous wrapper over the metrics/audit SQLite DB."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- generic helpers --------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ---- clients ----------------------------------------------------------
    def upsert_client(self, client_id: str, environment: str = "",
                      description: str = "", modes: tuple = ()) -> None:
        self._exec(
            """INSERT INTO clients(client_id, environment, description, modes, updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(client_id) DO UPDATE SET
                 environment=excluded.environment,
                 description=excluded.description,
                 modes=excluded.modes,
                 updated_at=excluded.updated_at""",
            (client_id, environment, description, json.dumps(list(modes)), time.time()),
        )

    # ---- routes -----------------------------------------------------------
    def upsert_route(self, r: dict[str, Any]) -> None:
        self._exec(
            """INSERT INTO routes(route_id, provider, display_name, protocol, country, city, updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(route_id) DO UPDATE SET
                 provider=excluded.provider,
                 display_name=excluded.display_name,
                 protocol=excluded.protocol,
                 country=excluded.country,
                 city=excluded.city,
                 updated_at=excluded.updated_at""",
            (r.get("id"), r.get("provider"), r.get("display_name"),
             r.get("protocol"), r.get("country"), r.get("city"), time.time()),
        )

    def sync_routes(self, routes: list[dict[str, Any]]) -> None:
        for r in routes:
            self.upsert_route(r)

    # ---- samples ----------------------------------------------------------
    def add_health(self, route_id: str, client_id: str, healthy: bool,
                   latency_ms: int | None, source: str = "delay") -> None:
        self._exec(
            "INSERT INTO health_samples(ts, route_id, client_id, healthy, latency_ms, source)"
            " VALUES(?,?,?,?,?,?)",
            (time.time(), route_id, client_id, 1 if healthy else 0, latency_ms, source),
        )

    def add_probe(self, route_id: str, client_id: str, domain: str,
                  reachable: bool, status: int | None, latency_ms: int | None,
                  error_class: str | None, detail: dict | None = None) -> None:
        self._exec(
            "INSERT INTO probe_samples(ts, route_id, client_id, domain, reachable,"
            " status, latency_ms, error_class, detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), route_id, client_id, domain, 1 if reachable else 0,
             status, latency_ms, error_class,
             json.dumps(_scrub(detail or {}))),
        )

    def add_benchmark(self, route_id: str, client_id: str, mbps: float | None,
                      nbytes: int, ok: bool, detail: dict | None = None) -> None:
        self._exec(
            "INSERT INTO benchmark_samples(ts, route_id, client_id, mbps, bytes, ok, detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (time.time(), route_id, client_id, mbps, nbytes, 1 if ok else 0,
             json.dumps(_scrub(detail or {}))),
        )

    def add_provider_update(self, provider: str, client_id: str, ok: bool,
                            nodes_before: int | None, nodes_after: int | None,
                            detail: dict | None = None) -> None:
        self._exec(
            "INSERT INTO provider_update_events(ts, provider, client_id, ok,"
            " nodes_before, nodes_after, detail) VALUES(?,?,?,?,?,?,?)",
            (time.time(), provider, client_id, 1 if ok else 0,
             nodes_before, nodes_after, json.dumps(_scrub(detail or {}))),
        )

    def add_default_selection(self, client_id: str, from_route: str | None,
                              to_route: str, reason: str) -> None:
        self._exec(
            "INSERT INTO default_selection_events(ts, client_id, from_route, to_route, reason)"
            " VALUES(?,?,?,?,?)",
            (time.time(), client_id, from_route, to_route, reason),
        )

    # ---- one-shot sessions ------------------------------------------------
    def open_session(self, session_id: str, client_id: str, route: str,
                     mode: str, slot: int) -> None:
        self._exec(
            "INSERT INTO oneshot_sessions(id, client_id, route, mode, slot, started_at)"
            " VALUES(?,?,?,?,?,?)",
            (session_id, client_id, route, mode, slot, time.time()),
        )

    def close_session(self, session_id: str, end_reason: str) -> None:
        self._exec(
            "UPDATE oneshot_sessions SET ended_at=?, end_reason=? WHERE id=?",
            (time.time(), end_reason, session_id),
        )

    def active_sessions(self, client_id: str | None = None) -> list[dict[str, Any]]:
        if client_id is None:
            return self._query(
                "SELECT * FROM oneshot_sessions WHERE ended_at IS NULL ORDER BY started_at DESC"
            )
        return self._query(
            "SELECT * FROM oneshot_sessions WHERE ended_at IS NULL AND client_id=?"
            " ORDER BY started_at DESC",
            (client_id,),
        )

    # ---- geo cache --------------------------------------------------------
    def set_geo(self, key: str, route_id: str, exit_ip: str | None,
                exit_country: str | None, asn: int | None) -> None:
        self._exec(
            "INSERT INTO geo_cache(key, route_id, exit_ip, exit_country, asn, verified_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET route_id=excluded.route_id,"
            " exit_ip=excluded.exit_ip, exit_country=excluded.exit_country,"
            " asn=excluded.asn, verified_at=excluded.verified_at",
            (key, route_id, exit_ip, exit_country, asn, time.time()),
        )

    def get_geo(self, key: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM geo_cache WHERE key=?", (key,))
        return rows[0] if rows else None

    # ---- audit tail (for /v1/doctor & debugging) --------------------------
    def audit_tail(self, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        out += [dict(t="default_selection", **r) for r in self._query(
            "SELECT ts, client_id, from_route, to_route, reason FROM default_selection_events"
            " ORDER BY ts DESC LIMIT ?", (limit,))]
        out += [dict(t="provider_update", **r) for r in self._query(
            "SELECT ts, provider, client_id, ok, nodes_before, nodes_after FROM provider_update_events"
            " ORDER BY ts DESC LIMIT ?", (limit,))]
        out += [dict(t="oneshot", **r) for r in self._query(
            "SELECT started_at AS ts, client_id, route, mode, slot, end_reason FROM oneshot_sessions"
            " ORDER BY started_at DESC LIMIT ?", (limit,))]
        out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return out[:limit]
