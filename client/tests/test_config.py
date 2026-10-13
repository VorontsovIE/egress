"""A4 — client config discovery/validation unit tests (spec section 29).

Run:  python -m pytest client/tests/test_config.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))

from vibelab_egress_client.config import (  # noqa: E402
    ENV_CONFIG,
    ClientConfig,
    ConfigError,
    load_config,
    parse_client_yaml,
)


def _write_config(path: Path, **overrides) -> Path:
    data = {
        "client_id": "openscience-main",
        "control_url": "http://127.0.0.1:9091",
        "proxy_host": "127.0.0.1",
        "default_proxy_port": "1080",
        "oneshot_proxy_port": "1081",
        "api_token_file": str(path.parent / "client.token"),
        "default_proxy_auth_file": str(path.parent / "default-proxy.auth"),
        "environment": "gvisor-container",
        "tun_backend": "none",
    }
    data.update(overrides)
    path.write_text("\n".join(f"{k}: {v}" for k, v in data.items()) + "\n")
    return path


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path / "client.yaml")
    (tmp_path / "client.token").write_text("tok-abc\n")
    (tmp_path / "default-proxy.auth").write_text("os-user:os-pass\n")
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
class TestParse:
    def test_flat_yaml(self):
        d = parse_client_yaml("client_id: host-main\ncontrol_url: http://x:1\n")
        assert d["client_id"] == "host-main"

    def test_int_coercion(self):
        d = parse_client_yaml("default_proxy_port: 1080\n")
        assert d["default_proxy_port"] == 1080

    def test_quotes(self):
        d = parse_client_yaml('control_url: "http://x:1"\n')
        assert d["control_url"] == "http://x:1"

    def test_comments_and_blank(self):
        d = parse_client_yaml("# comment\n\nclient_id: a\n")
        assert d == {"client_id": "a"}

    def test_inline_comment(self):
        d = parse_client_yaml("client_id: a  # trailing\n")
        assert d["client_id"] == "a"

    def test_bad_key_rejected(self):
        with pytest.raises(ConfigError):
            parse_client_yaml("bad key: v\n")

    def test_non_kv_line_rejected(self):
        with pytest.raises(ConfigError):
            parse_client_yaml("just a string\n")


# ---------------------------------------------------------------------------
# discovery order (spec 29): --config > env > /etc
# ---------------------------------------------------------------------------
class TestDiscovery:
    def test_explicit_path_wins(self, env, monkeypatch):
        other = _write_config(env / "other.yaml", client_id="host-main")
        monkeypatch.setenv(ENV_CONFIG, str(env / "client.yaml"))
        cfg = load_config(str(other))
        assert cfg.client_id == "host-main"

    def test_env_over_etc(self, env, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG, str(env / "client.yaml"))
        cfg = load_config()
        assert cfg.client_id == "openscience-main"

    def test_missing_config(self, env, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG, str(env / "nope.yaml"))
        with pytest.raises(ConfigError, match="not found"):
            load_config()


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
class TestValidation:
    def test_missing_key(self, env):
        p = env / "bad.yaml"
        p.write_text("client_id: a\n")
        with pytest.raises(ConfigError, match="missing keys"):
            load_config(str(p))

    def test_bad_environment(self, env):
        p = _write_config(env / "bad.yaml", environment="spaceship")
        with pytest.raises(ConfigError, match="environment"):
            load_config(str(p))

    def test_port_out_of_range(self, env):
        p = _write_config(env / "bad.yaml", default_proxy_port="70000")
        with pytest.raises(ConfigError, match="out of range"):
            load_config(str(p))

    def test_bad_control_url(self, env):
        p = _write_config(env / "bad.yaml", control_url="ftp://x")
        with pytest.raises(ConfigError, match="http"):
            load_config(str(p))

    def test_bad_client_id(self, env):
        p = _write_config(env / "bad.yaml", client_id="has space")
        with pytest.raises(ConfigError, match="client_id"):
            load_config(str(p))

    def test_bad_tun_backend(self, env):
        p = _write_config(env / "bad.yaml", tun_backend="magic")
        with pytest.raises(ConfigError, match="tun_backend"):
            load_config(str(p))

    def test_valid_config(self, env):
        cfg = load_config(str(env / "client.yaml"))
        assert cfg.client_id == "openscience-main"
        assert cfg.environment == "gvisor-container"
        assert cfg.tun_backend == "none"
        assert cfg.default_proxy_port == 1080


# ---------------------------------------------------------------------------
# secrets: separate files, never in the yaml (spec 37)
# ---------------------------------------------------------------------------
class TestSecrets:
    def test_token_read(self, env):
        cfg = load_config(str(env / "client.yaml"))
        assert cfg.api_token() == "tok-abc"

    def test_proxy_auth_parsed(self, env):
        cfg = load_config(str(env / "client.yaml"))
        assert cfg.default_proxy_auth() == ("os-user", "os-pass")

    def test_auth_without_colon(self, env):
        (env / "default-proxy.auth").write_text("nopassword\n")
        cfg = load_config(str(env / "client.yaml"))
        with pytest.raises(ConfigError, match="user:password"):
            cfg.default_proxy_auth()

    def test_missing_token_file(self, env):
        (env / "client.token").unlink()
        cfg = load_config(str(env / "client.yaml"))
        with pytest.raises(ConfigError, match="not found"):
            cfg.api_token()

    def test_empty_token_file(self, env):
        (env / "client.token").write_text("   \n")
        cfg = load_config(str(env / "client.yaml"))
        with pytest.raises(ConfigError, match="empty"):
            cfg.api_token()

    def test_trailing_newline_stripped(self, env):
        (env / "client.token").write_text("tok-x\n\n")
        cfg = load_config(str(env / "client.yaml"))
        assert cfg.api_token() == "tok-x"
