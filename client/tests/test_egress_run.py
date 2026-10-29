"""A4 — egress-run unit tests (spec section 30.2).

Covers the contract:
  * argv passed as a list (no shell re-parse);
  * exit status preserved (including 128+signum for signal death);
  * SIGINT/SIGTERM proxied to the child;
  * one-shot session closed on exit (finally);
  * explicit route/mode never changes the persistent default;
  * ``--default`` uses the stored mode unless the invocation overrides it;
  * ``direct`` uses no proxy at all;
  * ``--mode tun`` -> explicit unsupported (no silent fallback);
  * config/token discovery shared with egressctl.

Run:  python -m pytest client/tests/test_egress_run.py -v
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))

from vibelab_egress_client import egress_run  # noqa: E402
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
        "client_id: container-main\n"
        "control_url: http://127.0.0.1:9091\n"
        "proxy_host: 127.0.0.1\n"
        "default_proxy_port: 1080\n"
        "oneshot_proxy_port: 1081\n"
        f"api_token_file: {token}\n"
        f"default_proxy_auth_file: {auth}\n"
        "environment: container\n"
        "tun_backend: local-userns\n"
    )
    return str(cfg)


def run_egress_run(monkeypatch, cfg_file, argv, api_stub=None):
    out = []
    code = 0
    if api_stub is not None:
        monkeypatch.setattr(egress_run, "EgressApi",
                            lambda cfg, **kw: api_stub)
    try:
        egress_run.main(["--config", cfg_file, *argv])
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    return code


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
class TestParseArgs:
    def test_default_flag(self):
        opts, cmd = egress_run.parse_args(["--default", "--", "true"])
        assert opts["default"] is True and opts["route"] is None
        assert cmd == ["true"]

    def test_explicit_route(self):
        opts, cmd = egress_run.parse_args(["blanc:FI-Helsinki-1", "--", "true"])
        assert opts["route"] == "blanc:FI-Helsinki-1"

    def test_direct(self):
        opts, _ = egress_run.parse_args(["direct", "--", "true"])
        assert opts["route"] == "direct"

    def test_mode_proxy(self):
        opts, _ = egress_run.parse_args(["--mode", "proxy", "direct", "--", "true"])
        assert opts["mode"] == "proxy"

    def test_mode_tun(self):
        opts, _ = egress_run.parse_args(["--mode", "tun", "direct", "--", "true"])
        assert opts["mode"] == "tun"

    def test_mode_bad_value(self):
        with pytest.raises(SystemExit) as ei:
            egress_run.parse_args(["--mode", "carrier", "--", "true"])
        assert ei.value.code == egress_run.EXIT_USAGE

    def test_mode_missing_value(self):
        with pytest.raises(SystemExit) as ei:
            egress_run.parse_args(["--mode", "--", "true"])
        assert ei.value.code == egress_run.EXIT_USAGE

    def test_two_routes_rejected(self):
        with pytest.raises(SystemExit) as ei:
            egress_run.parse_args(["a:b", "c:d", "--", "true"])
        assert ei.value.code == egress_run.EXIT_USAGE

    def test_route_and_default_rejected(self):
        with pytest.raises(SystemExit) as ei:
            egress_run.parse_args(["--default", "a:b", "--", "true"])
        assert ei.value.code == egress_run.EXIT_USAGE

    def test_unknown_option_rejected(self):
        with pytest.raises(SystemExit) as ei:
            egress_run.parse_args(["--frobnicate", "direct", "--", "true"])
        assert ei.value.code == egress_run.EXIT_USAGE

    def test_no_route_no_default(self):
        with pytest.raises(SystemExit) as ei:
            egress_run.parse_args(["--", "true"])
        assert ei.value.code == egress_run.EXIT_USAGE

    def test_command_after_double_dash_verbatim(self):
        # the command is everything after the first '--', untouched
        opts, cmd = egress_run.parse_args(
            ["direct", "--", "echo", "--mode", "proxy", "a b"])
        assert cmd == ["echo", "--mode", "proxy", "a b"]


# ---------------------------------------------------------------------------
# direct: no proxy, no API
# ---------------------------------------------------------------------------
class TestDirect:
    def test_direct_exit_code_preserved(self, monkeypatch, cfg_file):
        code = run_egress_run(monkeypatch, cfg_file,
                              ["direct", "--", "sh", "-c", "exit 42"])
        assert code == 42

    def test_direct_no_proxy_env(self, monkeypatch, cfg_file, tmp_path):
        # print the child's proxy env through egress-run direct
        script = tmp_path / "env.sh"
        script.write_text("printf '%s|%s|%s' \"$ALL_PROXY\" \"$all_proxy\" \"$NO_PROXY\"\n")
        p = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(ROOT / 'client' / 'src')!r});"
             "from vibelab_egress_client import egress_run;"
             f"egress_run.main(['--config', {cfg_file!r}, 'direct', '--',"
             f" 'sh', {str(script)!r}])"],
            capture_output=True, text=True)
        assert p.returncode == 0
        all_proxy, lower, no_proxy = p.stdout.split("|")
        # direct: no proxy env at all (no proxy URL, no no_proxy list)
        assert all_proxy == "" and lower == "" and no_proxy == ""

    def test_direct_no_api_call(self, monkeypatch, cfg_file):
        def boom(*a, **k):
            raise AssertionError("API must not be called for direct")
        monkeypatch.setattr(egress_run, "EgressApi", boom)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["direct", "--", "true"])
        assert code == 0


# ---------------------------------------------------------------------------
# proxy mode: default vs explicit route
# ---------------------------------------------------------------------------
class TestProxy:
    def test_default_uses_default_proxy_auth(self, monkeypatch, cfg_file):
        seen = {}

        class Stub:
            def get_default_mode(self):
                return {"mode": "proxy"}

        def run_env(env, command):
            seen.update(env)
            return 0
        monkeypatch.setattr(egress_run, "_run_local", run_env)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["--default", "--", "true"], api_stub=Stub())
        assert code == 0
        assert "socks5h://os-user:os-pass@127.0.0.1:1080" in seen["all_proxy"]

    def test_explicit_route_uses_oneshot_session(self, monkeypatch, cfg_file):
        calls = {"created": [], "closed": []}

        class Stub:
            def oneshot_session(self, route):
                calls["created"].append(route)
                return {"id": "job-1", "capability": {
                    "host": "127.0.0.1", "port": 18081,
                    "username": "os_0", "password": "pw-0"}}

            def oneshot_close(self, sid):
                calls["closed"].append(sid)
                return {}
        monkeypatch.setattr(egress_run, "_run_local", lambda env, cmd: 0)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["blanc:FI-Helsinki-1", "--", "true"],
                              api_stub=Stub())
        assert code == 0
        assert calls["created"] == ["blanc:FI-Helsinki-1"]
        assert calls["closed"] == ["job-1"]  # closed in finally

    def test_personal_alias_uses_canonical_route(self, monkeypatch, cfg_file):
        calls = []
        class Stub:
            def resolve_route(self, route):
                assert route == "@blanc:vless-49"
                return "p-host-vibelab-blanc:vless-49"
            def oneshot_session(self, route):
                calls.append(route)
                return {"id": "job-alias", "capability": {
                    "username": "os_0", "password": "pw"}}
            def oneshot_close(self, _sid):
                return {}
        monkeypatch.setattr(egress_run, "_run_local", lambda _env, _cmd: 0)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["@blanc:vless-49", "--", "true"], api_stub=Stub())
        assert code == 0
        assert calls == ["p-host-vibelab-blanc:vless-49"]

    def test_oneshot_uses_client_vantage(self, monkeypatch, cfg_file):
        """The one-shot SOCKS endpoint comes from the CLIENT's own vantage
        (published port), not from the backend-reported host/port (which
        describe egressd's vantage and are unusable inside a container)."""
        seen = {}

        class Stub:
            def oneshot_session(self, route):
                return {"id": "job-4", "capability": {
                    "host": "127.0.0.1", "port": 18081,
                    "username": "os_0", "password": "pw-0"}}

            def oneshot_close(self, sid):
                return {}

        def run_env(env, cmd):
            seen.update(env)
            return 0
        monkeypatch.setattr(egress_run, "_run_local", run_env)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["blanc:FI-Helsinki-1", "--", "true"],
                              api_stub=Stub())
        assert code == 0
        # cfg_file: proxy_host=127.0.0.1, oneshot_proxy_port=1081
        assert "socks5h://os_0:pw-0@127.0.0.1:1081" in seen["all_proxy"]

    def test_explicit_route_session_closed_on_child_failure(
            self, monkeypatch, cfg_file):
        calls = {"closed": []}

        class Stub:
            def oneshot_session(self, route):
                return {"id": "job-2", "capability": {
                    "host": "127.0.0.1", "port": 18081,
                    "username": "os_0", "password": "pw-0"}}

            def oneshot_close(self, sid):
                calls["closed"].append(sid)
                return {}
        def run_env(env, cmd):
            return 7
        monkeypatch.setattr(egress_run, "_run_local", run_env)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["blanc:FI-Helsinki-1", "--", "true"],
                              api_stub=Stub())
        assert code == 7
        assert calls["closed"] == ["job-2"]

    def test_explicit_route_does_not_touch_default(self, monkeypatch, cfg_file):
        """An explicit route must never call set_default (spec 30.2)."""
        class Stub:
            def set_default(self, route):
                raise AssertionError("explicit route must not change the default")

            def oneshot_session(self, route):
                return {"id": "job-3", "capability": {
                    "host": "127.0.0.1", "port": 18081,
                    "username": "os_0", "password": "pw-0"}}

            def oneshot_close(self, sid):
                return {}
        monkeypatch.setattr(egress_run, "_run_local", lambda env, cmd: 0)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["blanc:FI-Helsinki-1", "--", "true"],
                              api_stub=Stub())
        assert code == 0

    def test_oneshot_api_error(self, monkeypatch, cfg_file):
        class Stub:
            def oneshot_session(self, route):
                raise ApiError(404, "unknown route")
        code = run_egress_run(monkeypatch, cfg_file,
                              ["nope:no", "--", "true"], api_stub=Stub())
        assert code == egress_run.EXIT_USAGE

    def test_oneshot_auth_error(self, monkeypatch, cfg_file):
        class Stub:
            def oneshot_session(self, route):
                raise ApiError(401, "unknown token")
        code = run_egress_run(monkeypatch, cfg_file,
                              ["nope:no", "--", "true"], api_stub=Stub())
        assert code == egress_run.EXIT_AUTH


# ---------------------------------------------------------------------------
# per-run local TUN: explicit failures, no fallback, private namespace runner
# ---------------------------------------------------------------------------
class TestTun:
    def test_unsupported_probe_fails_closed(self, monkeypatch, cfg_file):
        from vibelab_egress_client import tun_runtime
        monkeypatch.setattr(tun_runtime, "probe", lambda: {"status": "unsupported_in_environment", "reason": "no userns"})
        assert run_egress_run(monkeypatch, cfg_file,
                              ["--mode", "tun", "direct", "--", "true"]) == egress_run.EXIT_UNSUPPORTED

    def test_workload_uses_local_tun_runner(self, monkeypatch, cfg_file):
        from vibelab_egress_client import tun_runtime
        monkeypatch.setattr(tun_runtime, "probe", lambda: {"status": "supported", "reason": "ok"})
        seen = {}
        def fake_run(command, **kwargs):
            seen.update(command=command, **kwargs)
            return 7
        monkeypatch.setattr(tun_runtime, "run", fake_run)
        assert run_egress_run(monkeypatch, cfg_file,
                              ["--mode", "tun", "direct", "--", "sh", "-c", "exit 7"]) == 7
        assert seen["command"] == ["sh", "-c", "exit 7"]
        assert seen["upstream"] is None
        assert seen["dns_servers"] == ("1.1.1.1", "1.0.0.1")

    def test_route_specific_socks_capability_is_closed_after_run(self, monkeypatch, cfg_file):
        from vibelab_egress_client import tun_runtime
        monkeypatch.setattr(tun_runtime, "probe", lambda: {"status": "supported", "reason": "ok"})
        seen = {}
        class Api:
            def oneshot_session(self, route):
                seen["route"] = route
                return {"id": "session-1", "capability": {
                    "username": "route-user", "password": "route-pass"}}
            def oneshot_close(self, session_id):
                seen["closed"] = session_id
        def fake_run(command, **kwargs):
            seen.update(upstream=kwargs["upstream"])
            return 0
        monkeypatch.setattr(tun_runtime, "run", fake_run)
        assert run_egress_run(monkeypatch, cfg_file,
                              ["--mode", "tun", "blanc:FI-Helsinki-1", "--", "true"],
                              api_stub=Api()) == 0
        assert seen["route"] == "blanc:FI-Helsinki-1"
        assert seen["upstream"] == {
            "host": "127.0.0.1", "port": 1081,
            "username": "route-user", "password": "route-pass"}
        assert seen["closed"] == "session-1"

    def test_none_backend_fails_without_fallback(self, monkeypatch, cfg_file):
        path = Path(cfg_file)
        path.write_text(path.read_text().replace("tun_backend: local-userns", "tun_backend: none"))
        assert run_egress_run(monkeypatch, cfg_file,
                              ["--mode", "tun", "direct", "--", "true"]) == egress_run.EXIT_UNSUPPORTED

    def test_runtime_has_no_helper_socket_protocol(self):
        from vibelab_egress_client import tun_runtime
        assert not hasattr(egress_run, "_tun_helper_call")
        assert "--net" in Path(tun_runtime.__file__).read_text()


# ---------------------------------------------------------------------------
# child process semantics: argv list, exit status, signals
# ---------------------------------------------------------------------------
class TestChildSemantics:
    def test_argv_not_shell_reparsed(self, monkeypatch, cfg_file):
        # a single argument with spaces must survive as one argv element
        marker = "a b c"
        code = run_egress_run(monkeypatch, cfg_file,
                              ["direct", "--", "sh", "-c",
                               "test \"$1\" = 'a b c' && test $# -eq 1",
                               "sh", marker])
        assert code == 0

    def test_exit_status_preserved(self, monkeypatch, cfg_file):
        assert run_egress_run(monkeypatch, cfg_file,
                              ["direct", "--", "sh", "-c", "exit 3"]) == 3
        assert run_egress_run(monkeypatch, cfg_file,
                              ["direct", "--", "sh", "-c", "exit 0"]) == 0

    def test_signal_death_normalized_to_128_plus_signum(self, monkeypatch,
                                                        cfg_file):
        # child killed by SIGTERM -> egress-run exits 143 (shell convention)
        code = run_egress_run(monkeypatch, cfg_file,
                              ["direct", "--", "sh", "-c", "kill -TERM $$; sleep 1"])
        assert code == 128 + signal.SIGTERM

    def test_sigterm_forwarded_to_child(self, monkeypatch, cfg_file, tmp_path):
        # the child writes a file when it receives SIGTERM, then exits.
        # Wait for a 'ready' marker first so we never signal before the trap
        # is installed (removes the fixed-sleep race).
        ready = tmp_path / "ready"
        marker = tmp_path / "got-sigterm"
        # install the trap BEFORE signalling ready, so the parent (which only
        # signals after seeing the ready marker) can never hit the child in
        # the window before the trap is installed.
        script = (f"trap 'touch {marker}; exit 143' TERM;"
                  f" touch {ready};"
                  f" while :; do sleep 0.1; done")
        p = subprocess.Popen(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(ROOT / 'client' / 'src')!r});"
             "from vibelab_egress_client import egress_run;"
             f"egress_run.main(['--config', {cfg_file!r}, 'direct', '--',"
             f" 'sh', '-c', {script!r}])"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 10
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "child never signalled ready"
            p.send_signal(signal.SIGTERM)
            rc = p.wait(timeout=15)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        assert rc == 128 + signal.SIGTERM
        assert marker.exists(), "child did not receive SIGTERM"

    def test_concurrent_invocations_independent(self, monkeypatch, cfg_file):
        """Two concurrent egress-run processes must not interfere."""
        def run_env(env, cmd):
            time.sleep(0.3)
            return 0
        monkeypatch.setattr(egress_run, "_run_local", run_env)
        import threading
        results = []

        def worker(route):
            code = run_egress_run(monkeypatch, cfg_file,
                                  [route, "--", "true"], api_stub=_Stub())
            results.append((route, code))
        class _Stub:
            def oneshot_session(self, route):
                return {"id": f"job-{route}", "capability": {
                    "host": "127.0.0.1", "port": 18081,
                    "username": "os_0", "password": "pw-0"}}

            def oneshot_close(self, sid):
                return {}
        ts = [threading.Thread(target=worker, args=(r,))
              for r in ("blanc:FI-Helsinki-1", "xeovo:US-New-York-1", "direct")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert all(code == 0 for _, code in results)
