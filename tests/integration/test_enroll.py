"""Local UID enrollment never accepts caller-selected identities."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys_path = str(ROOT / "host")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)
from client_identity import default_client_id


def _module():
    path = ROOT / "host" / "egress-enroll"
    loader = importlib.machinery.SourceFileLoader("egress_enroll", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_uid_profiles_are_separate_and_persistent(tmp_path):
    enroll = _module()
    clients = tmp_path / "clients"
    a = enroll.credentials(12345, clients)
    b = enroll.credentials(12346, clients)
    again = enroll.credentials(12345, clients)
    assert a == again
    client_id = default_client_id(12345, "host")
    assert a["client_id"] == client_id
    assert a["token"] != b["token"]
    assert a["proxy_auth"] != b["proxy_auth"]
    assert a["tun_backend"] == "local-userns"
    assert (clients / f"{client_id}.token").stat().st_mode & 0o777 == 0o600
    assert json.loads((clients / f"{client_id}.profile.json").read_text())["environment"] == "host"


def test_socket_uses_peer_uid(tmp_path, monkeypatch):
    import threading
    import time
    enroll = _module()
    monkeypatch.setattr(enroll, "ready", lambda data: True)
    path = tmp_path / "enroll.sock"
    thread = threading.Thread(target=enroll.serve,
                              args=(path, tmp_path / "clients"),
                              daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.01)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(path))
        conn.sendall(b'{"op":"get","uid":0}')
        assert json.loads(conn.recv(4096))["ok"] is False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(path))
        conn.sendall(b'{"op":"get"}')
        result = json.loads(conn.recv(4096))
    assert result["client_id"] == default_client_id(os.getuid(), "host")
