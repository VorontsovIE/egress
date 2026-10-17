"""The two containers see the shared generated config at different paths."""

from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "egressd" / "src"))

from egressd.configctl import ConfigController  # noqa: E402


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
    egressd = compose["services"]["vibelab-egress-egressd"]
    mihomo = compose["services"]["vibelab-egress-mihomo"]
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
