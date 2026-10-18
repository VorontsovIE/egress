"""Provider source import boundaries and transport contracts."""
import base64
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

from adapters.common import normalize_source
from egressd.provider_fetch import normalize_url
from egressd.provider_sources import REFRESH_INTERVAL


@pytest.mark.parametrize("source,kind", [
    ("vless://uuid@proxy.example:443?security=tls#node", "vless"),
    ("trojan://secret@proxy.example:443#node", "trojan"),
    ("ss://YWVzLTEyOC1nY206cGFzcw==@proxy.example:443#node", "ss"),
    ("hysteria2://secret@proxy.example:443#node", "hysteria2"),
    ("tuic://uuid:secret@proxy.example:443#node", "tuic"),
    ("vmess://" + base64.b64encode(json.dumps({"ps": "node", "add": "proxy.example",
       "port": "443", "id": "uuid", "aid": "0", "scy": "auto"}).encode()).decode(), "vmess"),
])
def test_uri_formats(source, kind):
    output, meta = normalize_source(source)
    proxy = yaml.safe_load(output)["proxies"][0]
    assert proxy["type"] == kind
    assert proxy["server"] == "proxy.example"
    assert meta["count"] == 1


def test_base64_uri_list_and_clash_yaml():
    links = "trojan://one@a.example:443#one\ntrojan://two@b.example:443#two"
    output, meta = normalize_source(base64.b64encode(links.encode()).decode())
    assert meta["count"] == 2
    assert len(yaml.safe_load(output)["proxies"]) == 2
    clash = "proxies:\n  - name: proxy\n    type: socks5\n    server: a.example\n    port: 1080\n"
    assert normalize_source(clash)[1]["count"] == 1


def test_reality_fields_in_mixed_uri_source():
    source = ("vless://fake-uuid@proxy.example:443?security=reality&pbk=fake-key"
              "&sid=0011&sni=cover.example&fp=firefox&spx=%2Fignored#one\n"
              "trojan://fake-password@other.example:443#two")
    output, meta = normalize_source(source)
    assert meta["format"] == "uris"
    node = yaml.safe_load(output)["proxies"][0]
    assert node["servername"] == "cover.example"
    assert node["client-fingerprint"] == "firefox"
    assert node["reality-opts"] == {"public-key": "fake-key", "short-id": "0011"}


def test_duplicate_uri_labels_get_stable_unique_names():
    first = "hysteria2://fake-a@1.1.1.1:443#US-SLC-Hysteria2"
    second = "hysteria2://fake-b@8.8.8.8:443#US-SLC-Hysteria2"
    forward, _ = normalize_source(first + "\n" + second)
    reverse, _ = normalize_source(second + "\n" + first)
    forward_nodes = yaml.safe_load(forward)["proxies"]
    reverse_nodes = yaml.safe_load(reverse)["proxies"]
    names = {node["name"] for node in forward_nodes}
    assert len(names) == 2
    assert all(name.startswith("US-SLC-Hysteria2-") for name in names)
    assert names == {node["name"] for node in reverse_nodes}
    assert all("fake-" not in name for name in names)


def test_invalid_or_empty_never_normalizes():
    for source in ("", "proxies: []", "proxies: [bad]", "trojan://missing"):
        with pytest.raises((ValueError, TypeError)):
            normalize_source(source)


def test_https_default_explicit_http_and_interval():
    assert normalize_url("example.com/sub") == "https://example.com/sub"
    assert normalize_url("http://example.com/sub") == "http://example.com/sub"
    assert REFRESH_INTERVAL == 600


def test_source_inventory_separates_metadata_and_artifact(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    artifacts = tmp_path / "artifacts"
    secrets.mkdir()
    artifacts.mkdir()
    monkeypatch.setenv("EGRESSD_PROVIDER_SOURCES_DIR", str(secrets))
    monkeypatch.setenv("EGRESSD_PROVIDER_STATUS_DIR", str(tmp_path / "status"))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    from egressd.main import scan_providers, _fetch_and_write_provider
    (secrets / "alpha.json").write_text(json.dumps({"type": "file"}))
    (secrets / "alpha.source").write_text("trojan://secret@one.example:443#one")
    meta = scan_providers(artifacts)["alpha"]
    _fetch_and_write_provider("alpha", meta, None)
    before = (artifacts / "alpha.yaml").read_bytes()
    assert b"secret" in before
    (secrets / "alpha.source").write_text("proxies: []")
    with pytest.raises(ValueError):
        _fetch_and_write_provider("alpha", meta, None)
    assert (artifacts / "alpha.yaml").read_bytes() == before
    assert (artifacts / "alpha.yaml.lkg").read_bytes() == before
    (secrets / "alpha.source").write_text("trojan://other@two.example:443#two")
    _fetch_and_write_provider("alpha", meta, None)
    assert "two.example" in (artifacts / "alpha.yaml").read_text()
    assert "one.example" not in (artifacts / "alpha.yaml").read_text()


def test_via_uses_isolated_slot_and_releases_it(tmp_path, monkeypatch):
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    from egressd.main import _fetch_and_write_provider
    from egressd import provider_fetch

    calls = []
    slot = SimpleNamespace(group="os_3", user="slot-user", password="slot-pass")

    class Allocator:
        def allocate(self, timeout):
            calls.append("allocate")
            return slot

        def release(self, value):
            assert value is slot
            calls.append("release")

    class Mihomo:
        def put(self, path, body):
            calls.append((path, body))
            return 204, {}

        def get(self, path):
            return 200, {"now": "other-node"}

    class Catalog:
        def exists(self, route):
            return route == "other:node"

        def mihomo_name(self, route):
            return "other-node"

    def fake_fetch(url, host, port, user, password):
        calls.append((host, port, user, password))
        return b"trojan://secret@proxy.example:443#node"

    monkeypatch.setattr(provider_fetch, "socks_fetch", fake_fetch)
    app = SimpleNamespace(state=SimpleNamespace(
        catalog=Catalog(), allocator=Allocator(), mihomo=Mihomo(),
        probe_host="mihomo", oneshot_proxy_port=18081))
    meta = {"source": {"type": "url", "url": "https://example.com/sub?token=hidden"},
            "path": str(tmp_path / "output.yaml")}
    _fetch_and_write_provider("mine", meta, "other:node", app)
    assert calls == ["allocate", ("/proxies/os_3", {"name": "other-node"}),
                     ("mihomo", 18081, "slot-user", "slot-pass"), "release"]
    assert (tmp_path / "output.yaml").is_file()


def test_api_status_and_failure_hide_subscription_url(tmp_path, monkeypatch, capsys):
    from fastapi.testclient import TestClient
    from egressd import provider_fetch

    clients = tmp_path / "clients"
    sources = tmp_path / "sources"
    artifacts = tmp_path / "artifacts"
    for directory in (clients, sources, artifacts):
        directory.mkdir()
    (clients / "host-main.token").write_text("test-token")
    (clients / "host-main.profile.json").write_text(json.dumps({"client_id": "host-main"}))
    secret_url = "https://example.com/sub?token=HIDDEN-CREDENTIAL"
    (sources / "alpha.json").write_text(json.dumps({"type": "url", "url": secret_url}))
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDER_SOURCES_DIR", str(sources))
    monkeypatch.setenv("EGRESSD_PROVIDER_STATUS_DIR", str(tmp_path / "status"))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(artifacts))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    from egressd.main import create_app

    client = TestClient(create_app())
    headers = {"Authorization": "Bearer test-token"}
    monkeypatch.setattr(provider_fetch, "direct_fetch",
                        lambda url: b"trojan://password@proxy.example:443#node")
    updated = client.post("/v1/providers/alpha/update", headers=headers)
    assert updated.status_code == 200
    inventory = client.get("/v1/providers", headers=headers).json()
    assert inventory["providers"][0]["node_count"] == 1
    assert inventory["providers"][0]["last_success"] is not None

    def broken(url):
        raise ValueError("download failed: " + url)

    monkeypatch.setattr(provider_fetch, "direct_fetch", broken)
    failed = client.post("/v1/providers/alpha/update", headers=headers)
    assert failed.status_code == 502
    inventory = client.get("/v1/providers", headers=headers).json()
    assert inventory["providers"][0]["node_count"] == 1
    assert inventory["providers"][0]["last_error"] == "update_failed"
    combined = failed.text + json.dumps(inventory) + capsys.readouterr().out
    assert "HIDDEN-CREDENTIAL" not in combined
    assert "password@proxy.example" not in combined


def test_mihomo_path_points_to_read_only_provider_mount(tmp_path, monkeypatch):
    from egressd.configctl import load_providers

    directory = tmp_path / "providers"
    directory.mkdir()
    (directory / "alpha.yaml").write_text("proxies: []\n")
    monkeypatch.setenv("EGRESSD_MIHO_PROVIDER_DIR", "/root/.config/mihomo/providers")
    provider = load_providers(str(directory))[0]
    assert provider.to_config()["path"] == "/root/.config/mihomo/providers/alpha.yaml"
    compose = (ROOT / "compose.yaml").read_text()
    assert "providers:/root/.config/mihomo/providers:ro" in compose
    assert "providers:/var/lib/egressd/providers" in compose
