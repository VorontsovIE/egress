"""A6: client-local, per-invocation TUN namespace capability contract."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "egressd" / "src"))


def _write_blanc(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
    ]}, sort_keys=False))


@pytest.fixture()
def egress(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    for cid, env in (("host-main", "host"), ("container-main", "container")):
        (clients / f"{cid}.token").write_text(f"tok-{cid}\n")
        (clients / f"{cid}.profile.json").write_text(json.dumps(
            {"client_id": cid, "environment": env, "modes": ["proxy", "tun"]}))
        (clients / f"{cid}.proxy-creds").write_text(f"{cid}-user:{cid}-pass\n")
    provs = tmp_path / "providers"
    provs.mkdir()
    _write_blanc(provs / "blanc.yaml")
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(provs))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_METRICS_DB", str(tmp_path / "state" / "metrics.sqlite"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    monkeypatch.setenv("EGRESSD_ONESHOT_SLOTS", "4")
    from egressd.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    return TestClient(app), {"host": {"Authorization": "Bearer tok-host-main"},
                             "container": {"Authorization": "Bearer tok-container-main"}}


@pytest.mark.parametrize("client_id", ["host", "container"])
def test_capabilities_advertise_same_local_tun_backend(egress, client_id):
    client, auth = egress
    response = client.get("/v1/capabilities", headers=auth[client_id])
    assert response.status_code == 200
    data = response.json()
    assert data["modes"] == ["proxy", "tun"]
    assert data["tun"] == {"available": True, "backend": "local-userns", "status": "supported"}
    assert client.put("/v1/default/mode", headers=auth[client_id],
                      json={"mode": "tun"}).status_code == 200


def test_client_runtime_has_no_privileged_helper_or_shared_socket():
    files = [ROOT / "client/src/vibelab_egress_client/tun_runtime.py",
             ROOT / "client/src/vibelab_egress_client/tun_bootstrap.py"]
    source = "\n".join(p.read_text() for p in files)
    assert "SCM_RIGHTS" in source
    assert "--user" in source and "--net" in source
    assert "tun-helper.sock" not in source
    assert "host-helper" not in source
