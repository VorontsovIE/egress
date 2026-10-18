"""Ownership and persistence checks for private provider snapshots."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))


def test_container_snapshot_private_and_no_raw_source(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    for cid in ("alice", "bob"):
        (clients / f"{cid}.token").write_text(f"token-{cid}")
        (clients / f"{cid}.profile.json").write_text(json.dumps(
            {"environment": "container", "modes": ["proxy"]}))
    providers = tmp_path / "providers"
    providers.mkdir()
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(providers))
    monkeypatch.setenv("EGRESSD_PERSONAL_DIR", str(tmp_path / "personal"))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_METRICS_DB", str(tmp_path / "state" / "metrics.sqlite"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    from egressd.main import create_app
    api = TestClient(create_app())
    alice = {"Authorization": "Bearer token-alice"}
    bob = {"Authorization": "Bearer token-bob"}
    snapshot = yaml.safe_dump({"proxies": [{"name": "test", "type": "socks5",
                                          "server": "1.1.1.1", "port": 443}]})
    assert api.post("/v1/personal/providers", headers=alice,
                    json={"name": "forbidden", "kind": "file", "source": snapshot}).status_code == 403
    added = api.put("/v1/personal/providers/demo/snapshot", headers=alice,
                    json={"yaml": snapshot})
    assert added.status_code == 200
    provider = added.json()["provider_id"]
    assert api.get("/v1/personal/providers", headers=bob).json()["providers"] == []
    assert api.get("/v1/personal/providers", headers=alice).json()["providers"][0]["alias"] == "demo"
    routes_a = api.get("/v1/routes", headers=alice).json()["routes"]
    routes_b = api.get("/v1/routes", headers=bob).json()["routes"]
    assert any(r.get("provider") == provider for r in routes_a)
    assert all(r.get("provider") != provider for r in routes_b)
    assert api.post(f"/v1/providers/{provider}/update", headers=bob, json={}).status_code == 404
    assert api.delete("/v1/personal/providers/demo", headers=bob).status_code == 404
    assert not any("source" in value for value in added.json())
    assert api.delete("/v1/personal/providers/demo", headers=alice).status_code == 200
    assert not (providers / f"{provider}.yaml").exists()


def test_reject_private_proxy_address():
    from egressd.personal import validate_yaml
    raw = yaml.safe_dump({"proxies": [{"name": "inside", "type": "socks5",
                                     "server": "127.0.0.1", "port": 443}]})
    try:
        validate_yaml(raw)
    except ValueError:
        pass
    else:
        raise AssertionError("private address accepted")


def test_personal_provider_accepts_more_than_300_nodes():
    from egressd.personal import validate_yaml
    raw = yaml.safe_dump({"proxies": [
        {"name": f"node-{i}", "type": "socks5", "server": "1.1.1.1", "port": 1080}
        for i in range(317)
    ]})
    normalized, count = validate_yaml(raw)
    assert count == 317
    assert len(yaml.safe_load(normalized)["proxies"]) == 317


def test_personal_provider_rejects_empty_nodes():
    from egressd.personal import validate_yaml
    try:
        validate_yaml("proxies: []\n")
    except ValueError as exc:
        assert "at least one node" in str(exc)
    else:
        raise AssertionError("empty personal provider accepted")


def test_personal_domain_is_pinned_before_mihomo_dials(monkeypatch):
    from egressd import personal
    monkeypatch.setattr(personal, "_public_addresses", lambda host: ["1.1.1.1"])
    raw = yaml.safe_dump({"proxies": [{"name": "remote", "type": "trojan",
                                     "server": "public.example", "port": 443,
                                     "password": "fake-do-not-use"}]})
    normalized, _ = personal.validate_yaml(raw)
    node = yaml.safe_load(normalized)["proxies"][0]
    assert node["server"] == "1.1.1.1"
    assert node["servername"] == "public.example"


def test_mihomo_default_groups_exclude_foreign_personal_provider():
    from egressd.configgen import ClientDef, EgressConfig, OneShotSlot, ProviderDef
    cfg = EgressConfig(
        mihomo_secret="fake-do-not-use",
        clients=[ClientDef("alice", "alice-user", "fake-a"),
                 ClientDef("bob", "bob-user", "fake-b")],
        providers=[ProviderDef("global", path="/p/global.yaml"),
                   ProviderDef("private", path="/p/private.yaml", owner="alice")],
        oneshot_slots=[OneShotSlot(0, "os_0", "fake-slot")],
    ).generate()
    groups = {group["name"]: group for group in cfg["proxy-groups"]}
    assert groups["alice"]["use"] == ["global", "private"]
    assert groups["bob"]["use"] == ["global"]


def test_personal_url_rejects_private_redirect(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from egressd import personal

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(personal, "_public_addresses",
                        lambda host: ["127.0.0.1"] if host == "public.example" else [])
    try:
        try:
            personal.fetch_url(f"http://public.example:{server.server_port}/source")
        except ValueError:
            pass
        else:
            raise AssertionError("private redirect followed")
    finally:
        server.shutdown()
        server.server_close()
