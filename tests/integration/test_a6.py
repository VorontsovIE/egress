"""A6 — strict/TUN helper tests (spec section 21, phase A6).

The TUN helper is a local privileged helper. In this sandbox it cannot
actually create netns/TUN (no CAP_NET_ADMIN), so the tests focus on the
parts that ARE verifiable here and that carry the security invariants:

  * capability probe reports the real environment (unsupported_in_environment
    here, with a specific reason);
  * the fixed request protocol (capability/run/cleanup) and its validation:
    unknown op/field, malformed route, arbitrary upstream, direct+upstream,
    empty/oversized command, bad timeout;
  * `run` in an unsupported environment fails EXPLICITLY with
    unsupported_in_environment (no silent proxy fallback);
  * the UNIX-socket protocol (length-prefixed JSON frames) end to end, with
    SO_PEERCRED identity;
  * stale netns cleanup (file-based, no privileges needed);
  * compose.tun.yaml static audit: no public binds, cap_add is exactly
    NET_ADMIN (cap_drop ALL), no privileged, no docker socket,
    no-new-privileges, /dev/net/tun mounted;
  * egressd /v1/capabilities reports tun per-client (supported vs
    unsupported_in_environment) consistently with the mode-set check.

Run:  python -m pytest tests/integration/test_a6.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

HELPER = ROOT / "host" / "strict-helper" / "egress-tun-helper.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("egress_tun_helper", HELPER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["egress_tun_helper"] = mod
    spec.loader.exec_module(mod)
    return mod


th = _load_helper()

# shared fixtures for the helper tests
KNOWN = {"blanc:FI-Helsinki-1", "direct"}
UPS = {"127.0.0.1:18081"}


# ---------------------------------------------------------------------------
# capability probe
# ---------------------------------------------------------------------------
class TestCapability:
    def test_named_netns_requires_sys_admin(self, monkeypatch):
        monkeypatch.setattr(th, "_tun_dev_present", lambda: True)
        monkeypatch.setattr(th, "_has_cap_net_admin", lambda: True)
        monkeypatch.setattr(th, "_has_cap_sys_admin", lambda: False)
        monkeypatch.setattr(th.shutil, "which", lambda name: "/usr/sbin/ip")
        cap = th.probe_capability().to_dict()
        assert cap["status"] == "unsupported_in_environment"
        assert "CAP_SYS_ADMIN" in cap["reason"]

    def test_probe_reports_environment(self):
        cap = th.probe_capability().to_dict()
        # in this sandbox: no CAP_NET_ADMIN -> unsupported, with a reason
        assert cap["status"] in ("supported", "unsupported_in_environment")
        if cap["status"] == "unsupported_in_environment":
            assert cap["reason"]
            assert cap["tun"] is False
        # the fields are always present and typed
        assert isinstance(cap["tun_dev"], bool)
        assert isinstance(cap["cap_net_admin"], bool)
        assert isinstance(cap["netns"], bool)
        assert cap["backend"] in ("none", "netns+tun")

    def test_probe_is_pure(self):
        """The probe must not mutate the caller's netns (no side effects)."""
        before = os.uname().nodename
        th.probe_capability()
        assert os.uname().nodename == before


# ---------------------------------------------------------------------------
# request validation (the fixed protocol, spec 21.4)
# ---------------------------------------------------------------------------
class TestValidation:
    def _run(self, **over):
        req = {"op": "run", "route": "direct", "command": ["true"]}
        req.update(over)
        return req

    def test_valid_direct(self):
        th.validate_run(self._run(), KNOWN, UPS)

    def test_valid_route_with_upstream(self):
        th.validate_run(self._run(
            route="blanc:FI-Helsinki-1",
            upstream={"host": "127.0.0.1", "port": 18081,
                      "username": "u", "password": "p"}), KNOWN, UPS)

    @pytest.mark.parametrize("route", [
        "nope:no", "../etc/passwd", "a b", "a\nb", "a|b", "$(id)",
        "a:b:c", "a" * 5000,
    ])
    def test_malformed_route_rejected(self, route):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(route=route,
                                      upstream={"host": "127.0.0.1", "port": 18081}),
                            KNOWN, UPS)

    def test_direct_with_upstream_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(
                route="direct",
                upstream={"host": "127.0.0.1", "port": 18081}), KNOWN, UPS)

    def test_route_without_upstream_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(route="blanc:FI-Helsinki-1"),
                            KNOWN, UPS)

    def test_arbitrary_upstream_rejected(self):
        """The helper cannot be pointed at an arbitrary proxy (spec 21.4)."""
        for host, port in [("8.8.8.8", 1080), ("127.0.0.1", 9999),
                           ("evil.example.com", 443)]:
            with pytest.raises(th.ValidationError):
                th.validate_run(self._run(
                    route="blanc:FI-Helsinki-1",
                    upstream={"host": host, "port": port}), KNOWN, UPS)

    def test_empty_command_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(command=[]), KNOWN, UPS)

    def test_non_string_command_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(command=[1, 2]), KNOWN, UPS)

    def test_oversized_command_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(command=["a"] * (th.MAX_COMMAND_ARGS + 1)),
                            KNOWN, UPS)

    @pytest.mark.parametrize("timeout", [0, -1, 100000, "300"])
    def test_bad_timeout_rejected(self, timeout):
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(timeout=timeout), KNOWN, UPS)

    def test_unknown_field_rejected(self):
        """No free-form field (e.g. a shell flag) is accepted (spec 21.4)."""
        with pytest.raises(th.ValidationError):
            th.validate_run(self._run(shell="-c"), KNOWN, UPS)

    def test_unknown_op_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_run({"op": "exec", "command": ["id"]}, KNOWN, UPS)

    def test_cleanup_negative_older_than_rejected(self):
        with pytest.raises(th.ValidationError):
            th.validate_cleanup({"op": "cleanup", "older_than": -1})


# ---------------------------------------------------------------------------
# run in an unsupported environment: explicit, no silent fallback
# ---------------------------------------------------------------------------
class TestUnsupportedRun:
    def test_run_unsupported_explicit(self):
        h = th.Helper(KNOWN, UPS)
        resp = h.op_run({"op": "run", "route": "direct", "command": ["true"]},
                        1000, 1000)
        assert resp["ok"] is False
        assert resp["error"] == "unsupported_in_environment"
        assert resp["capability"]["status"] == "unsupported_in_environment"
        # no proxy fallback: there is no 'proxy' key / no env in the response
        assert "proxy" not in resp

    def test_run_validation_before_capability(self):
        """A malformed request is rejected before any capability work."""
        h = th.Helper(KNOWN, UPS)
        with pytest.raises(th.ValidationError):
            h.op_run({"op": "run", "route": "nope", "command": ["true"]},
                     1000, 1000)

    def test_netns_setup_error_returns_response(self, tmp_path):
        """A failed ip netns add must not kill the socket server."""
        class FailingBackend(th.NetnsBackend):
            def create(self, name):
                raise subprocess.CalledProcessError(1, ["ip", "netns", "add", name])

        backend = FailingBackend(str(tmp_path))
        cap = th.Capability(True, True, True, True, "ok", "netns+tun")
        h = th.Helper(KNOWN, UPS, backend=backend, capability=lambda: cap)
        resp = h.op_run({"op": "run", "route": "direct", "command": ["true"]},
                        1000, 1000)
        assert resp["ok"] is False
        assert resp["error"] == "netns_setup_failed"
        assert "ip" in resp["detail"]


# ---------------------------------------------------------------------------
# stale netns cleanup (file-based, no privileges needed)
# ---------------------------------------------------------------------------
class TestStaleCleanup:
    def test_cleanup_removes_only_stale(self, tmp_path):
        be = th.NetnsBackend(state_dir=str(tmp_path))
        netns_dir = tmp_path / "netns"
        netns_dir.mkdir()
        now = time.time()
        for name, age in (("egress-tun-old", 9999), ("egress-tun-new", 5),
                          ("other-ns-old", 9999)):  # not our prefix
            (netns_dir / (name + ".ts")).write_text(str(now - age))
        removed = be.cleanup_stale(ttl=300)
        assert sorted(removed) == ["egress-tun-old"]
        # the new one and the non-prefixed one survive
        assert (netns_dir / "egress-tun-new.ts").exists()
        assert (netns_dir / "other-ns-old.ts").exists()

    def test_cleanup_empty(self, tmp_path):
        be = th.NetnsBackend(state_dir=str(tmp_path))
        assert be.cleanup_stale() == []


# ---------------------------------------------------------------------------
# UNIX-socket protocol (length-prefixed JSON, SO_PEERCRED identity)
# ---------------------------------------------------------------------------
class TestSocketProtocol:
    def test_frames_roundtrip(self):
        # in-memory socketpair to exercise the frame codec without a server
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            th.write_frame(a, {"op": "capability"})
            got = th.read_frame(b)
            assert got == {"op": "capability"}
        finally:
            a.close()
            b.close()

    def test_read_frame_eof(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            a.close()
            assert th.read_frame(b) is None
        finally:
            b.close()

    def test_peer_credentials(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            uid, gid = th.peer_credentials(b)
            assert uid == os.getuid()
            assert gid == os.getgid()
        finally:
            a.close()
            b.close()

    def test_end_to_end_capability(self, tmp_path):
        """Start the helper server in a thread; a client gets the capability."""
        sock = tmp_path / "tun.sock"
        stop = threading.Event()

        def server():
            # run a single-connection server; stop after the first response
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            d = str(tmp_path)
            os.makedirs(d, exist_ok=True)
            if sock.exists():
                sock.unlink()
            srv.bind(str(sock))
            srv.listen(1)
            srv.settimeout(10)
            conn = None
            try:
                conn, _ = srv.accept()
                helper = th.Helper(KNOWN, UPS)
                th._handle_conn(conn, helper)
            except socket.timeout:
                pass
            finally:
                if conn is not None:
                    conn.close()
                srv.close()
                if sock.exists():
                    sock.unlink()

        t = threading.Thread(target=server, daemon=True)
        t.start()
        # wait for the socket to appear
        deadline = time.time() + 5
        while not sock.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert sock.exists(), "helper socket did not appear"

        resp = th.client_call(str(sock), {"op": "capability"}, timeout=5)
        assert resp.get("ok") is True
        assert "capability" in resp
        t.join(timeout=5)

    def test_end_to_end_unknown_op(self, tmp_path):
        sock = tmp_path / "tun.sock"

        def server():
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            if sock.exists():
                sock.unlink()
            srv.bind(str(sock))
            srv.listen(1)
            srv.settimeout(10)
            try:
                conn, _ = srv.accept()
                helper = th.Helper(KNOWN, UPS)
                th._handle_conn(conn, helper)
            except socket.timeout:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                srv.close()
                if sock.exists():
                    sock.unlink()

        t = threading.Thread(target=server, daemon=True)
        t.start()
        deadline = time.time() + 5
        while not sock.exists() and time.time() < deadline:
            time.sleep(0.02)
        resp = th.client_call(str(sock), {"op": "frobnicate"}, timeout=5)
        assert resp.get("ok") is False
        assert resp.get("error") == "unknown_op"
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# compose.tun.yaml static audit (spec 21.4 / 46 A8)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def compose_tun():
    return yaml.safe_load((ROOT / "compose.tun.yaml").read_text())


class TestComposeTun:

    def test_helper_service_exists(self, compose_tun):
        assert "vibelab-egress-tun-helper" in compose_tun["services"]

    def test_no_public_binds(self, compose_tun):
        """The helper exposes only a UNIX socket: no `ports:` at all."""
        svc = compose_tun["services"]["vibelab-egress-tun-helper"]
        assert "ports" not in svc, "TUN helper must not publish any TCP port"

    def test_capability_grant_is_minimal(self, compose_tun):
        """cap_drop ALL + cap_add exactly [NET_ADMIN]; no privileged."""
        svc = compose_tun["services"]["vibelab-egress-tun-helper"]
        assert svc.get("cap_drop") == ["ALL"]
        assert svc.get("cap_add") == ["NET_ADMIN"]
        assert svc.get("privileged") is not True

    def test_no_docker_socket(self, compose_tun):
        for v in compose_tun["services"]["vibelab-egress-tun-helper"].get("volumes", []):
            assert "docker.sock" not in str(v)

    def test_no_new_privileges(self, compose_tun):
        svc = compose_tun["services"]["vibelab-egress-tun-helper"]
        assert "no-new-privileges:true" in svc.get("security_opt", [])

    def test_tun_device_mounted(self, compose_tun):
        vols = [str(v) for v in
                compose_tun["services"]["vibelab-egress-tun-helper"].get("volumes", [])]
        assert any("/dev/net/tun" in v for v in vols)


def test_oneshot_slots_shared_by_probe_and_config(tmp_path):
    """The live Compose path feeds the same persisted credentials to both."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    env = compose["services"]["vibelab-egress-egressd"]["environment"]
    assert env["EGRESSD_ONESHOT_SLOTS_FILE"] == (
        "/etc/vibelab-egress/secrets/clients/oneshot-slots.json")
    slot_file = tmp_path / "oneshot-slots.json"
    slot_file.write_text(json.dumps({"os_0": "secret-0", "os_1": "secret-1"}))
    from egressd.probe import load_slots
    from egressd.configctl import load_oneshot_slots
    probe = load_slots(2, str(slot_file))
    config = load_oneshot_slots(str(slot_file), 2)
    assert [(s.user, s.password) for s in probe] == [
        (s.user, s.password) for s in config]
    assert [(s.user, s.password) for s in load_slots(2, None)] == [
        (s.user, s.password) for s in load_oneshot_slots(None, 2)]
    with pytest.raises(FileNotFoundError):
        load_slots(2, str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        load_oneshot_slots(str(tmp_path / "missing.json"), 2)


# ---------------------------------------------------------------------------
# egressd /v1/capabilities tun reporting (per-client, spec 21.5)
# ---------------------------------------------------------------------------
def _write_blanc(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
    ]}, sort_keys=False))


@pytest.fixture()
def egress(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    # host-main has tun in its modes (host-helper installed); openscience does not
    for cid, env, modes in (("host-main", "host", ["proxy", "tun"]),
                            ("openscience-main", "gvisor-container", ["proxy"])):
        (clients / f"{cid}.token").write_text(f"tok-{cid}\n")
        (clients / f"{cid}.profile.json").write_text(json.dumps(
            {"client_id": cid, "environment": env, "modes": modes}))
        (clients / f"{cid}.proxy-creds").write_text(f"{cid}-user:{cid}-pass\n")
    provs = tmp_path / "providers"
    provs.mkdir()
    _write_blanc(provs / "blanc.yaml")
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(provs))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_METRICS_DB", str(tmp_path / "state" / "metrics.sqlite"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    monkeypatch.setenv("EGRESSD_ONESHOT_SLOTS", "4")
    from egressd.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    return {
        "app": app, "client": TestClient(app), "tmp": tmp_path,
        "host": {"Authorization": "Bearer tok-host-main"},
        "os": {"Authorization": "Bearer tok-openscience-main"},
    }


class TestCapabilitiesTun:
    def test_host_tun_supported(self, egress):
        r = egress["client"].get("http://testserver/v1/capabilities",
                                 headers=egress["host"])
        assert r.status_code == 200
        tun = r.json()["tun"]
        assert tun["available"] is True
        assert tun["status"] == "supported"
        assert tun["backend"] == "host-helper"

    def test_openscience_tun_unsupported(self, egress):
        r = egress["client"].get("http://testserver/v1/capabilities",
                                 headers=egress["os"])
        tun = r.json()["tun"]
        assert tun["available"] is False
        assert tun["status"] == "unsupported_in_environment"
        assert tun["backend"] == "none"

    def test_mode_set_tun_consistent(self, egress):
        """The /v1/capabilities tun report must agree with `mode set tun`."""
        c, host, os_ = egress["client"], egress["host"], egress["os"]
        # host: supported -> mode set tun is accepted
        assert c.put("http://testserver/v1/default/mode", headers=host,
                     json={"mode": "tun"}).status_code == 200
        # openscience: unsupported -> explicit 400, no silent fallback
        r = c.put("http://testserver/v1/default/mode", headers=os_,
                  json={"mode": "tun"})
        assert r.status_code == 400
        assert "unsupported" in r.json()["detail"]
