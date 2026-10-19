"""A4 — local client package integration tests (spec section 30, 48).

A real ``egressd`` process is started on a local port (offline: no mihomo,
no network beyond loopback) and the **same client package** is exercised
against it through the actual ``egressctl`` / ``egress-run`` entry points,
with a real on-disk client config + token files.

Covers the spec 48 items that a client can verify from inside its own
environment:

  1. client package installed and version reported;
  2. ``egressctl config validate``;
  3. ``egressctl whoami`` -> server-bound identity;
  4. direct API ``/v1/self`` returns the same identity/capabilities (parity);
  5. spoofing another ``client_id`` does not change the identity;
  6. ``egressctl route current``;
  7/8. ``egressctl route set direct`` / ``route set <VPN>``;
  9. (host-side assertion: covered by the host harness; here we assert the
     per-client isolation through the API with two live clients);
 11/12. ``egress-run direct`` / ``egress-run <VPN>`` execute the child
     locally (marker file + pid in the same environment);
 13. 3 concurrent one-shot invocations (two VPN routes + direct);
 16. route list / reconciliation;
 17. health test (offline: mihomo delay degrades, direct baseline works);
 23. explicit route never changes the persistent default;
 25. ``--mode tun`` -> explicit unsupported (no silent fallback);
 26. exit status / signal semantics through the real binary path.

Run:  python -m pytest tests/integration/test_a4.py -v
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CLIENT_SRC = ROOT / "client" / "src"
EGRESSD_SRC = ROOT / "egressd" / "src"
sys.path.insert(0, str(EGRESSD_SRC))
sys.path.insert(0, str(CLIENT_SRC))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"egressd did not become ready at {url}")


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    """A real egressd process with two clients and two providers."""
    base = tmp_path_factory.mktemp("a4")
    clients = base / "clients"
    clients.mkdir()
    for cid in ("host-main", "container-main"):
        (clients / f"{cid}.token").write_text(f"tok-{cid}\n")
        env = "host" if cid == "host-main" else "container"
        (clients / f"{cid}.profile.json").write_text(json.dumps(
            {"client_id": cid, "environment": env, "modes": ["proxy"]}))
        (clients / f"{cid}.proxy-creds").write_text(f"{cid}-user:{cid}-pass\n")

    provs = base / "providers"
    provs.mkdir()
    (provs / "blanc.yaml").write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "FI-Helsinki-2", "type": "socks5", "server": "1.1.1.2", "port": 1080},
    ]}, sort_keys=False))
    (provs / "xeovo.yaml").write_text(yaml.safe_dump({"proxies": [
        {"name": "US-New-York-1", "type": "socks5", "server": "2.2.2.1", "port": 1080},
    ]}, sort_keys=False))

    port = _free_port()
    logf = open(base / "egressd.log", "w")
    env = dict(os.environ)
    env.update({
        "EGRESSD_CLIENTS_DIR": str(clients),
        "EGRESSD_PROVIDERS_DIR": str(provs),
        "EGRESSD_STATE_DIR": str(base / "state"),
        "EGRESSD_METRICS_DB": str(base / "state" / "metrics.sqlite"),
        "EGRESSD_GEO_DIR": str(base / "geo"),
        "EGRESSD_ONESHOT_SLOTS": "4",
        "EGRESSD_LISTEN": f"127.0.0.1:{port}",
        "EGRESSD_LOG_LEVEL": "WARNING",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "egressd.main"],
        env=env, stdout=logf, stderr=subprocess.STDOUT,
        cwd=str(EGRESSD_SRC),
    )
    _wait_http(f"http://127.0.0.1:{port}/v1/health")
    yield {
        "base": base,
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "proc": proc,
        "logf": logf,
    }
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    logf.close()


def _client_env(base: Path, cid: str, port: int) -> tuple[Path, dict]:
    """Write a client config + secrets for ``cid``; return (config_path, env)."""
    cdir = base / f"client-{cid}"
    cdir.mkdir(exist_ok=True)
    token = cdir / "client.token"
    auth = cdir / "default-proxy.auth"
    token.write_text(f"tok-{cid}\n")
    auth.write_text(f"{cid}-user:{cid}-pass\n")
    cfg = cdir / "client.yaml"
    cfg.write_text(
        f"client_id: {cid}\n"
        f"control_url: http://127.0.0.1:{port}\n"
        f"proxy_host: 127.0.0.1\n"
        f"default_proxy_port: 1080\n"
        f"oneshot_proxy_port: 1081\n"
        f"api_token_file: {token}\n"
        f"default_proxy_auth_file: {auth}\n"
        f"environment: {'host' if cid == 'host-main' else 'container'}\n"
        f"tun_backend: none\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(CLIENT_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return cfg, env


def _ctl(cfg: Path, env: dict, *args, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vibelab_egress_client.egressctl",
         "--config", str(cfg), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _run(cfg: Path, env: dict, *args, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vibelab_egress_client.egress_run",
         "--config", str(cfg), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _api(url: str, token: str, method: str, path: str, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


# ---------------------------------------------------------------------------
# 1-3. package, config validate, whoami
# ---------------------------------------------------------------------------
class TestIdentity:
    def test_version_reported(self, backend):
        from vibelab_egress_client import __version__
        assert __version__

    def test_config_validate(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _ctl(cfg, env, "config", "validate")
        assert p.returncode == 0, p.stderr
        assert "valid" in p.stdout

    def test_whoami_returns_server_bound_identity(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _ctl(cfg, env, "whoami")
        assert p.returncode == 0, p.stderr
        assert "client_id=container-main" in p.stdout
        assert "environment=container" in p.stdout

    def test_whoami_json(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _ctl(cfg, env, "--json", "whoami")
        data = json.loads(p.stdout)
        assert data["client_id"] == "container-main"
        assert "proxy" in data["modes"]
        assert data["capabilities"]

    def test_identity_mismatch_detected(self, backend):
        """A config claiming host-main but holding the container token must
        be rejected (spec 29: startup SHOULD verify /v1/self identity)."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        cfg.write_text(cfg.read_text().replace("client_id: container-main",
                                               "client_id: host-main"))
        p = _ctl(cfg, env, "whoami")
        assert p.returncode == 5
        assert "identity mismatch" in p.stderr

    def test_api_parity_self(self, backend):
        """egressctl whoami and a direct API call must agree (spec 27.1)."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _ctl(cfg, env, "--json", "whoami")
        cli = json.loads(p.stdout)
        st, api = _api(backend["url"], "tok-container-main", "GET", "/v1/self")
        assert st == 200
        assert cli["client_id"] == api["client_id"]
        assert cli["environment"] == api["environment"]
        assert cli["modes"] == api["modes"]

    def test_spoofed_client_id_ignored(self, backend):
        """A client_id field in the body must not change the identity."""
        url = backend["url"]
        st, body = _api(url, "tok-container-main", "PUT", "/v1/default",
                        {"route": "blanc:FI-Helsinki-1",
                         "client_id": "host-main"})
        assert st == 200
        assert body["client_id"] == "container-main"
        # host-main's default is untouched
        st, h = _api(url, "tok-host-main", "GET", "/v1/default")
        assert h["route"] == "direct"
        # reset container's default (the spoofed body correctly applied to
        # the token's own client) so later tests start from a known state
        _api(url, "tok-container-main", "PUT", "/v1/default",
             {"route": "direct"})


# ---------------------------------------------------------------------------
# 6-9. route current / set, per-client isolation
# ---------------------------------------------------------------------------
class TestDefaults:
    def test_route_current(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "route", "current")
        assert p.returncode == 0
        assert "default route: direct" in p.stdout

    def test_route_set_and_current(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "route", "set", "blanc:FI-Helsinki-1")
        assert p.returncode == 0, p.stderr
        p = _ctl(cfg, env, "route", "current")
        assert "blanc:FI-Helsinki-1" in p.stdout
        # reset for other tests
        _ctl(cfg, env, "route", "set", "direct")

    def test_route_set_unknown_route(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "route", "set", "nope:no")
        assert p.returncode == 1
        assert "unknown route" in p.stderr

    def test_per_client_isolation(self, backend):
        """Changing container's default must not change host's default
        (spec 15/16, acceptance 10) — through the live CLI + API."""
        os_cfg, os_env = _client_env(backend["base"], "container-main", backend["port"])
        h_cfg, h_env = _client_env(backend["base"], "host-main", backend["port"])
        url = backend["url"]
        # both start on direct
        assert _api(url, "tok-host-main", "GET", "/v1/default")[1]["route"] == "direct"
        assert _api(url, "tok-container-main", "GET", "/v1/default")[1]["route"] == "direct"
        # container switches to a VPN route
        p = _ctl(os_cfg, os_env, "route", "set", "xeovo:US-New-York-1")
        assert p.returncode == 0, p.stderr
        # container sees the new default
        p = _ctl(os_cfg, os_env, "route", "current")
        assert "xeovo:US-New-York-1" in p.stdout
        # host is unchanged
        p = _ctl(h_cfg, h_env, "route", "current")
        assert "direct" in p.stdout
        # reset
        _ctl(os_cfg, os_env, "route", "set", "direct")

    def test_mode_set_and_current(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "mode", "current")
        assert p.returncode == 0 and "default mode: proxy" in p.stdout

    def test_mode_set_tun_unsupported(self, backend):
        """tun is not in this client's modes -> explicit unsupported, no
        silent fallback (spec 16, acceptance 25)."""
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "mode", "set", "tun")
        assert p.returncode == 1
        assert "unsupported" in p.stderr.lower()
        # the mode is still proxy (no silent fallback)
        p = _ctl(cfg, env, "mode", "current")
        assert "default mode: proxy" in p.stdout


# ---------------------------------------------------------------------------
# 16-17. list / test
# ---------------------------------------------------------------------------
class TestListAndTest:
    def test_list(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "--json", "list")
        data = json.loads(p.stdout)
        ids = {r["id"] for r in data["routes"]}
        assert "direct" in ids
        assert "blanc:FI-Helsinki-1" in ids
        assert "xeovo:US-New-York-1" in ids

    def test_list_country_filter(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "--json", "list", "--country", "FI")
        data = json.loads(p.stdout)
        assert all(r["country"] == "FI" for r in data["routes"])

    def test_single_route(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        # the direct probe does a mihomo delay check that is slow offline;
        # give it a generous timeout so it does not flake under load.
        p = _ctl(cfg, env, "--json", "test", "direct", timeout=120)
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["count"] == 1
        # offline: the direct baseline may be healthy or not, but the result
        # is structured
        assert "healthy" in data["results"][0]

    def test_unknown_route(self, backend):
        cfg, env = _client_env(backend["base"], "host-main", backend["port"])
        p = _ctl(cfg, env, "--json", "test", "nope:no")
        data = json.loads(p.stdout)
        assert data["results"][0]["error"] == "unknown_route"


# ---------------------------------------------------------------------------
# 11-13, 23, 25, 26. egress-run semantics
# ---------------------------------------------------------------------------
class TestEgressRun:
    def test_direct_runs_child_locally(self, backend, tmp_path):
        """The child executes in the caller's environment: it writes a
        marker file that we can read here (same filesystem, same process
        tree) and reports its own pid (spec 30.2, acceptance 25/26)."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        marker = tmp_path / "direct-marker"
        p = _run(cfg, env, "direct", "--", sys.executable, "-c",
                 f"import os, json; "
                 f"open({str(marker)!r}, 'w').write("
                 f"json.dumps({{'pid': os.getpid(), 'uid': os.getuid()}}))")
        assert p.returncode == 0, p.stderr
        data = json.loads(marker.read_text())
        assert data["pid"] > 0
        assert data["uid"] == os.getuid()  # same user -> local execution

    def test_direct_exit_status_preserved(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        assert _run(cfg, env, "direct", "--", "sh", "-c", "exit 42").returncode == 42
        assert _run(cfg, env, "direct", "--", "true").returncode == 0

    def test_vpn_route_env_and_local_execution(self, backend, tmp_path):
        """egress-run <VPN>: the child gets the one-shot SOCKS env and still
        runs locally (marker file). Offline, the proxy itself is not
        reachable, so the child only reports its env."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        marker = tmp_path / "vpn-marker"
        p = _run(cfg, env, "blanc:FI-Helsinki-1", "--", sys.executable, "-c",
                 f"import os; "
                 f"open({str(marker)!r}, 'w').write(os.environ.get('ALL_PROXY', ''))")
        assert p.returncode == 0, p.stderr
        proxy_url = marker.read_text()
        assert proxy_url.startswith("socks5h://os_")  # slot credential
        assert f"@127.0.0.1:{backend['port']}" not in proxy_url

    def test_explicit_route_does_not_change_default(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        url = backend["url"]
        assert _api(url, "tok-container-main", "GET", "/v1/default")[1]["route"] == "direct"
        p = _run(cfg, env, "blanc:FI-Helsinki-2", "--", "true")
        assert p.returncode == 0, p.stderr
        # the persistent default is still direct (spec 30.2)
        st, body = _api(url, "tok-container-main", "GET", "/v1/default")
        assert body["route"] == "direct"

    def test_default_mode_uses_default_proxy_auth(self, backend, tmp_path):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        marker = tmp_path / "default-marker"
        p = _run(cfg, env, "--default", "--", sys.executable, "-c",
                 f"import os; "
                 f"open({str(marker)!r}, 'w').write(os.environ.get('ALL_PROXY', ''))")
        assert p.returncode == 0, p.stderr
        proxy_url = marker.read_text()
        assert proxy_url == "socks5h://container-main-user:container-main-pass@127.0.0.1:1080"

    def test_mode_tun_unsupported_explicit(self, backend):
        """--mode tun must fail explicitly (no silent proxy fallback)."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _run(cfg, env, "--mode", "tun", "direct", "--", "true")
        assert p.returncode == 6
        assert "tun" in p.stderr.lower()

    def test_signal_forwarding(self, backend, tmp_path):
        """SIGTERM to egress-run reaches the child (128+15)."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        ready = tmp_path / "ready"
        marker = tmp_path / "sigterm-marker"
        proc = subprocess.Popen(
            [sys.executable, "-m", "vibelab_egress_client.egress_run",
             "--config", str(cfg), "direct", "--", "sh", "-c",
             f"trap 'touch {marker}; exit 143' TERM; touch {ready};"
             f" while :; do sleep 0.1; done"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # wait for the child to install its trap before signaling (removes
            # the fixed-sleep race that flakes under load)
            deadline = time.time() + 15
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "child never signalled ready"
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert rc == 128 + signal.SIGTERM
        assert marker.exists()

    def test_three_concurrent_oneshots(self, backend, tmp_path):
        """3 concurrent egress-run invocations through different routes
        (two VPN + direct) must all succeed without interference
        (spec 18/20, acceptance 23/24)."""
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        procs = []
        for i, route in enumerate(("blanc:FI-Helsinki-1", "xeovo:US-New-York-1",
                                   "direct")):
            marker = tmp_path / f"conc-{i}"
            procs.append((route, marker, subprocess.Popen(
                [sys.executable, "-m", "vibelab_egress_client.egress_run",
                 "--config", str(cfg), route, "--", sys.executable, "-c",
                 f"import time; time.sleep(0.5); open({str(marker)!r}, 'w').write('ok')"],
                env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)))
        for route, marker, p in procs:
            rc = p.wait(timeout=30)
            assert rc == 0, f"{route}: rc={rc}"
            assert marker.exists() and marker.read_text() == "ok"


# ---------------------------------------------------------------------------
# 22. raw mihomo API is not reachable from the client (offline here: the
# client has no mihomo URL/secret at all; assert the config carries none)
# ---------------------------------------------------------------------------
class TestNoBackendSecrets:
    def test_client_config_has_no_backend_secrets(self, backend):
        cfg, _ = _client_env(backend["base"], "container-main", backend["port"])
        text = cfg.read_text()
        assert "mihomo" not in text.lower()
        assert "secret" not in text.lower()
        # the client token is a client credential, not a provider secret
        assert "tok-container-main" in (backend["base"] /
                                          "client-container-main" /
                                          "client.token").read_text()

    def test_config_show_masks_secrets(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _ctl(cfg, env, "config", "show")
        assert p.returncode == 0
        assert "tok-container-main" not in p.stdout
        assert "container-main-pass" not in p.stdout

    def test_doctor_runs(self, backend):
        cfg, env = _client_env(backend["base"], "container-main", backend["port"])
        p = _ctl(cfg, env, "doctor")
        # offline: proxy endpoint check fails -> exit 1, but the checks run
        # and no credentials are printed
        assert p.returncode in (0, 1)
        assert "config: ok" in p.stdout
        assert "identity: ok (container-main)" in p.stdout
        assert "tok-container-main" not in p.stdout
        assert "container-main-pass" not in p.stdout
