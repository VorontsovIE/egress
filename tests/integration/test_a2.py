"""A2 — Mihomo / Providers / GEO tests (offline, no mihomo binary).

Covers the spec's A2 checks that can be exercised without a live
Mihomo or network:

* provider adapters: format detection, normalization, stable route IDs,
  secret redaction, atomic write + last-known-good, failed-input handling
  (spec 11.2 adapter obligations);
* GEO update: download + atomic write + metadata + status + staleness,
  idempotency, force, and "a failed update never destroys the previous
  working state" (spec 12/14/41);
* config generator: geo-auto-update / geo-update-interval present
  (spec 14.1) and node-collision validation (spec A2 "node collisions").

The live-Mihomo checks (config validation, client selector isolation,
concurrent one-shot, mapping scale, Docker bypass precedence, GEO update
against a running mihomo) are in ``test_a2_live.py``.

Run:  python -m pytest tests/integration/test_a2.py -v
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

from adapters import (  # noqa: E402
    atomic_write,
    atomic_write_bytes,
    detect_format,
    redact,
    run_adapter,
    stable_slug,
)
from egressd.configgen import (  # noqa: E402
    ClientDef,
    EgressConfig,
    OneShotSlot,
    ProviderDef,
)
from egressd.geo_update import DATASET_NAMES, GeoConfig, GeoManager  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# format detection
# ---------------------------------------------------------------------------
def test_detect_clash_yaml():
    assert detect_format("proxies:\n  - name: a\n    type: socks5\n") == "clash"


def test_detect_base64_clash():
    src = base64.b64encode(b"proxies:\n  - name: a\n    type: socks5\n").decode()
    assert detect_format(src) == "base64"


def test_detect_wg_conf():
    conf = "[Interface]\nAddress = 10.0.0.2/32\nPrivateKey = x\n[Peer]\nPublicKey = y\n"
    assert detect_format(conf) == "wg-conf"


def test_detect_vless():
    assert detect_format("vless://uuid@host:443?security=tls") == "vless"


def test_detect_rejects_garbage():
    with pytest.raises(ValueError):
        detect_format("this is not a subscription at all")


# ---------------------------------------------------------------------------
# normalization + stable route IDs
# ---------------------------------------------------------------------------
def test_stable_slug_deterministic():
    assert stable_slug("FI - Helsinki 1") == "FI-Helsinki-1"
    assert stable_slug("US – New York 2") == "US-New-York-2"
    # same display always -> same id (spec 13: stable id, not display name)
    assert stable_slug("FI - Helsinki 1") == stable_slug("FI - Helsinki 1")


def test_blanc_clean_stable_names():
    # The adapter produces clean, stable node names (no provider tag). The
    # ``blanc-`` tag is applied by the provider config's additional-prefix
    # (spec 11.1), not baked into the file.
    clash = yaml.safe_dump({"proxies": [
        {"name": "FI - Helsinki 1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
    ]})
    y, meta = run_adapter("blanc", clash)
    out = yaml.safe_load(y)
    assert out["proxies"][0]["name"] == "FI-Helsinki-1"
    assert meta["nodes"] == ["FI-Helsinki-1"]
    assert meta["format"] == "clash"


def test_xeovo_base64_clean_names():
    clash = yaml.safe_dump({"proxies": [
        {"name": "US - New York 1", "type": "socks5", "server": "2.2.2.2", "port": 1080},
    ]})
    b64 = base64.b64encode(clash.encode()).decode()
    y, meta = run_adapter("xeovo", b64)
    out = yaml.safe_load(y)
    assert out["proxies"][0]["name"] == "US-New-York-1"
    assert meta["format"] == "base64"


def test_explicit_prefix_still_applied():
    # Standalone use: an explicit prefix is applied to the node names.
    clash = yaml.safe_dump({"proxies": [
        {"name": "FI - Helsinki 1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
    ]})
    y, _ = run_adapter("blanc", clash, prefix="blanc-")
    out = yaml.safe_load(y)
    assert out["proxies"][0]["name"] == "blanc-FI-Helsinki-1"


def test_wg_conf_normalized():
    conf = """[Interface]
Address = 10.66.0.2/32
PrivateKey = priv
[Peer]
PublicKey = pub
AllowedIPs = 0.0.0.0/0
Endpoint = wg.example.com:51820
PersistentKeepalive = 25
"""
    y, meta = run_adapter("blanc", conf)
    out = yaml.safe_load(y)
    p = out["proxies"][0]
    assert p["type"] == "wireguard"
    assert p["server"] == "wg.example.com"
    assert p["port"] == 51820
    assert p["ip"] == "10.66.0.2"
    assert p["allowed-ips"] == ["0.0.0.0/0"]
    assert p["persistent-keepalive"] == 25
    assert p["name"].startswith("wireguard-")


def test_vless_normalized():
    uri = ("vless://11111111-2222-3333-4444-555555555555@vless.example.com:443"
           "?encryption=none&security=reality&pbk=REALITYKEY&sid=0011"
           "&sni=vless.example.com&fp=firefox&spx=%2Fexample&type=tcp")
    y, meta = run_adapter("xeovo", uri)
    out = yaml.safe_load(y)
    p = out["proxies"][0]
    assert p["type"] == "vless"
    assert p["server"] == "vless.example.com"
    assert p["port"] == 443
    assert p["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert p["reality-opts"]["public-key"] == "REALITYKEY"
    assert p["reality-opts"]["short-id"] == "0011"
    assert p["servername"] == "vless.example.com"
    assert p["client-fingerprint"] == "firefox"
    assert "fingerprint" not in p["reality-opts"]
    assert "spider-x" not in p["reality-opts"]
    assert p["name"].startswith("vless-")


def test_empty_subscription_rejected():
    with pytest.raises(ValueError):
        run_adapter("blanc", "   ")


def test_duplicate_node_names_disambiguated():
    # Mihomo silently loads duplicate node names (a routing hazard). The
    # adapter must produce distinct stable IDs for different endpoints.
    dup = yaml.safe_dump({"proxies": [
        {"name": "dup-node", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "dup-node", "type": "socks5", "server": "2.2.2.2", "port": 1080},
    ]})
    normalized, meta = run_adapter("blanc", dup)
    names = [p["name"] for p in yaml.safe_load(normalized)["proxies"]]
    assert len(names) == len(set(names)) == meta["count"] == 2
    assert all(name.startswith("dup-node-") for name in names)
    reversed_nodes = list(reversed(yaml.safe_load(dup)["proxies"]))
    reversed_yaml = yaml.safe_dump({"proxies": reversed_nodes})
    reordered, _ = run_adapter("blanc", reversed_yaml)
    assert set(names) == {p["name"] for p in yaml.safe_load(reordered)["proxies"]}
    # distinct names are fine
    ok = yaml.safe_dump({"proxies": [
        {"name": "a", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "b", "type": "socks5", "server": "2.2.2.2", "port": 1080},
    ]})
    y, meta = run_adapter("blanc", ok)
    assert meta["count"] == 2


# ---------------------------------------------------------------------------
# secret redaction (spec 11.2: never log private keys)
# ---------------------------------------------------------------------------
def test_redact_masks_secrets():
    node = {
        "name": "n", "type": "wireguard", "server": "h", "port": 51820,
        "private-key": "TOPSECRET", "uuid": "u-1", "password": "pw",
        "public-key": "pub",  # public keys are NOT secrets
    }
    r = redact(node)
    assert r["private-key"] == "***REDACTED***"
    assert r["uuid"] == "***REDACTED***"
    assert r["password"] == "***REDACTED***"
    assert r["public-key"] == "pub"  # untouched
    assert "TOPSECRET" not in json.dumps(r)


def test_adapter_meta_never_contains_secret_values():
    clash = yaml.safe_dump({"proxies": [
        {"name": "n", "type": "wireguard", "server": "h", "port": 51820,
         "private-key": "TOPSECRET-VALUE", "public-key": "pub"},
    ]})
    y, meta = run_adapter("blanc", clash)
    # meta is what would be logged: only names + format, no secret values
    assert "TOPSECRET-VALUE" not in json.dumps(meta)
    assert meta["nodes"] == ["n"]


# ---------------------------------------------------------------------------
# atomic write + last-known-good
# ---------------------------------------------------------------------------
def test_atomic_write_and_lkg(tmp_path):
    p = tmp_path / "out.yaml"
    atomic_write(p, "v1\n")
    assert p.read_text() == "v1\n"
    assert (tmp_path / "out.yaml.lkg").read_text() == "v1\n"
    # update -> lkg follows the last good state
    atomic_write(p, "v2\n")
    assert p.read_text() == "v2\n"
    assert (tmp_path / "out.yaml.lkg").read_text() == "v2\n"
    # no stray temp files
    assert not list(tmp_path.glob(".out.yaml.*.tmp"))


def test_atomic_write_bytes_roundtrip(tmp_path):
    p = tmp_path / "geo.dat"
    data = bytes(range(256)) * 10  # binary, includes 0x00 and 0xFF
    atomic_write_bytes(p, data)
    assert p.read_bytes() == data  # exact binary round trip (no text mangling)
    assert (tmp_path / "geo.dat.lkg").read_bytes() == data


def test_atomic_write_creates_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "out.yaml"
    atomic_write(p, "x\n")
    assert p.read_text() == "x\n"


# ---------------------------------------------------------------------------
# fixtures (spec 11.2: fixtures without secrets)
# ---------------------------------------------------------------------------
def test_fixture_blanc_clash():
    text = (FIX / "blanc_clash.yaml").read_text()
    y, meta = run_adapter("blanc", text)
    names = [p["name"] for p in yaml.safe_load(y)["proxies"]]
    assert meta["count"] == 4
    assert "FI-Helsinki-1" in names
    assert "DE-FRA-1" in names
    # no real secret leaks into the normalized output's meta
    assert "fake-private-key-do-not-use-0001" not in json.dumps(meta)


def test_fixture_blanc_wireguard():
    text = (FIX / "blanc_wireguard.conf").read_text()
    y, meta = run_adapter("blanc", text)
    assert meta["format"] == "wg-conf"
    p = yaml.safe_load(y)["proxies"][0]
    assert p["type"] == "wireguard"
    assert p["server"] == "wg.example.com"


def test_fixture_xeovo_base64():
    text = (FIX / "xeovo_base64.txt").read_text()
    y, meta = run_adapter("xeovo", text)
    assert meta["format"] == "base64"
    assert meta["count"] == 3
    names = [p["name"] for p in yaml.safe_load(y)["proxies"]]
    assert "US-New-York-1" in names
    assert "DE-FRA-1" in names


def test_fixture_xeovo_vless():
    text = (FIX / "xeovo_vless.txt").read_text()
    y, meta = run_adapter("xeovo", text)
    assert meta["format"] == "vless"
    assert meta["count"] == 2
    p0 = yaml.safe_load(y)["proxies"][0]
    assert p0["reality-opts"]["public-key"] == "fake-reality-pubkey-do-not-use"


# ---------------------------------------------------------------------------
# GEO update (spec 14/41)
# ---------------------------------------------------------------------------
def _mgr(tmp_path, fetcher=None):
    if fetcher is None:
        fetcher = lambda url: (url + "x" * 2000).encode()
    return GeoManager(GeoConfig(geo_dir=tmp_path / "geo", fetcher=fetcher,
                                interval_hours=24))


def test_geo_update_writes_all(tmp_path):
    mgr = _mgr(tmp_path)
    res = mgr.update(now=1000.0)
    assert all(r["updated"] for r in res.values())
    for name in DATASET_NAMES:
        assert (tmp_path / "geo" / mgr.cfg.filenames[name]).is_file()
    assert (tmp_path / "geo" / "geo-metadata.json").is_file()


def test_geo_update_idempotent_no_rewrite(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.update(now=1000.0)
    mtimes = {n: (tmp_path / "geo" / mgr.cfg.filenames[n]).stat().st_mtime_ns
              for n in DATASET_NAMES}
    res = mgr.update(now=2000.0)
    assert all(r["reason"] == "unchanged" for r in res.values())
    mtimes2 = {n: (tmp_path / "geo" / mgr.cfg.filenames[n]).stat().st_mtime_ns
               for n in DATASET_NAMES}
    assert mtimes == mtimes2  # unchanged -> no rewrite


def test_geo_update_force(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.update(now=1000.0)
    mgr.cfg.fetcher = lambda url: (url + "y" * 2000).encode()
    res = mgr.update(names=("geoip",), force=True, now=3000.0)
    assert res["geoip"]["updated"]


def test_geo_failed_fetch_preserves_previous(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.update(now=1000.0)
    before = (tmp_path / "geo" / mgr.cfg.filenames["geosite"]).read_bytes()

    def boom(url):
        raise OSError("network down")

    mgr.cfg.fetcher = boom
    res = mgr.update(names=("geosite",), force=True, now=4000.0)
    assert res["geosite"]["updated"] is False
    assert "fetch failed" in res["geosite"]["reason"]
    # previous working file untouched (spec 12: failed update must not destroy
    # previous working provider/GEO state)
    assert (tmp_path / "geo" / mgr.cfg.filenames["geosite"]).read_bytes() == before


def test_geo_rejects_tiny_download(tmp_path):
    mgr = _mgr(tmp_path, fetcher=lambda url: b"too small")
    res = mgr.update(names=("geoip",), force=True, now=1000.0)
    assert res["geoip"]["updated"] is False
    assert "too small" in res["geoip"]["reason"]
    assert not (tmp_path / "geo" / mgr.cfg.filenames["geoip"]).exists()


def test_geo_status_fields(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.update(now=1000.0)
    st = mgr.status(now=2000.0)
    assert st["all_present"]
    g = st["datasets"]["geoip"]
    assert g["present"] and g["sha256"] and g["size"] > 0
    assert g["source"].startswith("https://")
    assert g["last_update"] is not None
    assert g["age_hours"] == pytest.approx((2000 - 1000) / 3600.0, abs=1e-3)
    assert g["stale"] is False  # 0.28h < 24h interval


def test_geo_status_stale(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.update(now=0.0)
    st = mgr.status(now=100 * 3600.0)  # 100h later > 24h interval
    assert st["datasets"]["geoip"]["stale"] is True


def test_geo_status_missing(tmp_path):
    mgr = _mgr(tmp_path)
    st = mgr.status()
    assert not st["all_present"]
    assert st["datasets"]["geoip"]["present"] is False


def test_geo_lkg_written(tmp_path):
    mgr = _mgr(tmp_path)
    mgr.update(now=1000.0)
    assert (tmp_path / "geo" / (mgr.cfg.filenames["geoip"] + ".lkg")).is_file()


# ---------------------------------------------------------------------------
# config generator: GEO auto-update (spec 14.1) + node collisions
# ---------------------------------------------------------------------------
def _cfg(**kw):
    base = dict(
        mihomo_secret="s",
        clients=[ClientDef("host-main", "hu", "hp")],
        providers=[ProviderDef("blanc", type="file", path="/p/blanc.yaml", prefix="blanc-")],
        oneshot_slots=[OneShotSlot(0, "os_0", "sp")],
    )
    base.update(kw)
    return EgressConfig(**base)


def test_configgen_geo_auto_update_present():
    cfg = _cfg().generate()
    assert cfg["geo-auto-update"] is True
    assert cfg["geo-update-interval"] == 24
    assert "geoip" in cfg["geox-url"] and "mmdb" in cfg["geox-url"]


def test_configgen_geo_auto_update_off():
    cfg = _cfg(geo_auto_update=False, geo_update_interval=6).generate()
    assert cfg["geo-auto-update"] is False
    assert cfg["geo-update-interval"] == 6


def test_configgen_node_collision_provider_vs_client():
    with pytest.raises(ValueError):
        _cfg(providers=[ProviderDef("host-main", type="file", path="/p/x.yaml")]).validate()


def test_configgen_node_collision_duplicate_provider():
    with pytest.raises(ValueError):
        _cfg(providers=[
            ProviderDef("blanc", type="file", path="/p/a.yaml"),
            ProviderDef("blanc", type="file", path="/p/b.yaml"),
        ]).validate()


def test_configgen_node_collision_reserved_direct():
    with pytest.raises(ValueError):
        _cfg(providers=[ProviderDef("DIRECT", type="file", path="/p/d.yaml")]).validate()


def test_configgen_node_collision_oneshot_vs_client():
    with pytest.raises(ValueError):
        _cfg(oneshot_slots=[OneShotSlot(0, "host-main", "sp")]).validate()


def test_configgen_generated_is_valid_yaml():
    data = _cfg().generate()
    text = yaml.safe_dump(data, sort_keys=False)
    reparsed = yaml.safe_load(text)
    assert reparsed["profile"]["store-selected"] is True
    # rules order: hard-direct first, MATCH,DIRECT last
    assert reparsed["rules"][0] == "GEOIP,LAN,DIRECT,no-resolve"
    assert reparsed["rules"][-1] == "MATCH,DIRECT"


# ---------------------------------------------------------------------------
# egressd /v1/geo/* endpoints (spec 41)
# ---------------------------------------------------------------------------
@pytest.fixture()
def egress_client(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    (clients / "host-main.token").write_text("tok-host\n")
    (clients / "host-main.profile.json").write_text(
        json.dumps({"client_id": "host-main", "environment": "host", "modes": ["proxy"]})
    )
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    from egressd.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    # offline fetcher
    app.state.geo.cfg.fetcher = lambda url: (url + "z" * 2000).encode()
    return TestClient(app), {"Authorization": "Bearer tok-host"}


def test_egressd_geo_status_and_update(egress_client):
    c, tok = egress_client
    r = c.get("/v1/geo/status", headers=tok)
    assert r.status_code == 200
    assert r.json()["client_id"] == "host-main"
    assert r.json()["all_present"] is False

    r = c.post("/v1/geo/update", headers=tok, json={})
    assert r.status_code == 200
    assert set(r.json()["updated"]) == set(DATASET_NAMES)

    r = c.get("/v1/geo/status", headers=tok)
    g = r.json()["datasets"]["geoip"]
    assert g["present"] and g["sha256"] and g["stale"] is False


def test_egressd_geo_update_subset_and_force(egress_client):
    c, tok = egress_client
    c.post("/v1/geo/update", headers=tok, json={})
    r = c.post("/v1/geo/update", headers=tok,
               json={"datasets": ["geoip"], "force": True})
    assert r.json()["updated"] == ["geoip"]


def test_egressd_geo_requires_auth(egress_client):
    c, _ = egress_client
    assert c.get("/v1/geo/status").status_code == 401
    assert c.post("/v1/geo/update", json={}).status_code == 401


def test_egressd_geo_unknown_dataset_400(egress_client):
    c, tok = egress_client
    r = c.post("/v1/geo/update", headers=tok, json={"datasets": ["nope"]})
    assert r.status_code == 400
