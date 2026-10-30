"""The two containers see the shared generated config at different paths."""

from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "egressd" / "src"))

from egressd.configctl import ConfigController, build_egress_config  # noqa: E402


class MihomoStub:
    def __init__(self):
        self.path = None

    def put(self, endpoint, data):
        assert endpoint == "/configs"
        self.path = data["path"]
        return 204, None

    def get(self, endpoint, timeout):
        assert endpoint == "/version"
        return 200, {}


def test_hot_reload_uses_mihomo_visible_config_path():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    egressd = compose["services"]["egress-egressd"]
    mihomo = compose["services"]["egress-mihomo"]
    env = egressd["environment"]

    assert env["EGRESSD_CONFIG_OUT"] != env["EGRESSD_MIHO_CONFIG_PATH"]
    assert env["EGRESSD_MIHO_CONFIG_PATH"] == mihomo["command"][-1]
    assert any(volume.endswith(":/var/lib/egressd/generated")
               for volume in egressd["volumes"])
    assert any(volume.endswith(":/root/.config/mihomo/generated:ro")
               for volume in mihomo["volumes"])

    stub = MihomoStub()
    ctl = ConfigController(None, env["EGRESSD_CONFIG_OUT"], stub,
                           env["EGRESSD_MIHO_CONFIG_PATH"])
    assert ctl.hot_reload() == (True, "reloaded")
    assert stub.path == env["EGRESSD_MIHO_CONFIG_PATH"]


def test_runtime_config_binds_inside_mihomo(monkeypatch, tmp_path):
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    env = compose["services"]["egress-egressd"]["environment"]
    clients = tmp_path / "clients"
    clients.mkdir()
    (clients / "host-main.token").write_text("fake-token\n")
    (clients / "host-main.proxy-creds").write_text("fake-user:fake-password\n")
    secret = tmp_path / "secret"
    secret.write_text("fake-secret\n")
    monkeypatch.setenv("MIHOMO_SECRET_FILE", str(secret))
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("EGRESSD_ONESHOT_SLOTS", "0")
    for key in ("MIHOMO_API_URL", "EGRESSD_MIHO_CONTROLLER_LISTEN",
                "EGRESSD_LISTENER_LISTEN"):
        monkeypatch.setenv(key, env[key])

    cfg = build_egress_config().generate()
    assert cfg["external-controller"] == "0.0.0.0:9090"
    assert {listener["listen"] for listener in cfg["listeners"]} == {"0.0.0.0"}
    assert cfg["external-controller"] != env["MIHOMO_API_URL"].removeprefix("http://")
