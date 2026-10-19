#!/usr/bin/env python3
"""egress-tun-helper -- local privileged strict/TUN helper (A6, spec section 21).

Narrow, LOCAL-ONLY interface for network-enforced (strict) execution. A host
user's `egress-run --mode tun ROUTE -- command` obtains a route-specific
one-shot upstream from the public egressd API, then asks THIS helper (over a
local UNIX socket) to run the command in a dedicated per-run network
namespace whose egress is pinned to that route.

Why a privileged helper: creating a network namespace + TUN device requires
CAP_SYS_ADMIN for the named netns and CAP_NET_ADMIN for networking (plus
/dev/net/tun). The host systemd helper runs with those privileges, does
the network setup, then DROPS to the caller's UID/GID before exec'ing the
target. It is NOT a remote-exec API and is NOT reachable over the network.

Security invariants (spec 21.4) -- each is enforced here and unit-tested:
  * local only: bound to a UNIX socket with peer UID verification; no TCP bind.
  * caller identity from OS credentials (SO_PEERCRED), never from the body.
  * fixed protocol of operations only: `capability`, `run`, `cleanup`.
    Any other op / unknown field -> error. There is no free-form command
    beyond the caller's own argv, and no arbitrary namespace path.
  * `run` validates the route (must be a known route id or `direct`) and the
    upstream (must be one of the configured egress one-shot listeners), so the
    helper cannot be pointed at an arbitrary proxy.
  * after network setup the target runs with the caller's UID/GID.
  * per-run netns, bounded lifetime, stale netns cleaned on start and via the
    `cleanup` op.
  * audit events log op + route + outcome only -- never the command argv or
    environment by default.

In an environment without CAP_NET_ADMIN / /dev/net/tun (e.g. the container
container environment, or this sandbox) the `capability` op reports
`tun: unsupported_in_environment` and `run` fails with an explicit,
diagnosable error. There is NO silent fallback to proxy (spec 21.1/21.5).

Standard library only.
"""
from __future__ import annotations

import errno
import array
import json
import os
import grp
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_netns import (NativeNetnsBackend, NetworkSetupError, MIHOMO_PATH,
                          _job_name)

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
DEFAULT_SOCKET = "/run/vibelab-egress/tun-helper.sock"
DEFAULT_STATE_DIR = "/run/vibelab-egress/jobs"
NETNS_PREFIX = "egress-tun-"
# a per-run netns older than this with no live job is stale and gets cleaned
STALE_TTL_S = 300.0
# hard cap on a single run (seconds) -- the helper enforces a bounded lifetime
MAX_RUN_S = 1800.0
MAX_COMMAND_ARGS = 256
MAX_ARG_LEN = 4096

# route ids are `<provider>:<stable-id>` or the synthetic `direct`. A safe
# charset only (no traversal / shell metachars / whitespace). The helper does
# not own the route catalog (egressd does, and validates the route when it
# creates the one-shot session); this format check is defense-in-depth so the
# helper can never be handed an arbitrary string.
ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+:[A-Za-z0-9._\-]+$")
SAFE_ROUTES = {"direct"}

ALLOWED_OPS = {"capability", "run", "cleanup"}
# fields allowed per op, beyond "op" itself (spec 21.4: fixed protocol)
ALLOWED_FIELDS = {
    "capability": set(),
    "run": {"protocol", "route", "upstream", "command", "timeout",
            "cwd", "env", "umask"},
    "cleanup": {"older_than"},
}

# where the helper may log audit events (no secrets, no command payload)
AUDIT_PATH = os.path.join(DEFAULT_STATE_DIR, "audit.jsonl")


def _now() -> float:
    return time.time()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(event: str, **fields: Any) -> None:
    """Append an audit event. Command argv/env are NEVER logged by default;
    only op, route, outcome and bounded diagnostics."""
    try:
        os.makedirs(DEFAULT_STATE_DIR, exist_ok=True)
        rec = {"ts": _ts(), "event": event, **fields}
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        # audit must never break the helper
        pass


# ---------------------------------------------------------------------------
# capability probe (spec 21.5, A6 "capability reporting")
# ---------------------------------------------------------------------------
@dataclass
class Capability:
    tun: bool
    tun_dev: bool
    cap_net_admin: bool
    netns: bool
    reason: str = ""
    backend: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tun": self.tun,
            "tun_dev": self.tun_dev,
            "cap_net_admin": self.cap_net_admin,
            "netns": self.netns,
            "backend": self.backend,
            "reason": self.reason,
            "status": "supported" if self.tun else "unsupported_in_environment",
            "protocol": 2,
        }


def _has_cap_net_admin() -> bool:
    """Best-effort CAP_NET_ADMIN check without external tools.

    Reads CapEff from /proc/self/status; CAP_NET_ADMIN is bit 12.
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    hexv = line.split(":", 1)[1].strip()
                    eff = int(hexv, 16)
                    return bool(eff & (1 << 12))
    except (OSError, ValueError):
        pass
    return False


def _has_cap_sys_admin() -> bool:
    """ip netns add uses mount/unshare and needs CAP_SYS_ADMIN (bit 21)."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return bool(int(line.split(":", 1)[1].strip(), 16) & (1 << 21))
    except (OSError, ValueError):
        pass
    return False


def _has_cap(bit: int) -> bool:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split(":", 1)[1].strip(), 16) & (1 << bit))
    except (OSError, ValueError):
        pass
    return False


def _tun_dev_present() -> bool:
    return os.path.exists("/dev/net/tun")


def _netns_available() -> bool:
    return bool(shutil.which("ip") and _has_cap_net_admin() and
                _has_cap_sys_admin())


def probe_capability() -> Capability:
    """Report whether strict/TUN is usable in THIS environment (spec 21.5)."""
    tun_dev = _tun_dev_present()
    cap = _has_cap_net_admin()
    netns = _netns_available()
    missing = NativeNetnsBackend.prerequisites(MIHOMO_PATH)
    tun = tun_dev and cap and netns and _has_cap(6) and _has_cap(7) and not missing
    if tun:
        reason, backend = "ok", "host-netns+mihomo"
    elif not tun_dev:
        reason, backend = "missing /dev/net/tun", "none"
    elif not cap:
        reason, backend = "missing CAP_NET_ADMIN", "none"
    elif not _has_cap_sys_admin():
        reason, backend = "ip netns add requires CAP_SYS_ADMIN", "none"
    elif not (_has_cap(6) and _has_cap(7)):
        reason, backend = "missing CAP_SETGID/CAP_SETUID", "none"
    else:
        reason, backend = "missing host TUN prerequisites: " + ", ".join(missing), "none"
    return Capability(tun=tun, tun_dev=tun_dev, cap_net_admin=cap,
                      netns=netns, reason=reason, backend=backend)


# ---------------------------------------------------------------------------
# request validation (pure -- unit tested)
# ---------------------------------------------------------------------------
class ValidationError(Exception):
    pass


def validate_capability(req: dict[str, Any]) -> None:
    _check_shape(req, "capability")


def validate_cleanup(req: dict[str, Any]) -> None:
    _check_shape(req, "cleanup")
    if "older_than" in req:
        v = req["older_than"]
        if not isinstance(v, (int, float)) or v < 0:
            raise ValidationError("'older_than' must be a non-negative number")


def validate_run(req: dict[str, Any], known_routes: set[str],
                 allowed_upstreams: set[str]) -> None:
    """Enforce the fixed `run` contract (spec 21.4).

    * route: a known route id or `direct` (never an arbitrary string).
    * upstream: only for a non-direct route; must be one of the configured
      egress one-shot listeners (host:port) -- the helper cannot be pointed
      at an arbitrary proxy.
    * command: a non-empty list of bounded strings (the caller's own argv).
    * timeout: bounded by MAX_RUN_S.
    """
    _check_shape(req, "run")
    if req.get("protocol") != 2:
        raise ValidationError("run requires host-native protocol 2")

    route = req.get("route")
    if not isinstance(route, str) or not route:
        raise ValidationError("'route' is required (a route id or 'direct')")
    # defense-in-depth: the route must be `direct` or a well-formed
    # `<provider>:<stable-id>` (safe charset). egressd already validated the
    # route when it created the one-shot session; this stops the helper from
    # ever receiving an arbitrary string (traversal / shell / whitespace).
    if route != "direct" and not ROUTE_ID_RE.fullmatch(route):
        raise ValidationError(f"malformed route id {route!r}")
    # optional stricter check: if the helper was told the exact catalog,
    # enforce membership too.
    if known_routes and route != "direct" and route not in known_routes:
        raise ValidationError(f"unknown route {route!r}")

    if route == "direct":
        # direct+tun: isolated direct path, no upstream (spec 21.3)
        if "upstream" in req:
            raise ValidationError("'upstream' must not be set for direct")
    else:
        up = req.get("upstream")
        if not isinstance(up, dict):
            raise ValidationError("'upstream' is required for a non-direct route")
        host = up.get("host")
        port = up.get("port")
        if not isinstance(host, str) or not host:
            raise ValidationError("upstream.host is required")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValidationError("upstream.port must be an integer 1..65535")
        if f"{host}:{port}" not in allowed_upstreams:
            raise ValidationError(
                f"upstream {host}:{port} is not a configured egress one-shot listener")
        # username/password are opaque to the helper (used only to reach the
        # listener); they are not validated as secrets, only bounded.
        for k in ("username", "password"):
            if k in up and not isinstance(up[k], str):
                raise ValidationError(f"upstream.{k} must be a string")

    cmd = req.get("command")
    if not isinstance(cmd, list) or not cmd:
        raise ValidationError("'command' must be a non-empty list")
    if len(cmd) > MAX_COMMAND_ARGS:
        raise ValidationError(f"too many command args (max {MAX_COMMAND_ARGS})")
    for a in cmd:
        if not isinstance(a, str) or len(a) > MAX_ARG_LEN:
            raise ValidationError("command args must be bounded strings")

    if "timeout" in req:
        t = req["timeout"]
        if not isinstance(t, (int, float)) or t <= 0 or t > MAX_RUN_S:
            raise ValidationError(f"'timeout' must be in (0, {MAX_RUN_S}]")

    cwd = req.get("cwd")
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > 4096:
        raise ValidationError("'cwd' must be a bounded absolute path")
    env = req.get("env")
    if not isinstance(env, dict) or len(env) > 512:
        raise ValidationError("'env' must be an object of at most 512 entries")
    for key, value in env.items():
        if (not isinstance(key, str) or not isinstance(value, str) or
                not key or "=" in key or "\x00" in key or "\x00" in value or
                len(key) > 256 or len(value) > 16384):
            raise ValidationError("invalid or oversized environment entry")
    mask = req.get("umask")
    if not isinstance(mask, int) or mask < 0 or mask > 0o777:
        raise ValidationError("'umask' must be an integer 0..0777")


def _check_shape(req: dict[str, Any], op: str) -> None:
    if not isinstance(req, dict):
        raise ValidationError("request must be a JSON object")
    if req.get("op") != op:
        raise ValidationError(f"op mismatch: expected {op!r}")
    unknown = set(req) - {"op"} - ALLOWED_FIELDS[op]
    if unknown:
        raise ValidationError(f"unknown field(s) for {op}: {sorted(unknown)}")


# ---------------------------------------------------------------------------
# netns backend (the privileged network setup)
# ---------------------------------------------------------------------------
NetnsBackend = NativeNetnsBackend


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------
class Helper:
    def __init__(self, known_routes: set[str], allowed_upstreams: set[str],
                 backend: NetnsBackend | None = None,
                 capability: Callable[[], Capability] | None = None):
        self.known_routes = set(known_routes)
        self.allowed_upstreams = set(allowed_upstreams)
        self.backend = backend
        self._backend_lock = threading.Lock()
        self._probe = capability or probe_capability

    def _backend(self) -> NetnsBackend:
        with self._backend_lock:
            if self.backend is None:
                self.backend = NetnsBackend(
                    gateway=os.environ.get("VIBELAB_EGRESS_DOCKER_GATEWAY",
                                           "172.17.0.1"))
            return self.backend

    # -- ops ----------------------------------------------------------------
    def op_capability(self, req: dict[str, Any]) -> dict[str, Any]:
        validate_capability(req)
        cap = self._probe()
        audit("capability", tun=cap.tun, reason=cap.reason)
        return {"ok": True, "capability": cap.to_dict()}

    def op_cleanup(self, req: dict[str, Any], peer_uid: int = 0) -> dict[str, Any]:
        validate_cleanup(req)
        if peer_uid != 0:
            return {"ok": False, "error": "forbidden"}
        ttl = req.get("older_than", STALE_TTL_S)
        removed = self._backend().cleanup_stale(ttl=ttl)
        audit("cleanup", removed=len(removed))
        return {"ok": True, "removed": removed}

    def op_run(self, req: dict[str, Any], peer_uid: int, peer_gid: int,
               *, peer_groups: list[int] | None = None,
               stdio: tuple[int, int, int] | None = None,
               conn: socket.socket | None = None) -> dict[str, Any]:
        validate_run(req, self.known_routes, self.allowed_upstreams)
        route = req["route"]
        cap = self._probe()
        if not cap.tun:
            # explicit, diagnosable failure -- NO silent proxy fallback
            audit("run", route=route, outcome="unsupported", reason=cap.reason)
            return {"ok": False, "error": "unsupported_in_environment",
                    "capability": cap.to_dict()}

        name = _job_name()
        backend = self._backend()
        try:
            backend.create(name)
            if route == "direct":
                backend.pin_direct(name)
            else:
                backend.pin_route(name, req["upstream"])
            timeout = req.get("timeout", 300.0)
            audit("run", route=route, netns=name, uid=peer_uid,
                  outcome="started")
            rc = backend.exec_in(
                name, req["command"], peer_uid, peer_gid, timeout,
                groups=peer_groups or [], env=req["env"], cwd=req["cwd"],
                umask=req["umask"], stdio=stdio or (0, 1, 2),
                cancel_sock=conn)
            audit("run", route=route, netns=name, outcome="done", exit_code=rc)
            return {"ok": True, "exit_code": rc}
        except (OSError, subprocess.CalledProcessError, NetworkSetupError) as e:
            audit("run", route=route, netns=name, outcome="error",
                  error=str(e)[:200])
            return {"ok": False, "error": "netns_setup_failed",
                    "detail": str(e)[:200]}
        finally:
            backend.destroy(name)


# ---------------------------------------------------------------------------
# UNIX-socket server (local only)
# ---------------------------------------------------------------------------
def read_frame(sock: socket.socket) -> dict[str, Any] | None:
    """Read one length-prefixed JSON frame. None on clean EOF."""
    hdr = _recv_exact(sock, 4)
    if hdr is None:
        return None
    (length,) = struct.unpack(">I", hdr)
    if length > 1_000_000:  # 1 MiB cap -- a run request is tiny
        raise ValidationError("frame too large")
    body = _recv_exact(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def read_frame_with_fds(sock: socket.socket) -> tuple[dict[str, Any] | None, list[int]]:
    """Read the fixed request frame and at most stdin/stdout/stderr FDs."""
    head, ancillary, flags, _ = sock.recvmsg(4, socket.CMSG_SPACE(16 * 4))
    if not head:
        return None, []
    fds: list[int] = []
    for level, typ, payload in ancillary:
        if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(payload[:len(payload) - len(payload) % values.itemsize])
            fds.extend(values)
    if flags & socket.MSG_CTRUNC:
        for fd in fds:
            os.close(fd)
        raise ValidationError("too many passed file descriptors")
    if len(fds) > 3:
        for fd in fds:
            os.close(fd)
        raise ValidationError("expected at most three stdio descriptors")
    header = head + (_recv_exact(sock, 4 - len(head)) or b"")
    if len(header) != 4:
        for fd in fds:
            os.close(fd)
        return None, []
    (length,) = struct.unpack(">I", header)
    if length < 2 or length > 1_000_000:
        for fd in fds:
            os.close(fd)
        raise ValidationError("invalid request frame size")
    body = _recv_exact(sock, length)
    if body is None:
        for fd in fds:
            os.close(fd)
        return None, []
    try:
        req = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        for fd in fds:
            os.close(fd)
        raise ValidationError("invalid JSON request") from None
    return req, fds


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def write_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    body = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def peer_credentials(sock: socket.socket) -> tuple[int, int]:
    """Caller UID/GID from OS credentials (SO_PEERCRED), not the body."""
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 128)
    # struct ucred { pid_t pid; uid_t uid; gid_t gid; }
    pid, uid, gid = struct.unpack("iii", data[:12])
    return uid, gid


def peer_groups(sock: socket.socket) -> list[int]:
    """Read the connecting process's actual supplementary groups."""
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, gid = struct.unpack("iii", data)
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Groups:"):
            return [int(item) for item in line.split(":", 1)[1].split()]
    raise ValidationError("cannot read caller groups")


def serve(sock_path: str, known_routes: set[str],
          allowed_upstreams: set[str],
          helper: Helper | None = None) -> None:
    d = os.path.dirname(sock_path)
    if d:
        os.makedirs(d, exist_ok=True)
        group_id = grp.getgrnam("vibelab-egress-tun").gr_gid
        os.chown(d, 0, group_id)
        os.chmod(d, 0o755)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    os.chown(sock_path, 0, group_id)
    # Every host UID may request a run. SO_PEERCRED supplies the actual caller
    # identity, and the workload is executed only after dropping to that UID.
    os.chmod(sock_path, 0o666)
    server.listen(16)
    audit("listen", socket=sock_path)
    instance = helper or Helper(known_routes, allowed_upstreams)
    active = threading.BoundedSemaphore(16)
    if probe_capability().tun:
        instance._backend().cleanup_stale(ttl=0)

    def worker(conn: socket.socket) -> None:
        try:
            _handle_conn(conn, instance)
        except (ValidationError, OSError, subprocess.CalledProcessError,
                NetworkSetupError, json.JSONDecodeError) as e:
            try:
                write_frame(conn, {"ok": False, "error": "bad_request",
                                   "detail": str(e)[:200]})
            except OSError:
                pass
        finally:
            conn.close()
            active.release()

    try:
        while True:
            conn, _ = server.accept()
            if not active.acquire(blocking=False):
                try:
                    write_frame(conn, {"ok": False, "error": "busy"})
                except OSError:
                    pass
                conn.close()
                continue
            threading.Thread(target=worker, args=(conn,), daemon=True).start()
    finally:
        server.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


def _handle_conn(conn: socket.socket, helper: Helper) -> None:
    uid, gid = peer_credentials(conn)
    while True:
        req, fds = read_frame_with_fds(conn)
        if req is None:
            return
        try:
            if not isinstance(req, dict):
                raise ValidationError("request must be an object")
            op = req.get("op")
            if op == "capability":
                if fds:
                    raise ValidationError("capability accepts no descriptors")
                resp = helper.op_capability(req)
            elif op == "cleanup":
                if fds:
                    raise ValidationError("cleanup accepts no descriptors")
                resp = helper.op_cleanup(req, uid)
            elif op == "run":
                if len(fds) != 3:
                    raise ValidationError("run requires stdin/stdout/stderr descriptors")
                resp = helper.op_run(req, uid, gid,
                                     peer_groups=peer_groups(conn),
                                     stdio=(fds[0], fds[1], fds[2]), conn=conn)
            else:
                resp = {"ok": False, "error": "unknown_op",
                        "allowed": sorted(ALLOWED_OPS)}
        finally:
            for fd in fds:
                os.close(fd)
        write_frame(conn, resp)
        if op != "capability":
            # run/cleanup are one-shot for this connection in the thin-client
            # contract; capability may be polled repeatedly.
            return


# ---------------------------------------------------------------------------
# client (used by egress-run) + CLI
# ---------------------------------------------------------------------------
def client_call(sock_path: str, req: dict[str, Any],
                timeout: float = 30.0) -> dict[str, Any]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        write_frame(s, req)
        resp = read_frame(s)
        return resp or {"ok": False, "error": "no_response"}
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="egress-tun-helper")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--known-routes", default="",
                    help="comma-separated route ids (or file path) the helper may pin")
    ap.add_argument("--allowed-upstreams", default="127.0.0.1:1081",
                    help="comma-separated host:port one-shot listeners "
                         "(clients send their own PUBLISHED one-shot port, "
                         "not the in-container listener port)")
    ap.add_argument("--probe", action="store_true",
                    help="print the capability probe and exit")
    args = ap.parse_args(argv)

    if args.probe:
        print(json.dumps(probe_capability().to_dict(), indent=2))
        return 0

    # known routes: a path to a file (one route per line) or a comma list
    known: set[str] = set()
    kr = args.known_routes.strip()
    if kr:
        if os.path.isfile(kr):
            known = {ln.strip() for ln in open(kr) if ln.strip()}
        else:
            known = {r.strip() for r in kr.split(",") if r.strip()}
    upstreams = {u.strip() for u in args.allowed_upstreams.split(",") if u.strip()}

    serve(args.socket, known, upstreams)
    return 0


if __name__ == "__main__":
    sys.exit(main())
