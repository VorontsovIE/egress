from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "client_identity", ROOT / "host" / "client_identity.py")
CLIENT_IDENTITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT_IDENTITY)


def test_default_id_uses_username_for_host_and_container(monkeypatch):
    monkeypatch.setattr(CLIENT_IDENTITY.pwd, "getpwuid",
                        lambda _uid: SimpleNamespace(pw_name="egress"))
    assert CLIENT_IDENTITY.default_client_id(1000, "host") == "host-egress"
    assert CLIENT_IDENTITY.default_client_id(1000, "container") == "container-egress"


def test_default_id_falls_back_for_unavailable_or_unsafe_username(monkeypatch):
    def no_user(_uid):
        raise KeyError
    monkeypatch.setattr(CLIENT_IDENTITY.pwd, "getpwuid", no_user)
    assert CLIENT_IDENTITY.default_client_id(1000, "host") == "host-uid-1000"
    monkeypatch.setattr(CLIENT_IDENTITY.pwd, "getpwuid",
                        lambda _uid: SimpleNamespace(pw_name="bad/name"))
    assert CLIENT_IDENTITY.default_client_id(1000, "container") == "container-uid-1000"


def test_default_id_falls_back_when_username_exceeds_client_id_limit(monkeypatch):
    monkeypatch.setattr(CLIENT_IDENTITY.pwd, "getpwuid",
                        lambda _uid: SimpleNamespace(pw_name="u" * 60))
    assert CLIENT_IDENTITY.default_client_id(1000, "host") == "host-uid-1000"
