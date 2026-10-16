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
  * host-native systemd unit audit: bounded capabilities, local UNIX socket,
    no Docker socket, no-new-privileges;
  * egressd /v1/capabilities reports tun per-client (supported vs
    unsupported_in_environment) consistently with the mode-set check.

Run:  python -m pytest tests/integration/test_a6.py -v
"""
from __future__ import annotations

import importlib.util
import array
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
        assert cap["backend"] in ("none", "host-netns+mihomo")

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
        req = {"op": "run", "protocol": 2, "route": "direct", "command": ["true"],
               "cwd": "/tmp", "env": {}, "umask": 0o022}
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
        resp = h.op_run({"op": "run", "protocol": 2, "route": "direct",
                         "command": ["true"], "cwd": "/tmp", "env": {}, "umask": 0o022},
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

        backend = FailingBackend.__new__(FailingBackend)
        backend.destroy = lambda name: None
        cap = th.Capability(True, True, True, True, "ok", "host-netns+mihomo")
        h = th.Helper(KNOWN, UPS, backend=backend, capability=lambda: cap)
        resp = h.op_run({"op": "run", "protocol": 2, "route": "direct",
                         "command": ["true"], "cwd": "/tmp", "env": {}, "umask": 0o022},
                        1000, 1000)
        assert resp["ok"] is False
        assert resp["error"] == "netns_setup_failed"
        assert "ip" in resp["detail"]


# ---------------------------------------------------------------------------
# stale netns cleanup (file-based, no privileges needed)
# ---------------------------------------------------------------------------
class TestStaleCleanup:
    def test_cleanup_only_owned_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(th.NativeNetnsBackend, "__init__", lambda self, **kw: setattr(self, "state_dir", tmp_path))
        backend = th.NetnsBackend(state_dir=tmp_path)
        backend.jobs = {}
        removed = []
        monkeypatch.setattr(backend, "destroy", lambda name: removed.append(name))
        old = tmp_path / "egress-tun-abcdef012345"
        fresh = tmp_path / "egress-tun-abcdef012346"
        other = tmp_path / "other-ns"
        for directory, age in ((old, 9999), (fresh, 5), (other, 9999)):
            directory.mkdir()
            (directory / "state.json").write_text(json.dumps({"created": time.time()-age}))
        assert backend.cleanup_stale(ttl=300) == [old.name]
        assert removed == [old.name]

    def test_cleanup_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(th.NativeNetnsBackend, "__init__", lambda self, **kw: setattr(self, "state_dir", tmp_path))
        backend = th.NetnsBackend(state_dir=tmp_path)
        backend.jobs = {}
        assert backend.cleanup_stale() == []


# ---------------------------------------------------------------------------
# UNIX-socket protocol (length-prefixed JSON, SO_PEERCRED identity)
# ---------------------------------------------------------------------------
class TestSocketProtocol:
    def test_protocol_v2_passes_stdio_and_os_identity(self):
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        read_fd, write_fd = os.pipe()
        null_fd = os.open(os.devnull, os.O_RDONLY)
        seen = {}

        class RecordingHelper:
            def op_run(self, req, uid, gid, *, peer_groups, stdio, conn):
                seen.update(req=req, uid=uid, gid=gid, groups=peer_groups)
                os.write(stdio[1], b"through-caller-stdout")
                return {"ok": True, "exit_code": 7}

        thread = threading.Thread(target=th._handle_conn,
                                  args=(server, RecordingHelper()), daemon=True)
        thread.start()
        req = {"op": "run", "protocol": 2, "route": "direct",
               "command": ["true"], "cwd": "/tmp", "env": {}, "umask": 0o022}
        body = json.dumps(req).encode()
        fds = array.array("i", [null_fd, write_fd, write_fd])
        try:
            client.sendmsg([struct.pack(">I", len(body)) + body],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds.tobytes())])
            assert th.read_frame(client) == {"ok": True, "exit_code": 7}
            assert seen["uid"] == os.getuid()
            assert seen["gid"] == os.getgid()
            assert seen["req"]["cwd"] == "/tmp"
            assert os.read(read_fd, 1024) == b"through-caller-stdout"
        finally:
            client.close()
            server.close()
            os.close(read_fd)
            os.close(write_fd)
            os.close(null_fd)
            thread.join(timeout=2)

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


# Host-native unit static audit (spec 21.4 / 46 A8).
class TestHostTunUnit:
    @pytest.fixture
    def unit(self):
        return (ROOT / "host/units/vibelab-egress-tun-helper.service").read_text()

    def test_local_service_and_bounded_capabilities(self, unit):
        assert "User=root" in unit
        assert "Group=vibelab-egress-tun" in unit
        assert "CAP_SYS_ADMIN CAP_NET_ADMIN CAP_SETUID CAP_SETGID" in unit
        assert "NoNewPrivileges=true" in unit
        assert "--socket /run/vibelab-egress/tun-helper.sock" in unit
        assert "docker.sock" not in unit

    def test_host_namespace_and_drop_before_exec(self, unit):
        source = (ROOT / "host/strict-helper/native_netns.py").read_text()
        assert "--reuid={uid}" in source
        assert "--regid={gid}" in source
        assert "--bounding-set=-all" in source
        assert "ProtectHome=false" in unit
        assert "ProtectSystem=false" in unit


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
