"""A3 — egressd full public API tests (offline, no mihomo binary / no network).

Covers the spec's A3 "Обязательные tests" list, exercised against a real
FastAPI ``TestClient`` (the control plane works offline; only the data plane
and mihomo reads degrade gracefully):

  1. client impersonation denied
  2. one client cannot change another's default
  3. concurrent one-shot sessions (distinct slots, no shared selector)
  4. route deletion during an active one-shot job -> deterministic termination
  5. provider update races (concurrent updates stay consistent, no corruption)
  6. secret redaction (provider private keys never reach API/DB/audit)
  7. per-client fallback to direct (only the affected client falls back)
  8. public API has no remote exec / raw Mihomo passthrough
  9. domain probe error classification (all spec-24 classes)

Plus supporting coverage: stable route IDs, per-client default persistence,
mode unsupported (no silent fallback), proxy default-capability (no provider
secrets), and the metrics DB schema.

Run:  python -m pytest tests/integration/test_a3.py -v
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

from egressd.probe import (  # noqa: E402
    ALL_ERROR_CLASSES,
    CONNECTION_RESET,
    DNS_FAILURE,
    HTTP_STATUS_DENIED,
    HTTP_TIMEOUT,
    OK,
    REDIRECT,
    SLOT_EXHAUSTED,
    TLS_FAILURE,
    TCP_REFUSED,
    TCP_TIMEOUT,
    ProbeOutcome,
    classify_probe,
    socks_probe,
)

# A secret that must NEVER appear in any API response, DB row, or audit line.
WG_SECRET = "TOPSECRET-WG-KEY-DO-NOT-LEAK-0001"


# ---------------------------------------------------------------------------
# fixture: a fresh egressd app with two clients + two providers
# ---------------------------------------------------------------------------
def _write_blanc(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "FI-Helsinki-2", "type": "socks5", "server": "1.1.1.2", "port": 1080},
        {"name": "FI-WG-1", "type": "wireguard", "server": "wg.example.com",
         "port": 51820, "ip": "10.66.0.2", "private-key": WG_SECRET,
         "public-key": "pub-key-not-a-secret", "allowed-ips": ["0.0.0.0/0"]},
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
        (clients / f"{cid}.proxy-creds").write_text(f"{cid.split('-')[0]}-user:{cid.split('-')[0]}-pass\n")

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


def _dump_all_tables(app) -> str:
    """Stringify every row in every metrics table (for secret-leak checks)."""
    db = sqlite3.connect(str(app.state.metrics.path))
    out = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        for row in db.execute(f"SELECT * FROM {t}"):
            out.append(str(row))
    db.close()
    return "\n".join(out)


# ===========================================================================
# 1. client impersonation denied
# ===========================================================================
def test_impersonation_denied(egress):
    c, host, os_ = egress["client"], egress["host"], egress["os"]

    # A client cannot name another client_id to change someone else's default.
    # The identity is bound to the token; a client_id field in the body is
    # ignored. host-main sets its own default even though the body claims to
    # target openscience-main.
    r = c.put("/v1/default", headers=host,
              json={"route": "blanc:FI-Helsinki-1", "client_id": "openscience-main"})
    assert r.status_code == 200
    assert r.json()["client_id"] == "host-main"  # applied to the token's client

    # openscience-main's default is untouched by host-main's impersonation attempt.
    r = c.get("/v1/default", headers=os_)
    assert r.json()["client_id"] == "openscience-main"
    assert r.json()["route"] == "direct"

    # No endpoint exposes another client's state by naming it.
    r = c.get("/v1/default", headers=os_)
    assert r.json()["route"] != "blanc:FI-Helsinki-1"


# ===========================================================================
# 2. one client cannot change another's default
# ===========================================================================
def test_cross_client_default_isolation(egress):
    c, host, os_ = egress["client"], egress["host"], egress["os"]

    c.put("/v1/default", headers=host, json={"route": "blanc:FI-Helsinki-1"})
    c.put("/v1/default", headers=os_, json={"route": "xeovo:US-New-York-1"})

    # each client sees only its own default
    assert c.get("/v1/default", headers=host).json()["route"] == "blanc:FI-Helsinki-1"
    assert c.get("/v1/default", headers=os_).json()["route"] == "xeovo:US-New-York-1"

    # os changing its default does not touch host's default
    c.put("/v1/default", headers=os_, json={"route": "blanc:FI-Helsinki-2"})
    assert c.get("/v1/default", headers=host).json()["route"] == "blanc:FI-Helsinki-1"
    assert c.get("/v1/default", headers=os_).json()["route"] == "blanc:FI-Helsinki-2"


# ===========================================================================
# 3. concurrent one-shot sessions
# ===========================================================================
def test_concurrent_oneshot_sessions(egress):
    c, host = egress["client"], egress["host"]
    routes = ["blanc:FI-Helsinki-1", "blanc:FI-Helsinki-2",
              "blanc:FI-WG-1", "xeovo:US-New-York-1"]

    def create(i):
        return c.post("/v1/oneshot/session", headers=host,
                      json={"route": routes[i]}).json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(create, range(4)))

    # all 4 created (4 slots available), each with a distinct slot
    assert all(r.get("id") for r in results)
    active = c.get("/v1/oneshot/active", headers=host).json()["sessions"]
    active_slots = {s["slot"] for s in active}
    assert len(active_slots) == 4  # no two sessions share a slot
    # each capability points at its own slot user (private data-plane path)
    users = {s["slot"]: r["capability"]["username"] for r, s in
             zip(results, active)}
    assert len(set(r["capability"]["username"] for r in results)) == 4

    # closing one frees exactly its slot
    sid = results[0]["id"]
    assert c.delete(f"/v1/oneshot/session/{sid}", headers=host).json()["closed"]
    active2 = c.get("/v1/oneshot/active", headers=host).json()["sessions"]
    assert len(active2) == 3
    assert len({s["slot"] for s in active2}) == 3


# ===========================================================================
# 4. route deletion during an active one-shot job
# ===========================================================================
def test_route_deletion_during_job(egress):
    c, host, provs = egress["client"], egress["host"], egress["provs"]

    # active session on blanc:FI-Helsinki-2
    sess = c.post("/v1/oneshot/session", headers=host,
                  json={"route": "blanc:FI-Helsinki-2"}).json()
    assert sess["id"]
    assert c.get("/v1/oneshot/active", headers=host).json()["count"] == 1

    # delete the route: rewrite the provider file without FI-Helsinki-2
    (provs / "blanc.yaml").write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "FI-WG-1", "type": "wireguard", "server": "wg.example.com",
         "port": 51820, "ip": "10.66.0.2", "private-key": WG_SECRET,
         "public-key": "pub", "allowed-ips": ["0.0.0.0/0"]},
    ]}, sort_keys=False))

    # reconcile re-reads the inventory and terminates the affected session
    r = c.post("/v1/reconcile", headers=host).json()
    assert r["terminated_sessions"] == 1

    # the session is gone and its slot is released
    assert c.get("/v1/oneshot/active", headers=host).json()["count"] == 0
    caps = c.get("/v1/capabilities", headers=host).json()
    assert caps["oneshot_free"] == caps["oneshot_slots"]  # slot freed

    # a NEW session for the deleted route is refused (route no longer exists)
    r = c.post("/v1/oneshot/session", headers=host,
               json={"route": "blanc:FI-Helsinki-2"})
    assert r.status_code == 400


# ===========================================================================
# 5. provider update races
# ===========================================================================
def test_provider_update_race(egress):
    c, host, provs, tmp = egress["client"], egress["host"], egress["provs"], egress["tmp"]
    app = egress["app"]

    # give blanc a local source so the fetch+adapter+atomic-write path runs
    src = tmp / "blanc_source.yaml"
    src.write_text(yaml.safe_dump({"proxies": [
        {"name": "FI - Helsinki 1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "FI - Helsinki 2", "type": "socks5", "server": "1.1.1.2", "port": 1080},
    ]}, sort_keys=False))
    os.environ["EGRESSD_PROVIDER_SOURCE_blanc"] = str(src)
    try:
        # fire two concurrent updates to the same provider
        def upd():
            return c.post("/v1/providers/blanc/update", headers=host, json={}).json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            res = list(ex.map(lambda _: upd(), range(2)))

        assert all(r["ok"] for r in res)
        # the provider file is still valid YAML with a consistent node set
        data = yaml.safe_load((provs / "blanc.yaml").read_text())
        names = [p["name"] for p in data["proxies"]]
        assert names == ["FI-Helsinki-1", "FI-Helsinki-2"]
        assert len(names) == len(set(names))  # no duplicates
        # catalog is consistent with the file
        routes = c.get("/v1/routes", headers=host,
                       params={"provider": "blanc"}).json()["routes"]
        assert {r["id"] for r in routes} == {"blanc:FI-Helsinki-1", "blanc:FI-Helsinki-2"}
    finally:
        del os.environ["EGRESSD_PROVIDER_SOURCE_blanc"]


# ===========================================================================
# 6. secret redaction
# ===========================================================================
def test_secret_redaction(egress):
    c, host = egress["client"], egress["host"]
    app = egress["app"]

    # the provider file on disk DOES contain the secret
    assert WG_SECRET in (egress["provs"] / "blanc.yaml").read_text()

    # but no API response leaks it
    blobs = []
    blobs.append(c.get("/v1/routes", headers=host).text)
    blobs.append(c.get("/v1/routes/blanc:FI-WG-1", headers=host).text)
    blobs.append(c.get("/v1/providers", headers=host).text)
    blobs.append(c.get("/v1/self", headers=host).text)
    blobs.append(c.get("/v1/status", headers=host).text)
    blobs.append(c.post("/v1/reconcile", headers=host).text)
    for b in blobs:
        assert WG_SECRET not in b

    # exercise a test + probe so rows land in the metrics DB, then check the DB
    c.post("/v1/routes/blanc:FI-WG-1/test", headers=host)
    c.post("/v1/probe-domain", headers=host,
           json={"domain": "probe.invalid", "routes": ["blanc:FI-WG-1"]})
    assert WG_SECRET not in _dump_all_tables(app)

    # the public-key (not a secret) is fine to be absent too (catalog stores no keys)
    assert "pub-key-not-a-secret" not in c.get("/v1/routes/blanc:FI-WG-1", headers=host).text


# ===========================================================================
# 6b. version negotiation (spec 49.42)
# ===========================================================================
def test_status_reports_backend_version(egress):
    """/v1/status must report the egressd version even when mihomo is
    unreachable — the client needs it for version negotiation (spec 49.42;
    2026-10-15 live run: mihomo unreachable -> no version -> check fail)."""
    c, host = egress["client"], egress["host"]
    data = c.get("/v1/status", headers=host).json()
    assert data.get("version"), "backend did not report a version"


# ===========================================================================
# 7. per-client fallback to direct
# ===========================================================================
def test_per_client_fallback_to_direct(egress):
    c, host, os_, provs = egress["client"], egress["host"], egress["os"], egress["provs"]

    # host -> FI-Helsinki-1 (will be deleted); os -> FI-Helsinki-2 (stays)
    c.put("/v1/default", headers=host, json={"route": "blanc:FI-Helsinki-1"})
    c.put("/v1/default", headers=os_, json={"route": "blanc:FI-Helsinki-2"})

    # delete only FI-Helsinki-1
    (provs / "blanc.yaml").write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-2", "type": "socks5", "server": "1.1.1.2", "port": 1080},
        {"name": "FI-WG-1", "type": "wireguard", "server": "wg.example.com",
         "port": 51820, "ip": "10.66.0.2", "private-key": WG_SECRET,
         "public-key": "pub", "allowed-ips": ["0.0.0.0/0"]},
    ]}, sort_keys=False))

    r = c.post("/v1/reconcile", headers=host).json()
    # only the affected client (host-main) falls back
    assert len(r["fallbacks"]) == 1
    assert r["fallbacks"][0]["client_id"] == "host-main"
    assert r["fallbacks"][0]["from_route"] == "blanc:FI-Helsinki-1"
    assert r["fallbacks"][0]["to_route"] == "direct"

    assert c.get("/v1/default", headers=host).json()["route"] == "direct"
    # os's default is untouched (its route still exists)
    assert c.get("/v1/default", headers=os_).json()["route"] == "blanc:FI-Helsinki-2"


# ===========================================================================
# 8. no remote exec / raw mihomo passthrough
# ===========================================================================
def test_no_remote_exec_or_raw_passthrough(egress):
    app = egress["app"]
    c, host = egress["client"], egress["host"]

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    # no endpoint that executes commands or exposes raw mihomo/admin
    for bad in ("exec", "command", "mihomo", "raw", "shell", "run",
                "passthrough", "admin", "secret", "eval", "system"):
        assert not any(bad in p.lower() for p in paths), f"forbidden path with {bad!r}: {paths}"

    # the documented public API is present
    for want in ("/v1/self", "/v1/status", "/v1/capabilities", "/v1/routes",
                 "/v1/default", "/v1/default/mode", "/v1/providers",
                 "/v1/probe-domain", "/v1/benchmark", "/v1/oneshot/session",
                 "/v1/reconcile", "/v1/doctor", "/v1/geo/status"):
        assert want in paths, f"missing public endpoint {want}"

    # a 'command' field in a one-shot session body is ignored (never executed)
    r = c.post("/v1/oneshot/session", headers=host,
               json={"route": "blanc:FI-Helsinki-1",
                     "command": "rm -rf /", "argv": ["sh", "-c", "id"]})
    assert r.status_code == 200
    body = r.json()
    assert "command" not in body and "argv" not in body
    assert body["route"] == "blanc:FI-Helsinki-1"
    # clean up
    c.delete(f"/v1/oneshot/session/{body['id']}", headers=host)


# ===========================================================================
# 9. domain probe error classification
# ===========================================================================
def test_probe_error_classification_pure():
    # each spec-24 class is reachable and distinct
    def o(**kw):
        base = dict(route_id="r", domain="x")
        base.update(kw)
        return ProbeOutcome(**base)

    # data-plane-driven: a client-side NXDOMAIN alone is context, not a block
    # (the domain is resolved at the exit); it only tips a host-unreachable TCP
    # failure toward dns_failure.
    assert classify_probe(o(dns_ok=False, tcp_ok=False, socks_code=4)) == DNS_FAILURE
    assert classify_probe(o(dns_ok=False, tcp_ok=False)) == TCP_REFUSED
    assert classify_probe(o(dns_ok=True, tcp_ok=False, detail={"tcp_timeout": True})) == TCP_TIMEOUT
    assert classify_probe(o(dns_ok=True, tcp_ok=False, socks_code=4)) == TCP_REFUSED
    assert classify_probe(o(dns_ok=True, tcp_ok=False)) == TCP_REFUSED
    assert classify_probe(o(dns_ok=True, tcp_ok=True, tls_ok=False)) == TLS_FAILURE
    assert classify_probe(o(dns_ok=True, tcp_ok=True, tls_ok=True, detail={"http_reset": True})) == CONNECTION_RESET
    assert classify_probe(o(dns_ok=True, tcp_ok=True, tls_ok=True, detail={"http_timeout": True})) == HTTP_TIMEOUT
    assert classify_probe(o(dns_ok=True, tcp_ok=True, tls_ok=True, status=403)) == HTTP_STATUS_DENIED
    assert classify_probe(o(dns_ok=True, tcp_ok=True, tls_ok=True, status=302)) == REDIRECT
    assert classify_probe(o(dns_ok=True, tcp_ok=True, tls_ok=True, status=200)) == OK
    # every class is a known class
    for cls in (DNS_FAILURE, TCP_TIMEOUT, TCP_REFUSED, TLS_FAILURE,
                HTTP_STATUS_DENIED, REDIRECT, CONNECTION_RESET, HTTP_TIMEOUT,
                OK, SLOT_EXHAUSTED):
        assert cls in ALL_ERROR_CLASSES


class _SocksStub:
    """Minimal SOCKS5 stub to exercise the real socks_probe + classifier."""

    def __init__(self, code: int = 0, http: bytes = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"):
        self.code = code
        self.http = http
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            conn.recv(2)                      # greeting
            conn.sendall(b"\x05\x02")         # username/password
            conn.recv(32)                     # auth
            conn.sendall(b"\x01\x00")         # auth ok
            conn.recv(1024)                   # connect request
            if self.code == 0:
                conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # success (atyp=1)
                conn.sendall(self.http)
            else:
                conn.sendall(b"\x05" + bytes([self.code]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")
            # keep the connection open so the client can read the response
            while True:
                d = conn.recv(65536)
                if not d:
                    break
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except Exception:
            pass
        self.thread.join(timeout=2)


def test_probe_classification_via_socks_stub():
    # success -> 200 -> ok
    stub = _SocksStub(code=0, http=b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    o = socks_probe("127.0.0.1", stub.port, "u", "p", "example.com",
                    scheme="http", check=("tcp", "http"), expected=(200, 399))
    assert o.error == OK and o.status == 200 and o.reachable
    stub.close()

    # success -> 403 -> http_status_denied
    stub = _SocksStub(code=0, http=b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    o = socks_probe("127.0.0.1", stub.port, "u", "p", "example.com",
                    scheme="http", check=("tcp", "http"), expected=(200, 399))
    assert o.error == HTTP_STATUS_DENIED and o.status == 403 and not o.reachable
    stub.close()

    # connect refused (SOCKS code 4) -> tcp_refused
    stub = _SocksStub(code=4)
    o = socks_probe("127.0.0.1", stub.port, "u", "p", "example.com",
                    scheme="http", check=("tcp",), expected=(200, 399))
    assert o.error == TCP_REFUSED and not o.reachable
    stub.close()


def test_probe_endpoint_returns_classified_result(egress):
    c, host = egress["client"], egress["host"]
    # no listener on the oneshot port in this offline env -> the probe must
    # still return a well-formed, classified result (not crash)
    r = c.post("/v1/probe-domain", headers=host,
               json={"domain": "probe.invalid", "routes": ["blanc:FI-Helsinki-1"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    res = body["results"][0]
    assert res["route"] == "blanc:FI-Helsinki-1"
    assert res["error"] in ALL_ERROR_CLASSES
    for k in ("reachable", "dns_ok", "tcp_ok", "tls_ok", "http_ok", "status",
              "latency_ms", "resolved_ip", "exit_ip", "exit_country", "asn"):
        assert k in res


# ===========================================================================
# supporting coverage
# ===========================================================================
def test_stable_route_ids(egress):
    c, host = egress["client"], egress["host"]
    routes = c.get("/v1/routes", headers=host).json()["routes"]
    ids = {r["id"] for r in routes}
    # direct is always present as a synthetic route
    assert "direct" in ids
    # stable provider:stable-id form
    assert "blanc:FI-Helsinki-1" in ids
    assert "xeovo:US-New-York-1" in ids
    # country is parsed from the stable name (catalog classification)
    fi = next(r for r in routes if r["id"] == "blanc:FI-Helsinki-1")
    assert fi["country"] == "FI"
    # filter by country
    fi_only = c.get("/v1/routes", headers=host, params={"country": "FI"}).json()["routes"]
    assert all(r["country"] == "FI" for r in fi_only)
    assert all(r["id"].startswith("blanc:") for r in fi_only)


def test_default_persistence(egress, tmp_path):
    c, host = egress["client"], egress["host"]
    c.put("/v1/default", headers=host, json={"route": "blanc:FI-Helsinki-1"})
    c.put("/v1/default/mode", headers=host, json={"mode": "proxy"})
    # the state file was written
    state_file = egress["tmp"] / "state" / "defaults.json"
    assert state_file.is_file()
    data = json.loads(state_file.read_text())
    assert data["clients"]["host-main"]["route"] == "blanc:FI-Helsinki-1"


def test_mode_tun_unsupported_no_silent_fallback(egress):
    c, host = egress["client"], egress["host"]
    # tun is not in the client's allowed modes -> explicit unsupported, 400
    r = c.put("/v1/default/mode", headers=host, json={"mode": "tun"})
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"]
    # the mode was NOT silently changed to proxy
    assert c.get("/v1/default/mode", headers=host).json()["mode"] == "proxy"


def test_proxy_default_capability_no_provider_secrets(egress):
    c, host = egress["client"], egress["host"]
    r = c.get("/v1/proxy/default-capability", headers=host).json()
    # the client's own default-proxy credential (not provider secrets)
    assert r["username"] == "host-user"
    assert r["password"] == "host-pass"
    assert r["port"] == 18080
    # no provider private keys leak
    assert WG_SECRET not in json.dumps(r)


def test_metrics_db_schema(egress):
    app = egress["app"]
    db = sqlite3.connect(str(app.state.metrics.path))
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    for want in ("clients", "routes", "health_samples", "probe_samples",
                 "benchmark_samples", "provider_update_events",
                 "default_selection_events", "oneshot_sessions", "geo_cache",
                 "meta"):
        assert want in tables, f"missing table {want}"
    # schema version recorded
    assert app.state.metrics._query(
        "SELECT value FROM meta WHERE key='schema_version'")[0]["value"] == "1"


def test_unknown_route_rejected(egress):
    c, host = egress["client"], egress["host"]
    assert c.get("/v1/routes/nope:nope", headers=host).status_code == 404
    assert c.put("/v1/default", headers=host, json={"route": "nope:nope"}).status_code == 400
    assert c.post("/v1/oneshot/session", headers=host,
                  json={"route": "nope:nope"}).status_code == 400


def test_requires_auth(egress):
    c = egress["client"]
    for path in ("/v1/self", "/v1/routes", "/v1/default", "/v1/oneshot/active",
                 "/v1/proxy/default-capability", "/v1/doctor"):
        assert c.get(path).status_code == 401
    assert c.put("/v1/default", json={"route": "direct"}).status_code == 401
    # health/ready are unauthenticated (compose healthchecks)
    assert c.get("/v1/health").status_code == 200
    assert c.get("/v1/ready").status_code == 200
