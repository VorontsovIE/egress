"""Filesystem-sandboxed behavior checks for the root uninstall helper."""
from __future__ import annotations

import builtins
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
HELPER = ROOT_DIR / "host" / "egress-uninstall"


def load_helper(monkeypatch, tmp_path):
    loader = importlib.machinery.SourceFileLoader("egress_uninstall_test", str(HELPER))
    spec = importlib.util.spec_from_loader("egress_uninstall_test", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    real_path = Path
    roots = {
        "/etc/egress": tmp_path / "etc" / "egress",
        "/etc/systemd/system": tmp_path / "etc" / "systemd" / "system",
        "/usr/local/bin": tmp_path / "usr" / "local" / "bin",
        "/usr/local/lib/egress": tmp_path / "usr" / "local" / "lib" / "egress",
        "/usr/local/sbin": tmp_path / "usr" / "local" / "sbin",
        "/usr/local/libexec/egress": tmp_path / "usr" / "local" / "libexec" / "egress",
        "/usr/sbin": tmp_path / "usr" / "sbin",
        "/usr/share/egress": tmp_path / "usr" / "share" / "egress",
        "/var/lib/egress": tmp_path / "var" / "lib" / "egress",
    }

    def mapped_path(value):
        raw = str(value)
        for prefix, destination in roots.items():
            if raw == prefix or raw.startswith(prefix + "/"):
                return real_path(str(destination) + raw[len(prefix):])
        return real_path(value)

    monkeypatch.setattr(module, "Path", mapped_path)
    module.ETC = mapped_path("/etc/egress")
    module.COMPOSE_ENV = mapped_path("/etc/egress/compose.env")
    module.SECRETS = module.ETC / "secrets"
    module.ROOT = mapped_path("/var/lib/egress")
    module.CLIENT_LIB = mapped_path("/usr/local/lib/egress")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "alice")
    home = tmp_path / "home" / "alice"
    monkeypatch.setattr(module.pwd, "getpwall", lambda: [
        SimpleNamespace(pw_uid=1000, pw_dir=str(home)),
        SimpleNamespace(pw_uid=0, pw_dir=str(tmp_path / "home" / "root")),
    ])
    calls = []
    monkeypatch.setattr(module, "run", lambda argv, check=True: calls.append((argv, check)))
    module._test_roots = roots
    module._test_home = home
    module._test_calls = calls
    return module, roots, home, calls


def touch(path: Path, text="data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def run_helper(module, monkeypatch, *args):
    monkeypatch.setattr(module.sys, "argv", ["egress-uninstall", *args])
    return module.main()


def test_client_only_uninstall_keeps_backend_files_and_state(monkeypatch, tmp_path):
    module, roots, _home, calls = load_helper(monkeypatch, tmp_path)
    etc = roots["/etc/egress"]
    state = roots["/var/lib/egress"]
    touch(etc / "client.yaml")
    touch(etc / "client.token", "client-token")
    touch(etc / "default-proxy.auth", "proxy-auth")
    compose_env = touch(etc / "compose.env", "backend-config")
    secret = touch(etc / "secrets" / "mihomo_api_secret", "api-secret")
    managed = touch(etc / "secrets" / "providers" / "alpha.source", "sub-token")
    personal = touch(state / "egressd" / "personal" / "p-host-alice-work.json", "personal-source")
    provider_yaml = touch(state / "providers" / "alpha.yaml", "provider snapshot")
    metrics = touch(state / "egressd" / "metrics.sqlite", "metrics")
    client_lib = touch(roots["/usr/local/lib/egress"] / "old-client.py")
    assert run_helper(module, monkeypatch, "--client-only") == 0

    assert not (etc / "client.yaml").exists()
    assert not (etc / "client.token").exists()
    assert not client_lib.exists()
    assert compose_env.read_text() == "backend-config"
    assert secret.read_text() == "api-secret"
    assert managed.read_text() == "sub-token"
    assert personal.read_text() == "personal-source"
    assert provider_yaml.read_text() == "provider snapshot"
    assert metrics.read_text() == "metrics"
    assert calls == []


def test_normal_uninstall_retains_subscriptions_and_backend_state(monkeypatch, tmp_path):
    module, roots, home, calls = load_helper(monkeypatch, tmp_path)
    etc = roots["/etc/egress"]
    state = roots["/var/lib/egress"]
    touch(etc / "compose.env", "compose")
    touch(etc / "client.token", "runtime-client-token")
    touch(etc / "secrets" / "mihomo_api_secret", "runtime-api-secret")
    touch(etc / "secrets" / "clients" / "host-alice.token", "issued-token")
    managed_input = touch(etc / "secrets" / "providers" / "managed.source", "provider-subscription")
    managed_meta = touch(etc / "secrets" / "providers" / "managed.json", '{"type":"url"}')
    provider_yaml = touch(state / "providers" / "managed.yaml", "normalized")
    provider_lkg = touch(state / "providers" / "managed.yaml.lkg", "last-known-good")
    host_personal = touch(state / "egressd" / "personal" / "p-host-alice-work.json",
                          '{"source":"https://example.invalid/sub?token=fake"}')
    container_snapshot = touch(state / "egressd" / "personal" / "p-container-main-demo.json",
                               '{"kind":"snapshot"}')
    metrics = touch(state / "egressd" / "metrics.sqlite", "metrics")
    defaults = touch(state / "egressd" / "state" / "defaults.json", '{"clients":{}}')
    container_raw = touch(home / ".local" / "state" / "egress" / "sources" / "demo.json",
                          '{"kind":"url","source":"https://example.invalid/fake"}')
    touch(state / "client-bundles" / "host-alice" / "client.token", "issued-token")
    compose_file = touch(roots["/usr/share/egress"] / "compose.yaml", "services: {}")

    assert run_helper(module, monkeypatch) == 0

    for path in (managed_input, managed_meta, provider_yaml, provider_lkg,
                 host_personal, container_snapshot, metrics, defaults, container_raw):
        assert path.is_file(), path
    assert not compose_file.exists()
    assert not (etc / "client.token").exists()
    assert not (etc / "secrets" / "mihomo_api_secret").exists()
    assert not (etc / "secrets" / "clients").exists()
    assert not (state / "client-bundles").exists()
    assert calls and any("compose" in argv for argv, _check in calls)


def test_purge_cancelled_preserves_all_subscription_data(monkeypatch, tmp_path):
    module, roots, home, calls = load_helper(monkeypatch, tmp_path)
    managed = touch(roots["/etc/egress"] / "secrets" / "providers" / "managed.source", "sub")
    personal = touch(roots["/var/lib/egress"] / "egressd" / "personal" / "p-host-a-x.json", "source")
    snapshot = touch(roots["/var/lib/egress"] / "providers" / "managed.yaml", "yaml")
    container_raw = touch(home / ".local" / "state" / "egress" / "sources" / "demo.json", "raw")

    class TTYInput:
        def isatty(self):
            return True

    monkeypatch.setattr(module.sys, "stdin", TTYInput())
    monkeypatch.setattr(builtins, "input", lambda _prompt: "NO")
    result = run_helper(module, monkeypatch, "--purge")

    assert result == 1
    assert all(path.is_file() for path in (managed, personal, snapshot, container_raw))
    assert calls == []


def test_confirmed_purge_deletes_provider_data_and_state(monkeypatch, tmp_path):
    module, roots, home, _calls = load_helper(monkeypatch, tmp_path)
    other_home = tmp_path / "home" / "bob"
    monkeypatch.setattr(module.pwd, "getpwall", lambda: [
        SimpleNamespace(pw_uid=1000, pw_dir=str(home)),
        SimpleNamespace(pw_uid=1001, pw_dir=str(other_home)),
        SimpleNamespace(pw_uid=0, pw_dir=str(tmp_path / "home" / "root")),
    ])
    managed = touch(roots["/etc/egress"] / "secrets" / "providers" / "managed.source", "sub")
    personal = touch(roots["/var/lib/egress"] / "egressd" / "personal" / "p-host-a-x.json", "source")
    snapshot = touch(roots["/var/lib/egress"] / "providers" / "managed.yaml", "yaml")
    container_raw = touch(home / ".local" / "state" / "egress" / "sources" / "demo.json", "raw")
    other_raw = touch(other_home / ".local" / "state" / "egress" / "sources" / "other.json", "raw")

    class TTYInput:
        def isatty(self):
            return True

    monkeypatch.setattr(module.sys, "stdin", TTYInput())
    monkeypatch.setattr(builtins, "input", lambda _prompt: "PURGE")
    assert run_helper(module, monkeypatch, "--purge") == 0

    assert not managed.exists()
    assert not personal.exists()
    assert not snapshot.exists()
    assert not container_raw.exists()
    assert not other_raw.exists()
    assert not roots["/var/lib/egress"].exists()


def test_purge_requires_tty_and_makes_no_changes(monkeypatch, tmp_path):
    module, roots, _home, calls = load_helper(monkeypatch, tmp_path)
    provider = touch(roots["/etc/egress"] / "secrets" / "providers" / "p.source", "sub")

    class NonTTYInput:
        def isatty(self):
            return False

    monkeypatch.setattr(module.sys, "stdin", NonTTYInput())
    result = run_helper(module, monkeypatch, "--purge")

    assert result == 3
    assert provider.is_file()
    assert calls == []


def test_unprivileged_upgrade_prints_both_supported_choices(monkeypatch, capsys):
    sys.path.insert(0, str(ROOT_DIR / "client" / "src"))
    from egress_client import egressctl

    monkeypatch.setattr(egressctl.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(egressctl.subprocess, "run",
                        lambda *_args, **_kwargs: pytest.fail("must not launch updater"))
    args = egressctl.build_parser().parse_args(["upgrade"])
    with pytest.raises(SystemExit) as error:
        egressctl.cmd_upgrade(args)
    assert error.value.code == 3
    output = capsys.readouterr().err
    assert "sudo egressctl upgrade" in output
    assert "egressctl upgrade --client-only" in output
