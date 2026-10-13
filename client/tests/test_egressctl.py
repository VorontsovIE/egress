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
                                "delay_ms": 42, "healthy": True}]}
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
