"""Host tooling copies personal route snapshots only into an explicit client."""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "egress_hostctl_test_module", str(ROOT / "host" / "egress-hostctl"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
HOSTCTL = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = HOSTCTL
LOADER.exec_module(HOSTCTL)


def _fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(HOSTCTL.os, "geteuid", lambda: 0)
    root = tmp_path / "state"
    providers = root / "providers"
    personal = root / "egressd" / "personal"
    clients = tmp_path / "secrets" / "clients"
    providers.mkdir(parents=True)
    personal.mkdir(parents=True)
    clients.mkdir(parents=True)

    owner = "host-owner"
    target = "egress-test-client"
    alias = "blanc"
    source_provider = f"p-{hashlib.sha256(owner.encode()).hexdigest()[:24]}-{alias}"
    source_route = f"{source_provider}:vless-49"
    yaml_text = ("proxies:\n- name: vless-49\n  type: vless\n"
                 "  server: 203.0.113.10\n  port: 443\n"
                 "  uuid: fake-private-node-credential\n")
    (providers / f"{source_provider}.yaml").write_text(yaml_text)
    (personal / f"{source_provider}.json").write_text(json.dumps({
        "provider_id": source_provider,
        "client_id": owner,
        "alias": alias,
        "kind": "url",
    }))
    (clients / f"{owner}.token").write_text("fake-owner-token")
    (clients / f"{target}.token").write_text("fake-test-token")
    (clients / f"{target}.profile.json").write_text(json.dumps({
        "client_id": target, "environment": "container", "modes": ["proxy", "tun"]
    }))
    monkeypatch.setattr(HOSTCTL, "root_dir", lambda: str(root))
    monkeypatch.setenv("VIBELAB_EGRESS_SECRETS", str(tmp_path / "secrets"))
    return root, clients, owner, target, source_provider, source_route, yaml_text


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class _FakeOpener:
    def __init__(self, open_fn):
        self.open_fn = open_fn

    def open(self, request, timeout):
        return self.open_fn(request, timeout)


def test_personal_api_redirects_are_rejected():
    handler = HOSTCTL._RejectRedirects()
    assert handler.redirect_request(None, None, 307, "Temporary Redirect", {},
                                    "https://example.invalid/collect") is None
    assert HOSTCTL.ProxyHandler({}).proxies == {}
    assert not any(isinstance(h, HOSTCTL.ProxyHandler)
                   for h in HOSTCTL._PERSONAL_API_OPENER.handlers)


def test_copy_personal_snapshot_creates_target_owned_route_without_logging_yaml(
        tmp_path, monkeypatch, capsys):
    (_root, _clients, _owner, target, source_provider, source_route,
     yaml_text) = _fixture(tmp_path, monkeypatch)
    calls = []
    target_alias = "test-blanc-" + hashlib.sha256(source_provider.encode()).hexdigest()[:8]
    target_provider = f"p-{target}-{target_alias}"
    target_route = f"{target_provider}:vless-49"

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        path = urlsplit(request.full_url).path
        if request.method == "GET" and path.endswith("/" + source_route.replace(":", "%3A")):
            return _Response({"id": source_route})
        if request.method == "PUT" and path.endswith("/snapshot"):
            payload = json.loads(request.data)
            assert payload == {"yaml": yaml_text}
            return _Response({"provider_id": target_provider, "node_count": 1})
        if request.method == "GET" and unquote(path).endswith("/" + target_route):
            return _Response({"id": target_route})
        raise AssertionError(f"unexpected snapshot API request: {request.method} {path}")

    monkeypatch.setattr(HOSTCTL, "_PERSONAL_API_OPENER", _FakeOpener(fake_urlopen))
    args = argparse.Namespace(source_route=source_route, target_client=target)
    assert HOSTCTL.cmd_copy_personal_source(args) == 0

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["target_route"] == target_route
    assert result["node_count"] == 1
    assert "fake-private-node-credential" not in output
    assert len(calls) == 3
    assert calls[0][0].get_header("Authorization") == "Bearer fake-owner-token"
    assert calls[1][0].get_header("Authorization") == "Bearer fake-test-token"
    assert calls[2][0].get_header("Authorization") == "Bearer fake-test-token"
    assert all(timeout == 15 for _, timeout in calls)


def test_copy_personal_snapshot_rejects_non_personal_route(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch)
    args = argparse.Namespace(source_route="blanc:vless-49",
                              target_client="egress-test-client")
    with pytest.raises(SystemExit):
        HOSTCTL.cmd_copy_personal_source(args)


def test_copy_personal_snapshot_resolves_alias_for_explicit_host_profile(
        tmp_path, monkeypatch, capsys):
    (_root, clients, owner, target, source_provider, _source_route,
     _yaml) = _fixture(tmp_path, monkeypatch)
    (clients / f"{owner}.profile.json").write_text(json.dumps({
        "client_id": owner, "environment": "host", "modes": ["proxy", "tun"]
    }))
    calls = []
    target_alias = "test-blanc-" + hashlib.sha256(source_provider.encode()).hexdigest()[:8]
    target_provider = f"p-{target}-{target_alias}"

    def fake_urlopen(request, timeout):
        calls.append(request)
        path = unquote(urlsplit(request.full_url).path)
        if request.method == "GET" and path.endswith("/" + source_provider + ":vless-49"):
            return _Response({"id": source_provider + ":vless-49"})
        if request.method == "PUT" and path.endswith("/snapshot"):
            return _Response({"provider_id": target_provider, "node_count": 1})
        if request.method == "GET" and path.endswith("/" + target_provider + ":vless-49"):
            return _Response({"id": target_provider + ":vless-49"})
        raise AssertionError(f"unexpected API request: {request.method} {path}")

    monkeypatch.setattr(HOSTCTL, "_PERSONAL_API_OPENER", _FakeOpener(fake_urlopen))
    args = argparse.Namespace(source_route="@blanc:vless-49", source_client=owner,
                              target_client=target)
    assert HOSTCTL.cmd_copy_personal_source(args) == 0
    assert len(calls) == 3
    assert calls[0].get_header("Authorization") == "Bearer fake-owner-token"
    assert json.loads(capsys.readouterr().out)["source_provider"] == source_provider


def test_copy_personal_snapshot_refuses_overwriting_non_snapshot_source(
        tmp_path, monkeypatch):
    root, clients, _owner, target, source_provider, source_route, _yaml = \
        _fixture(tmp_path, monkeypatch)
    target_alias = "test-blanc-" + hashlib.sha256(source_provider.encode()).hexdigest()[:8]
    target_provider = f"p-{target}-{target_alias}"
    personal = root / "egressd" / "personal"
    (personal / f"{target_provider}.json").write_text(json.dumps({
        "provider_id": target_provider, "client_id": target,
        "alias": target_alias, "kind": "url",
    }))
    monkeypatch.setattr(
        HOSTCTL, "_PERSONAL_API_OPENER",
        _FakeOpener(lambda *_args, **_kwargs: pytest.fail("API called")))
    args = argparse.Namespace(source_route=source_route, target_client=target)
    with pytest.raises(SystemExit):
        HOSTCTL.cmd_copy_personal_source(args)
