"""A4 — egressctl CLI unit tests (spec section 30.1).

The API layer is stubbed: these tests verify argument parsing, exit codes,
output formats (human + ``--json``), secret masking and the identity-mismatch
guard. Live API behaviour is covered by tests/integration/test_a4.py.

Run:  python -m pytest client/tests/test_egressctl.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))

from vibelab_egress_client import egressctl  # noqa: E402
from vibelab_egress_client.api import ApiError  # noqa: E402
from vibelab_egress_client.config import ConfigError  # noqa: E402


@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    token = tmp_path / "client.token"
    token.write_text("tok-abc\n")
    auth = tmp_path / "default-proxy.auth"
    auth.write_text("os-user:os-pass\n")
    cfg = tmp_path / "client.yaml"
    cfg.write_text(
        "client_id: openscience-main\n"
        "control_url: http://127.0.0.1:9091\n"
        "proxy_host: 127.0.0.1\n"
        "default_proxy_port: 1080\n"
        "oneshot_proxy_port: 1081\n"
        f"api_token_file: {token}\n"
        f"default_proxy_auth_file: {auth}\n"
        "environment: gvisor-container\n"
        "tun_backend: none\n"
    )
    return str(cfg)


def run_cli(monkeypatch, cfg_file, *argv, api_stub=None):
    """Run egressctl.main, capturing SystemExit + stdout/stderr."""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    code = 0
    if api_stub is not None:
        monkeypatch.setattr(egressctl, "EgressApi",
                            lambda cfg, **kw: api_stub)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            egressctl.main(["--config", cfg_file, *argv])
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    return code, out.getvalue(), err.getvalue()


class TestStatus:
    def test_status_human(self, monkeypatch, cfg_file):
        stub = type("S", (), {"status": lambda self: {
            "client_id": "openscience-main",
            "mihomo": {"reachable": True, "version": "v1.19.7"}}})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "status", api_stub=stub)
        assert code == 0
        assert "client=openscience-main" in out
        assert "mihomo=up" in out and "v1.19.7" in out

    def test_status_json(self, monkeypatch, cfg_file):
        payload = {"client_id": "openscience-main",
                   "mihomo": {"reachable": False, "version": None}}
        stub = type("S", (), {"status": lambda self: payload})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "--json", "status",
                               api_stub=stub)
        assert code == 0
        assert json.loads(out) == payload

    def test_status_api_error(self, monkeypatch, cfg_file):
        def boom(self):
            raise ApiError(502, "backend down")
        stub = type("S", (), {"status": boom})()
        code, _, err = run_cli(monkeypatch, cfg_file, "status", api_stub=stub)
        assert code == egressctl.EXIT_RUNTIME
        assert "backend down" in err

    def test_status_auth_error(self, monkeypatch, cfg_file):
        def boom(self):
            raise ApiError(401, "unknown token")
        stub = type("S", (), {"status": boom})()
        code, _, err = run_cli(monkeypatch, cfg_file, "status", api_stub=stub)
        assert code == egressctl.EXIT_AUTH


class TestWhoami:
    def test_identity_ok(self, monkeypatch, cfg_file):
        stub = type("S", (), {"self_": lambda self: {
            "client_id": "openscience-main", "environment": "gvisor-container",
            "modes": ["proxy"]}})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "whoami", api_stub=stub)
        assert code == 0
        assert "client_id=openscience-main" in out

    def test_identity_mismatch(self, monkeypatch, cfg_file):
        stub = type("S", (), {"self_": lambda self: {
            "client_id": "host-main", "environment": "host",
            "modes": ["proxy"]}})()
        code, _, err = run_cli(monkeypatch, cfg_file, "whoami", api_stub=stub)
        assert code == egressctl.EXIT_IDENTITY
        assert "identity mismatch" in err


class TestRouteMode:
    def test_route_current(self, monkeypatch, cfg_file):
        stub = type("S", (), {"get_default": lambda self: {"route": "direct"}})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "route", "current",
                               api_stub=stub)
        assert code == 0 and "default route: direct" in out

    def test_route_set(self, monkeypatch, cfg_file):
        seen = {}
        stub = type("S", (), {"set_default": lambda self, r:
                              seen.setdefault("route", r) or {"route": r}})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "route", "set",
                               "blanc:FI-Helsinki-1", api_stub=stub)
        assert code == 0
        assert seen["route"] == "blanc:FI-Helsinki-1"
        assert "default route set" in out

    def test_mode_set_choices(self, monkeypatch, cfg_file):
        # 'tun' is a valid choice for the CLI (the server decides support)
        stub = type("S", (), {"set_default_mode": lambda self, m: {"mode": m}})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "mode", "set", "tun",
                               api_stub=stub)
        assert code == 0 and "tun" in out

    def test_mode_set_bad_choice(self, monkeypatch, cfg_file):
        code, _, err = run_cli(monkeypatch, cfg_file, "mode", "set", "carrier-pigeon")
        assert code == egressctl.EXIT_USAGE


class TestList:
    def test_list_filters(self, monkeypatch, cfg_file):
        seen = {}

        def list_routes(self, **f):
            seen.update(f)
            return {"routes": [{"id": "blanc:FI-Helsinki-1",
                                "provider": "blanc", "country": "FI",
                                "latency_ms": 42, "healthy": True}]}
        stub = type("S", (), {"list_routes": list_routes})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "list", "--country", "FI",
                               "--provider", "blanc", "--healthy", api_stub=stub)
        assert code == 0
        assert seen == {"country": "FI", "provider": "blanc", "healthy": "true"}
        assert "blanc:FI-Helsinki-1" in out and "42ms" in out

    def test_list_json(self, monkeypatch, cfg_file):
        payload = {"routes": [{"id": "direct"}]}
        stub = type("S", (), {"list_routes": lambda self, **f: payload})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "--json", "list",
                               api_stub=stub)
        assert code == 0
        assert json.loads(out) == payload


class TestUpdate:
    def test_update_args(self, monkeypatch, cfg_file):
        seen = {}

        def update_provider(self, provider_id=None, via=None, if_older_than=None):
            seen.update(provider=provider_id, via=via, if_older_than=if_older_than)
            return {"updated": True}
        stub = type("S", (), {"update_provider": update_provider})()
        code, out, _ = run_cli(monkeypatch, cfg_file, "update",
                               "--provider", "blanc", "--via", "direct",
                               "--if-older-than", "6h", api_stub=stub)
        assert code == 0
        assert seen == {"provider": "blanc", "via": "direct",
                        "if_older_than": "6h"}


class TestConfig:
    def test_config_show_masks_secrets(self, monkeypatch, cfg_file):
        code, out, _ = run_cli(monkeypatch, cfg_file, "config", "show")
        assert code == 0
        assert "client_id: openscience-main" in out
        # secrets must NOT be printed by default (spec 29)
        assert "tok-abc" not in out
        assert "os-pass" not in out

    def test_config_show_secrets_flag(self, monkeypatch, cfg_file):
        code, out, _ = run_cli(monkeypatch, cfg_file, "config", "show",
                               "--secrets")
        assert code == 0
        assert "tok-abc" in out
        assert "os-user:os-pass" in out

    def test_config_validate_ok(self, monkeypatch, cfg_file):
        code, out, _ = run_cli(monkeypatch, cfg_file, "config", "validate")
        assert code == 0 and "valid" in out

    def test_config_validate_bad_secret(self, monkeypatch, cfg_file):
        # break the auth file
        import pathlib
        p = pathlib.Path(cfg_file).parent / "default-proxy.auth"
        p.write_text("no-colon-here\n")
        code, _, err = run_cli(monkeypatch, cfg_file, "config", "validate")
        assert code == egressctl.EXIT_CONFIG
        assert "user:password" in err

    def test_config_missing(self, monkeypatch, tmp_path):
        code, _, err = run_cli(monkeypatch, str(tmp_path / "nope.yaml"),
                               "config", "show")
        assert code == egressctl.EXIT_CONFIG
        assert "config error" in err


class TestUsage:
    def test_no_command(self, monkeypatch, cfg_file):
        code, _, _ = run_cli(monkeypatch, cfg_file)
        assert code == egressctl.EXIT_USAGE

    def test_unknown_command(self, monkeypatch, cfg_file):
        code, _, _ = run_cli(monkeypatch, cfg_file, "frobnicate")
        assert code == egressctl.EXIT_USAGE

    def test_route_set_requires_arg(self, monkeypatch, cfg_file):
        code, _, _ = run_cli(monkeypatch, cfg_file, "route", "set")
        assert code == egressctl.EXIT_USAGE

class TestCapabilities:
    def _write_host_cfg(self, tmp_path, **ov):
        token = tmp_path / "c.token"
        token.write_text("tok-abc\n")
        auth = tmp_path / "c.auth"
        auth.write_text("os-user:os-pass\n")
        data = {
            "client_id": "host-main",
            "control_url": "http://127.0.0.1:9091",
            "proxy_host": "127.0.0.1",
            "default_proxy_port": "1080",
            "oneshot_proxy_port": "1081",
            "api_token_file": str(token),
            "default_proxy_auth_file": str(auth),
            "environment": "host",
            "tun_backend": "host-helper",
            "tun_helper_socket": str(tmp_path / "tun.sock"),
        }
        data.update(ov)
        p = tmp_path / "host-client.yaml"
        p.write_text("\n".join(f"{k}: {v}" for k, v in data.items()) + "\n")
        return str(p)

    def test_host_helper_reports_supported(self, monkeypatch, tmp_path):
        from vibelab_egress_client import egress_run

        cfg = self._write_host_cfg(tmp_path)
        (tmp_path / "tun.sock").write_text("")
        monkeypatch.setattr(egress_run, "_tun_helper_call", lambda *a, **k: {
            "ok": True, "capability": {"status": "supported", "reason": "ok"}})
        code, out, _ = run_cli(monkeypatch, cfg, "--json", "capabilities")
        assert code == 0
        data = json.loads(out)
        assert data["tun"]["status"] == "supported"
        assert data["tun"]["socket"].endswith("tun.sock")
        assert data["client_version"]
        assert data["environment"] == "host"

    def test_host_socket_present_but_helper_unsupported(self, monkeypatch, tmp_path):
        from vibelab_egress_client import egress_run

        cfg = self._write_host_cfg(tmp_path)
        (tmp_path / "tun.sock").write_text("")
        monkeypatch.setattr(egress_run, "_tun_helper_call", lambda *a, **k: {
            "ok": True, "capability": {
                "status": "unsupported_in_environment",
                "reason": "ip netns add requires CAP_SYS_ADMIN"}})
        code, out, _ = run_cli(monkeypatch, cfg, "--json", "capabilities")
        assert code == 0
        data = json.loads(out)
        assert data["tun"]["status"] == "unsupported_in_environment"
        assert "CAP_SYS_ADMIN" in data["tun"]["reason"]

    def test_stale_socket_does_not_report_supported(self, monkeypatch, tmp_path):
        cfg = self._write_host_cfg(tmp_path)
        (tmp_path / "tun.sock").write_text("")
        code, out, _ = run_cli(monkeypatch, cfg, "--json", "capabilities")
        assert code == 0
        assert json.loads(out)["tun"]["status"] == "helper_not_running"

    def test_host_helper_socket_missing_not_running(self, monkeypatch, tmp_path):
        cfg = self._write_host_cfg(tmp_path)
        code, out, _ = run_cli(monkeypatch, cfg, "--json", "capabilities")
        assert code == 0
        data = json.loads(out)
        assert data["tun"]["status"] == "helper_not_running"

    def test_gvisor_unsupported_in_environment(self, monkeypatch, cfg_file):
        code, out, _ = run_cli(monkeypatch, cfg_file, "--json", "capabilities")
        assert code == 0
        data = json.loads(out)
        assert data["tun"]["status"] == "unsupported_in_environment"

    def test_none_backend_non_gvisor(self, monkeypatch, tmp_path):
        cfg = self._write_host_cfg(tmp_path, tun_backend="none")
        code, out, _ = run_cli(monkeypatch, cfg, "--json", "capabilities")
        assert code == 0
        data = json.loads(out)
        assert data["tun"]["status"] == "none"


def test_container_source_keeps_raw_locally(monkeypatch, cfg_file, tmp_path):
    from vibelab_egress_client import personal_sources
    monkeypatch.setenv("VIBELAB_EGRESS_SOURCE_DIR", str(tmp_path / "sources"))
    monkeypatch.setattr(personal_sources, "normalize", lambda name: "proxies: []\n")
    raw = tmp_path / "subscription.txt"
    raw.write_text("private-source-marker")
    sent = []

    class Stub:
        def upload_personal(self, name, yaml_text):
            sent.append((name, yaml_text))
            return {"alias": name, "node_count": 1, "skipped_nodes": 2}

        def list_personal(self):
            return {"providers": [{"alias": "demo", "node_count": 1,
                                   "skipped_nodes": 2}]}

    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "demo",
                             "--file", str(raw), api_stub=Stub())
    assert code == 0, err
    assert "source added: demo (1 node, 2 skipped)" in out
    assert sent == [("demo", "proxies: []\n")]
    local = tmp_path / "sources" / "demo.json"
    assert json.loads(local.read_text())["source"] == "private-source-marker"
    assert local.stat().st_mode & 0o777 == 0o600
    before = local.stat().st_mtime_ns
    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "demo",
                             "--file", str(raw), api_stub=Stub())
    assert code == 0, err
    assert "source skipped: demo" in out
    assert sent == [("demo", "proxies: []\n")]
    assert local.stat().st_mtime_ns == before


def test_source_add_missing_file_reports_usage_error(monkeypatch, cfg_file, tmp_path):
    missing = tmp_path / "missing-subscription.yaml"
    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "demo",
                             "--file", str(missing))
    assert code == egressctl.EXIT_USAGE
    assert out == ""
    assert "source file error:" in err
    assert str(missing) in err
    assert "Traceback" not in err


def test_source_add_timeout_explains_unknown_outcome(monkeypatch, cfg_file, tmp_path):
    raw = tmp_path / "subscription.txt"
    raw.write_text("proxies: []\n")

    class Stub:
        def upload_personal(self, name, yaml_text):
            raise TimeoutError()

    from vibelab_egress_client import personal_sources
    monkeypatch.setenv("VIBELAB_EGRESS_SOURCE_DIR", str(tmp_path / "sources"))
    monkeypatch.setattr(personal_sources, "normalize", lambda name: raw.read_text())
    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "demo",
                             "--file", str(raw), api_stub=Stub())
    assert code == egressctl.EXIT_RUNTIME
    assert out == ""
    assert "outcome unknown" in err
    assert "source list" in err


def test_container_add_single_uri_without_file(monkeypatch, cfg_file, tmp_path):
    from vibelab_egress_client import personal_sources
    monkeypatch.setenv("VIBELAB_EGRESS_SOURCE_DIR", str(tmp_path / "sources"))
    uploaded = []

    class Stub:
        def upload_personal(self, name, yaml_text):
            uploaded.append((name, yaml_text))
            return {"alias": name, "node_count": 1, "skipped_nodes": 0}

        def list_personal(self):
            return {"providers": [{"alias": "single", "node_count": 1}]}

    uri = "vless://fake-uuid@1.1.1.1:443?security=tls#single"
    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "single",
                             "--uri", uri, api_stub=Stub())
    assert code == 0, err
    assert "source added: single" in out
    assert len(uploaded) == 1
    assert "type: vless" in uploaded[0][1]
    assert personal_sources.load("single") == {"kind": "uri", "source": uri}
    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "single",
                             "--uri", uri, api_stub=Stub())
    assert code == 0, err
    assert "source skipped: single" in out
    assert len(uploaded) == 1

    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(uri + "\n"))
    code, out, err = run_cli(monkeypatch, cfg_file, "source", "add", "fromstdin",
                             "--uri", "-", api_stub=Stub())
    assert code == 0, err
    assert personal_sources.load("fromstdin") == {"kind": "uri", "source": uri}
