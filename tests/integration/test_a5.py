"""A5 — probe / benchmark / ranking tests (spec sections 25, 26).

Covers the spec's A5 "Обязательные checks" list:

  1. direct baseline (benchmark shortlist appends ``direct``)
  2. parallel probes with cap (probe engine bounded by slots/parallel)
  3. DNS/TCP/TLS/HTTP classification (classifier unit tests)
  4. bounded bytes/time (benchmark honours the ``bytes`` cap)
  5. ``best --domain`` uses an actual domain probe, not GEO heuristics

Plus the transparent ranking contract: filters, health gate, freshness,
latency, speed-aware throughput, GEO confidence tier, deterministic
tie-break, and the JSON explanation.

Run:  python -m pytest tests/integration/test_a5.py -v
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

from egressd.catalog import RouteCatalog  # noqa: E402
from egressd.metrics import MetricsDB  # noqa: E402
from egressd.probe import (  # noqa: E402
    CONNECTION_RESET,
    DNS_FAILURE,
    HTTP_STATUS_DENIED,
    HTTP_TIMEOUT,
    OK,
    REDIRECT,
    TLS_FAILURE,
    TCP_REFUSED,
    TCP_TIMEOUT,
    ProbeOutcome,
    classify_probe,
    socks_probe,
)


def test_tls_handshake_timeout_is_route_failure():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def stall_tls():
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(1)
            conn.recv(3)
            conn.sendall(b"\x05\x00")
            conn.recv(512)
            conn.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            time.sleep(0.2)
        listener.close()

    worker = threading.Thread(target=stall_tls)
    worker.start()
    try:
        result = socks_probe("127.0.0.1", listener.getsockname()[1], "u", "p",
                             "example.com", timeout=0.05, check=("tcp", "tls"))
    finally:
        worker.join(timeout=2)
        listener.close()
    assert result.tcp_ok is True
    assert result.tls_ok is False
    assert result.error == TLS_FAILURE
from egressd.ranking import (  # noqa: E402
    WEIGHTS_DEFAULT,
    WEIGHTS_SPEED_AWARE,
    freshness_score,
    geo_score,
    health_score,
    latency_score,
    rank_routes,
    throughput_score,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _write_blanc(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "FI-Helsinki-2", "type": "socks5", "server": "1.1.1.2", "port": 1080},
        {"name": "NL-Ams-1", "type": "socks5", "server": "3.3.3.1", "port": 1080},
    ]}, sort_keys=False))


def _write_xeovo(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "US-New-York-1", "type": "socks5", "server": "2.2.2.1", "port": 1080},
    ]}, sort_keys=False))


@pytest.fixture()
def egress(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    for cid, env in (("host-main", "host"), ("openscience-main", "gvisor-container")):
        (clients / f"{cid}.token").write_text(f"tok-{cid}\n")
        (clients / f"{cid}.profile.json").write_text(json.dumps(
            {"client_id": cid, "environment": env, "modes": ["proxy"]}))
        (clients / f"{cid}.proxy-creds").write_text(f"{cid}-user:{cid}-pass\n")

    provs = tmp_path / "providers"
    provs.mkdir()
    _write_blanc(provs / "blanc.yaml")
    _write_xeovo(provs / "xeovo.yaml")

    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(provs))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_METRICS_DB", str(tmp_path / "state" / "metrics.sqlite"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    monkeypatch.setenv("EGRESSD_ONESHOT_SLOTS", "4")

    from egressd.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    return {
        "app": app,
        "client": TestClient(app),
        "provs": provs,
        "tmp": tmp_path,
        "host": {"Authorization": "Bearer tok-host-main"},
        "os": {"Authorization": "Bearer tok-openscience-main"},
    }


# ---------------------------------------------------------------------------
# ranking unit tests (pure functions, no network)
# ---------------------------------------------------------------------------
class TestRankingUnit:
    def _route(self, rid="p:n1", healthy=True, latency=100, country="FI",
               mbps=None, exit_country=None, last_test_at=None):
        from egressd.catalog import RouteInfo
        r = RouteInfo(id=rid, provider="p", display_name=rid.split(":")[1],
                      country=country)
        r.healthy = healthy
        r.latency_ms = latency
        r.benchmark_mbps = mbps
        r.exit_country = exit_country
        r.last_test_at = last_test_at
        return r

    def test_weights_sum_to_100(self):
        assert abs(sum(WEIGHTS_DEFAULT.values()) - 100.0) < 1e-9
        assert abs(sum(WEIGHTS_SPEED_AWARE.values()) - 100.0) < 1e-9
        assert WEIGHTS_DEFAULT["throughput"] == 0.0

    def test_health_score(self):
        now = time.time()
        assert health_score(True, now, now) == 1.0
        assert health_score(None, None, now) == 0.5
        assert health_score(False, now, now) == 0.0

    def test_freshness_score(self):
        now = time.time()
        assert freshness_score(now, now) == 1.0
        assert freshness_score(now - 1800, now) == 0.5
        assert freshness_score(now - 7200, now) == 0.0
        assert freshness_score(None, now) == 0.0
        utc_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1800))
        assert freshness_score(utc_stamp, now) == pytest.approx(0.5, abs=0.001)
        assert freshness_score("invalid", now) == 0.0

    def test_latency_score(self):
        assert latency_score(50, 50) == 1.0
        assert latency_score(150, 50) == pytest.approx(50 / 150, abs=1e-9)
        assert latency_score(None, 50) == 0.0
        assert latency_score(100, None) == 0.0

    def test_throughput_score(self):
        assert throughput_score(80.0, 80.0) == 1.0
        assert throughput_score(40.0, 80.0) == 0.5
        assert throughput_score(None, 80.0) == 0.0

    def test_geo_score_tiers(self):
        r_verified = self._route(exit_country="FI")
        r_heuristic = self._route(exit_country=None)
        r_unknown = self._route(country=None, exit_country=None)
        assert geo_score(r_verified) == 1.0
        assert geo_score(r_heuristic) == 0.5
        assert geo_score(r_unknown) == 0.0
        # a geo_cache entry counts as verified
        assert geo_score(r_heuristic, geo_cache={"p:n1": {"exit_country": "FI"}}) == 1.0

    def test_ranking_orders_by_score(self):
        now = time.time()
        fast = self._route("p:fast", latency=40, last_test_at=now)
        slow = self._route("p:slow", latency=400, last_test_at=now)
        untested = self._route("p:untested", latency=None, last_test_at=None)
        dead = self._route("p:dead", healthy=False, latency=10, last_test_at=now)
        res = rank_routes([slow, untested, fast, dead], now=now)
        assert res["best"] == "p:fast"
        # known-healthy beats never-tested even with a worse latency
        assert [c["route"] for c in res["candidates"]] == ["p:fast", "p:slow", "p:untested"]
        assert any(e["route"] == "p:dead" and e["reason"] == "unhealthy"
                   for e in res["excluded"])

    def test_freshness_beats_latency(self):
        now = time.time()
        # A: fast but stale; B: slower but fresh. B must win on the
        # freshness+latency combination (freshness weight 20 > the latency
        # gap here).
        a = self._route("p:a", latency=40, last_test_at=now - 7200)
        b = self._route("p:b", latency=60, last_test_at=now)
        res = rank_routes([a, b], now=now)
        assert res["best"] == "p:b"

    def test_speed_aware_prefers_throughput(self):
        now = time.time()
        a = self._route("p:a", latency=40, mbps=10.0, last_test_at=now)
        b = self._route("p:b", latency=45, mbps=120.0, last_test_at=now)
        res_default = rank_routes([a, b], now=now, speed_aware=False)
        res_speed = rank_routes([a, b], now=now, speed_aware=True)
        # without speed-aware the latency gap decides; with it, throughput
        # (15 pts) outweighs the 5ms latency difference.
        assert res_default["best"] == "p:a"
        assert res_speed["best"] == "p:b"

    def test_max_latency_excludes(self):
        now = time.time()
        a = self._route("p:a", latency=40, last_test_at=now)
        b = self._route("p:b", latency=400, last_test_at=now)
        res = rank_routes([a, b], now=now, max_latency_ms=150)
        assert res["best"] == "p:a"
        assert any(e["route"] == "p:b" for e in res["excluded"])

    def test_domain_gate_requires_fresh_probe(self):
        now = time.time()
        a = self._route("p:a", latency=40, last_test_at=now)
        b = self._route("p:b", latency=45, last_test_at=now)
        # no probes at all -> nobody is a candidate
        res = rank_routes([a, b], now=now, domain="example.com", fresh_probes={})
        assert res["best"] is None
        assert len(res["excluded"]) == 2
        # a fresh successful probe for a only -> a is the only candidate
        res = rank_routes([a, b], now=now, domain="example.com",
                          fresh_probes={"p:a": {"reachable": True}})
        assert res["best"] == "p:a"
        # a failed probe excludes the route too
        res = rank_routes([a, b], now=now, domain="example.com",
                          fresh_probes={"p:a": {"reachable": False, "error": "tls_failure"}})
        assert res["best"] is None

    def test_deterministic_tie_break(self):
        now = time.time()
        a = self._route("p:aa", latency=50, last_test_at=now)
        b = self._route("p:bb", latency=50, last_test_at=now)
        r1 = rank_routes([a, b], now=now)
        r2 = rank_routes([b, a], now=now)
        assert [c["route"] for c in r1["candidates"]] == \
               [c["route"] for c in r2["candidates"]] == ["p:aa", "p:bb"]

    def test_explanation_and_formula_present(self):
        now = time.time()
        a = self._route("p:a", latency=50, last_test_at=now)
        res = rank_routes([a], now=now)
        c = res["candidates"][0]
        assert c["explanation"]
        assert set(c["components"]) == {"health", "freshness", "latency",
                                        "throughput", "geo"}
        assert res["formula"]["weights"] == WEIGHTS_DEFAULT
        assert abs(sum(c["components"].values()) - c["score"]) < 0.1


# ---------------------------------------------------------------------------
# classifier (spec 24) — the A5 "DNS/TCP/TLS/HTTP classification" check
# ---------------------------------------------------------------------------
class TestClassifier:
    def _o(self, **kw) -> ProbeOutcome:
        base = dict(route_id="p:n", domain="example.com")
        base.update(kw)
        return ProbeOutcome(**base)

    def test_ok(self):
        o = self._o(tcp_ok=True, tls_ok=True, http_ok=True, status=200)
        assert classify_probe(o) == OK

    def test_dns_failure(self):
        o = self._o(tcp_ok=False, dns_ok=False, socks_code=4)
        assert classify_probe(o) == DNS_FAILURE

    def test_tcp_timeout(self):
        o = self._o(tcp_ok=False, detail={"tcp_timeout": True})
        assert classify_probe(o) == TCP_TIMEOUT

    def test_tcp_refused(self):
        o = self._o(tcp_ok=False, detail={"tcp_refused": True})
        assert classify_probe(o) == TCP_REFUSED

    def test_tls_failure(self):
        o = self._o(tcp_ok=True, tls_ok=False)
        assert classify_probe(o) == TLS_FAILURE

    def test_http_denied(self):
        o = self._o(tcp_ok=True, tls_ok=True, http_ok=False, status=403)
        assert classify_probe(o) == HTTP_STATUS_DENIED

    def test_redirect(self):
        o = self._o(tcp_ok=True, tls_ok=True, http_ok=True, status=301)
        assert classify_probe(o) == REDIRECT

    def test_connection_reset(self):
        o = self._o(tcp_ok=True, tls_ok=True, detail={"http_reset": True})
        assert classify_probe(o) == CONNECTION_RESET

    def test_http_timeout(self):
        o = self._o(tcp_ok=True, tls_ok=True, detail={"http_timeout": True})
        assert classify_probe(o) == HTTP_TIMEOUT


# ---------------------------------------------------------------------------
# API: /v1/best (spec 26)
# ---------------------------------------------------------------------------
class TestBestApi:
    def test_best_returns_transparent_ranking(self, egress):
        c, host = egress["client"], egress["host"]
        r = c.get("/v1/best", headers=host)
        assert r.status_code == 200
        data = r.json()
        assert data["client_id"] == "host-main"
        assert data["count"] == 5  # 3 blanc + 1 xeovo + direct (all untested)
        assert data["best"] is not None
        # candidates sorted by score desc, ranks 1..N
        scores = [x["score"] for x in data["candidates"]]
        assert scores == sorted(scores, reverse=True)
        assert [x["rank"] for x in data["candidates"]] == list(range(1, data["count"] + 1))
        # every candidate carries the explanation + components
        for x in data["candidates"]:
            assert x["explanation"]
            assert set(x["components"]) == {"health", "freshness", "latency",
                                            "throughput", "geo"}
        # formula is returned
        assert data["formula"]["weights"] == WEIGHTS_DEFAULT

    def test_best_country_filter(self, egress):
        c, host = egress["client"], egress["host"]
        r = c.get("/v1/best", headers=host, params={"country": "FI"})
        data = r.json()
        assert data["count"] == 2
        assert all(x["country"] == "FI" for x in data["candidates"])

    def test_best_provider_filter(self, egress):
        c, host = egress["client"], egress["host"]
        r = c.get("/v1/best", headers=host, params={"provider": "xeovo"})
        data = r.json()
        assert data["count"] == 1
        assert data["best"] == "xeovo:US-New-York-1"

    def test_best_unhealthy_excluded(self, egress):
        c, host, app = egress["client"], egress["host"], egress["app"]
        # mark one route unhealthy in the catalog
        app.state.catalog.get("blanc:FI-Helsinki-1").healthy = False
        r = c.get("/v1/best", headers=host)
        data = r.json()
        assert "blanc:FI-Helsinki-1" not in [x["route"] for x in data["candidates"]]
        assert any(e["route"] == "blanc:FI-Helsinki-1" and e["reason"] == "unhealthy"
                   for e in data["excluded"])

    def test_best_max_latency(self, egress):
        c, host, app = egress["client"], egress["host"], egress["app"]
        now = time.time()
        for rid, ms in (("blanc:FI-Helsinki-1", 40), ("blanc:FI-Helsinki-2", 400),
                        ("blanc:NL-Ams-1", 60), ("xeovo:US-New-York-1", 50)):
            r = app.state.catalog.get(rid)
            r.healthy, r.latency_ms, r.last_test_at = True, ms, now
        r = c.get("/v1/best", headers=host, params={"max_latency": 150})
        data = r.json()
        routes = [x["route"] for x in data["candidates"]]
        assert "blanc:FI-Helsinki-2" not in routes
        assert data["best"] == "blanc:FI-Helsinki-1"

    def test_best_domain_uses_stored_probe(self, egress):
        """best --domain must use an actual probe result, not GEO heuristics."""
        c, host, app = egress["client"], egress["host"], egress["app"]
        now = time.time()
        # fresh probe samples: FI-Helsinki-1 reachable, NL-Ams-1 blocked (TLS)
        app.state.metrics.add_probe("blanc:FI-Helsinki-1", "host-main",
                                    "example.com", True, 200, 42, OK, {})
        app.state.metrics.add_probe("blanc:NL-Ams-1", "host-main",
                                    "example.com", False, None, 30,
                                    TLS_FAILURE, {})
        # all other routes get no probe -> they must be excluded even though
        # their GEO heuristic looks fine (country is set on every route).
        r = c.get("/v1/best", headers=host, params={"domain": "example.com"})
        data = r.json()
        assert data["best"] == "blanc:FI-Helsinki-1"
        assert [x["route"] for x in data["candidates"]] == ["blanc:FI-Helsinki-1"]
        # every exclusion is backed by an actual probe result: the stored
        # failed probe (NL-Ams-1) and on-demand probes for the rest (they
        # fail with proxy_failure because mihomo is offline in this test).
        reasons = {e["route"]: e["reason"] for e in data["excluded"]}
        assert "probe for example.com failed (tls_failure)" == \
            reasons["blanc:NL-Ams-1"]
        assert all("probe for example.com failed" in v for v in reasons.values())

    def test_best_domain_stored_probe_only(self, egress, monkeypatch):
        """With on-demand probing disabled, routes without a fresh stored
        sample are excluded with 'no fresh probe' (GEO heuristics alone can
        never qualify a route)."""
        c, host, app = egress["client"], egress["host"], egress["app"]
        app.state.metrics.add_probe("blanc:FI-Helsinki-1", "host-main",
                                    "example.com", True, 200, 42, OK, {})
        monkeypatch.setattr(app.state.engine, "probe_routes",
                            lambda *a, **k: [])
        data = c.get("/v1/best", headers=host,
                     params={"domain": "example.com"}).json()
        assert data["best"] == "blanc:FI-Helsinki-1"
        reasons = {e["route"]: e["reason"] for e in data["excluded"]}
        assert reasons["xeovo:US-New-York-1"] == "no fresh probe for example.com"
        assert reasons["blanc:NL-Ams-1"] == "no fresh probe for example.com"

    def test_best_domain_on_demand_probe(self, egress, monkeypatch):
        """Routes without a fresh stored sample are probed on demand."""
        c, host, app = egress["client"], egress["host"], egress["app"]
        calls = []

        def fake_probe_routes(route_ids, domain, parallel=8, **kw):
            calls.append((list(route_ids), domain, parallel))
            from egressd.probe import ProbeOutcome
            out = []
            for rid in route_ids:
                o = ProbeOutcome(route_id=rid, domain=domain)
                if rid == "blanc:FI-Helsinki-2":
                    o.tcp_ok, o.tls_ok, o.http_ok, o.status = True, True, True, 200
                    o.error, o.latency_ms = OK, 55
                else:
                    o.tcp_ok = False
                    o.detail["tcp_refused"] = True
                    o.error, o.latency_ms = TCP_REFUSED, 12
                out.append(o)
            return out

        monkeypatch.setattr(app.state.engine, "probe_routes", fake_probe_routes)
        r = c.get("/v1/best", headers=host, params={"domain": "example.com"})
        data = r.json()
        assert data["best"] == "blanc:FI-Helsinki-2"
        # the on-demand probe was bounded (<= test_max_routes) and parallel
        assert calls and calls[0][1] == "example.com"
        assert calls[0][2] <= 8

    def test_best_speed_aware_uses_benchmarks(self, egress, monkeypatch):
        c, host, app = egress["client"], egress["host"], egress["app"]
        now = time.time()
        for rid, ms in (("blanc:FI-Helsinki-1", 40), ("blanc:FI-Helsinki-2", 45)):
            r = app.state.catalog.get(rid)
            r.healthy, r.latency_ms, r.last_test_at = True, ms, now
        # benchmark samples: Helsinki-2 is much faster
        app.state.metrics.add_benchmark("blanc:FI-Helsinki-1", "host-main", 12.0,
                                        8 * 1024 * 1024, True, {})
        app.state.metrics.add_benchmark("blanc:FI-Helsinki-2", "host-main", 140.0,
                                        8 * 1024 * 1024, True, {})
        # rebuild the catalog so the benchmark enrichment picks the samples up
        from egressd.main import rebuild_catalog
        rebuild_catalog(app)
        r = c.get("/v1/best", headers=host,
                  params={"provider": "blanc", "speed_aware": "true"})
        data = r.json()
        assert data["best"] == "blanc:FI-Helsinki-2"
        assert data["formula"]["speed_aware"] is True
        # without speed-aware the 5ms latency gap wins for Helsinki-1
        r = c.get("/v1/best", headers=host, params={"provider": "blanc"})
        assert r.json()["best"] == "blanc:FI-Helsinki-1"

    def test_best_requires_auth(self, egress):
        r = egress["client"].get("/v1/best")
        assert r.status_code == 401

    def test_list_healthy_filter(self, egress):
        c, host, app = egress["client"], egress["host"], egress["app"]
        now = time.time()
        for rid, ok in (("blanc:FI-Helsinki-1", True), ("blanc:FI-Helsinki-2", False)):
            r = app.state.catalog.get(rid)
            r.healthy, r.last_test_at = ok, now
        data = c.get("/v1/routes", headers=host, params={"healthy": "true"}).json()
        ids = [x["id"] for x in data["routes"]]
        assert "blanc:FI-Helsinki-1" in ids
        assert "blanc:FI-Helsinki-2" not in ids


# ---------------------------------------------------------------------------
# benchmark (spec 25): bounded, shortlist pipeline, direct baseline
# ---------------------------------------------------------------------------
class TestBenchmarkApi:
    def test_benchmark_single_route_bounded(self, egress, monkeypatch):
        c, host = egress["client"], egress["host"]
        seen = {}

        def fake_download(host_, port, user, password, nbytes):
            seen["nbytes"] = nbytes
            return 81.2, nbytes, None

        monkeypatch.setattr("egressd.main._download_through_socks", fake_download)
        r = c.post("/v1/benchmark", headers=host,
                   json={"route": "blanc:FI-Helsinki-1", "bytes": "1M"})
        data = r.json()
        assert data["count"] == 1
        res = data["results"][0]
        assert res["ok"] is True
        assert res["mbps"] == 81.2
        # bounded by the requested bytes (spec 25: bounded by bytes/time)
        assert seen["nbytes"] == 1024 * 1024
        assert res["bytes"] == 1024 * 1024

    def test_benchmark_shortlist_includes_direct_baseline(self, egress, monkeypatch):
        c, host, app = egress["client"], egress["host"], egress["app"]
        now = time.time()
        for rid, ms in (("blanc:FI-Helsinki-1", 40), ("blanc:FI-Helsinki-2", 50),
                        ("blanc:NL-Ams-1", 60), ("xeovo:US-New-York-1", 70)):
            r = app.state.catalog.get(rid)
            r.healthy, r.latency_ms, r.last_test_at = True, ms, now

        def fake_download(host_, port, user, password, nbytes):
            return 50.0, nbytes, None

        monkeypatch.setattr("egressd.main._download_through_socks", fake_download)
        r = c.post("/v1/benchmark", headers=host,
                   json={"top_latency": 2, "bytes": "1M"})
        data = r.json()
        routes = [x["route"] for x in data["results"]]
        # shortlist = 2 fastest + the direct baseline (spec 25)
        assert "direct" in routes
        assert "blanc:FI-Helsinki-1" in routes and "blanc:FI-Helsinki-2" in routes
        assert len(routes) == 3

    def test_benchmark_unknown_route(self, egress):
        r = egress["client"].post("/v1/benchmark", headers=egress["host"],
                                  json={"route": "nope:no"})
        assert r.json()["results"][0]["error"] == "unknown_route"

    def test_benchmarks_history(self, egress, monkeypatch):
        c, host = egress["client"], egress["host"]
        monkeypatch.setattr("egressd.main._download_through_socks",
                            lambda h, p, u, pw, n: (66.0, n, None))
        c.post("/v1/benchmark", headers=host, json={"route": "blanc:FI-Helsinki-1"})
        data = c.get("/v1/benchmarks", headers=host).json()
        assert len(data["benchmarks"]) == 1
        assert data["benchmarks"][0]["mbps"] == 66.0
        # per-client: the other client sees nothing
        data = c.get("/v1/benchmarks", headers=egress["os"]).json()
        assert data["benchmarks"] == []


# ---------------------------------------------------------------------------
# probe engine: parallel probes with cap (spec 24/25)
# ---------------------------------------------------------------------------
class TestProbeParallelCap:
    def test_probe_routes_respects_slot_cap(self, egress):
        app = egress["app"]
        from egressd.probe import SlotAllocator, load_slots
        alloc = SlotAllocator(load_slots(2, None))  # only 2 slots
        engine = app.state.engine
        engine.allocator = alloc
        calls = []

        def fake_probe_route(route_id, domain, **kw):
            calls.append(route_id)
            from egressd.probe import ProbeOutcome
            o = ProbeOutcome(route_id=route_id, domain=domain)
            o.tcp_ok, o.tls_ok, o.http_ok, o.status = True, True, True, 200
            o.error, o.latency_ms = OK, 10
            return o

        monkey = pytest.MonkeyPatch()
        monkey.setattr(engine, "probe_route", fake_probe_route)
        try:
            outcomes = engine.probe_routes(
                ["blanc:FI-Helsinki-1", "blanc:FI-Helsinki-2", "blanc:NL-Ams-1",
                 "xeovo:US-New-York-1"], "example.com", parallel=8)
        finally:
            monkey.undo()
        assert len(outcomes) == 4
        # all four completed even though only 2 slots exist (allocator
        # blocks/retries instead of dropping)
        assert len(calls) == 4


def test_catalog_refresh_preserves_runtime_state_only_for_unchanged_route():
    old = RouteCatalog().build(
        {"personal": ["FI-node"]},
        identities={"personal": {"FI-node": "fingerprint-a"}},
    )
    route = old.get("personal:FI-node")
    route.healthy, route.latency_ms = True, 42
    route.exit_ip, route.exit_country, route.asn = "203.0.113.1", "FI", 123
    route.last_test_at = "2026-10-19T10:00:00Z"
    route.benchmark_mbps, route.benchmark_at = 88.0, "2026-10-19T10:01:00Z"

    unchanged = RouteCatalog().build(
        {"personal": ["FI-node"]},
        identities={"personal": {"FI-node": "fingerprint-a"}},
    )
    unchanged.preserve_runtime_from(old)
    assert unchanged.get("personal:FI-node").to_dict() == route.to_dict()

    changed = RouteCatalog().build(
        {"personal": ["FI-node"]},
        identities={"personal": {"FI-node": "fingerprint-b"}},
    )
    changed.preserve_runtime_from(old)
    reset = changed.get("personal:FI-node")
    assert reset.healthy is None
    assert reset.latency_ms is None
    assert reset.exit_ip is None and reset.exit_country is None and reset.asn is None
    assert reset.last_test_at is None
    assert reset.benchmark_mbps is None and reset.benchmark_at is None


def test_provider_fingerprint_tracks_config_not_yaml_key_order(tmp_path):
    from egressd.main import _read_provider_file

    provider = tmp_path / "provider.yaml"
    provider.write_text(
        "proxies:\n  - name: FI-node\n    type: hysteria2\n"
        "    server: 203.0.113.2\n    port: 443\n    password: first\n"
    )
    _, _, _, first = _read_provider_file(provider)
    provider.write_text(
        "proxies:\n  - password: first\n    port: 443\n"
        "    server: 203.0.113.2\n    type: hysteria2\n    name: FI-node\n"
    )
    _, _, _, reordered = _read_provider_file(provider)
    assert first == reordered

    provider.write_text(
        "proxies:\n  - name: FI-node\n    type: hysteria2\n"
        "    server: 203.0.113.2\n    port: 443\n    password: second\n"
    )
    _, _, _, changed = _read_provider_file(provider)
    assert first["FI-node"] != changed["FI-node"]


def test_metrics_drop_stale_samples_when_route_definition_changes(tmp_path):
    db = MetricsDB(tmp_path / "metrics.sqlite3")
    try:
        identity = {"personal:FI-node": "fingerprint-a"}
        db.sync_route_identities(identity)
        db.add_health("personal:FI-node", "host-main", True, 42)
        db.add_probe("personal:FI-node", "host-main", "example.com",
                     True, 200, 80, None, {})
        db.add_benchmark("personal:FI-node", "host-main", 88.0, 1024, True, {})
        db.set_geo("test-key", "personal:FI-node", "203.0.113.1", "FI", 123)

        db.sync_route_identities(identity)
        assert len(db.latest_health_samples()) == 1
        assert len(db._query("SELECT route_id FROM probe_samples")) == 1
        assert len(db._query("SELECT route_id FROM benchmark_samples")) == 1
        assert db.get_geo("test-key") is not None

        db.sync_route_identities({"personal:FI-node": "fingerprint-b"})
        assert db.latest_health_samples() == []
        assert db._query("SELECT route_id FROM probe_samples") == []
        assert db._query("SELECT route_id FROM benchmark_samples") == []
        assert db.get_geo("test-key") is None
    finally:
        db.close()
