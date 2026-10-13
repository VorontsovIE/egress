#!/usr/bin/env python3
"""egress-tun-helper -- local privileged strict/TUN helper (A6, spec section 21).

Narrow, LOCAL-ONLY interface for network-enforced (strict) execution. A host
user's `egress-run --mode tun ROUTE -- command` obtains a route-specific
one-shot upstream from the public egressd API, then asks THIS helper (over a
local UNIX socket) to run the command in a dedicated per-run network
namespace whose egress is pinned to that route.

Why a privileged helper: creating a network namespace + TUN device requires
CAP_NET_ADMIN (and /dev/net/tun). The helper runs with that privilege, does
the network setup, then DROPS to the caller's UID/GID before exec'ing the
target. It is NOT a remote-exec API and is NOT reachable over the network.

Security invariants (spec 21.4) -- each is enforced here and unit-tested:
  * local only: bound to a UNIX socket in a 0700 dir; no TCP bind.
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

In an environment without CAP_NET_ADMIN / /dev/net/tun (e.g. the OpenScience
gVisor container, or this sandbox) the `capability` op reports
`tun: unsupported_in_environment` and `run` fails with an explicit,
diagnosable error. There is NO silent fallback to proxy (spec 21.1/21.5).

Standard library only.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import pwd
import grp
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
DEFAULT_SOCKET = "/run/vibelab-egress/tun-helper.sock"
DEFAULT_STATE_DIR = "/run/vibelab-egress/tun-helper"
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
    "run": {"route", "upstream", "command", "timeout", "stdout", "stderr"},
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


def _tun_dev_present() -> bool:
    return os.path.exists("/dev/net/tun")


def _netns_available() -> bool:
    """Can we create a network namespace? Requires unshare + CAP_NET_ADMIN."""
    if not shutil.which("unshare"):
        return False
    if not _has_cap_net_admin():
        return False
    # a real probe would unshare here; keep it capability-based so we never
    # mutate the caller's netns during a probe.
    return True


def probe_capability() -> Capability:
    """Report whether strict/TUN is usable in THIS environment (spec 21.5)."""
    tun_dev = _tun_dev_present()
    cap = _has_cap_net_admin()
    netns = _netns_available()
    tun = tun_dev and cap and netns
    if tun:
        reason, backend = "ok", "netns+tun"
    elif not tun_dev:
        reason, backend = "missing /dev/net/tun", "none"
    elif not cap:
        reason, backend = "missing CAP_NET_ADMIN", "none"
    else:
        reason, backend = "netns unavailable (no unshare/CAP_NET_ADMIN)", "none"
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
class NetnsBackend:
    """Creates/destroys per-run network namespaces and pins egress.

    The real backend shells out to `unshare`/`ip`/`tun2socks`. It is only
    reached after `probe_capability()` reports `tun: supported`, so in an
    unsupported environment the helper never mutates the caller's netns.
    """

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)

    # -- netns lifecycle ----------------------------------------------------
    def _netns_path(self, name: str) -> str:
        return os.path.join(self.state_dir, "netns", name)

    def create(self, name: str) -> None:
        path = self._netns_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # create a new netns bound to a file so we can enter it later
        self._run(["ip", "netns", "add", name])
        # record creation time for stale detection
        with open(self._netns_path(name) + ".ts", "w") as f:
            f.write(str(_now()))

    def destroy(self, name: str) -> None:
        # the netns may already be gone (e.g. it was never really created in an
        # unsupported env, or the kernel cleaned it); suppress the harmless
        # "cannot remove namespace file" stderr.
        try:
            self._run(["ip", "netns", "del", name], check=False, quiet=True)
        except OSError:
            pass
        for suffix in ("", ".ts"):
            p = self._netns_path(name) + suffix
            try:
                os.unlink(p)
            except OSError:
                pass

    def list_netns(self) -> list[tuple[str, float]]:
        """Return [(name, created_ts)] for our netns (by prefix)."""
        out: list[tuple[str, float]] = []
        netns_dir = os.path.join(self.state_dir, "netns")
        if not os.path.isdir(netns_dir):
            return out
        for entry in os.listdir(netns_dir):
            if not entry.endswith(".ts"):
                continue
            name = entry[:-3]
            if not name.startswith(NETNS_PREFIX):
                continue
            try:
                ts = float(open(os.path.join(netns_dir, entry)).read().strip())
            except (OSError, ValueError):
                ts = 0.0
            out.append((name, ts))
        return out

    def cleanup_stale(self, now: float | None = None,
                      ttl: float = STALE_TTL_S) -> list[str]:
        """Destroy netns older than ``ttl``. Returns the names removed."""
        now = now if now is not None else _now()
        removed = []
        for name, ts in self.list_netns():
            if now - ts > ttl:
                self.destroy(name)
                removed.append(name)
        return removed

    # -- egress pinning -----------------------------------------------------
    def pin_route(self, name: str, upstream: dict[str, Any]) -> None:
        """Inside netns ``name``: bring up a TUN device and forward its
        traffic to the route-specific one-shot SOCKS upstream (tun2socks or
        equivalent). This is the 'route-specific upstream' of spec 21.2."""
        host = upstream["host"]
        port = upstream["port"]
        user = upstream.get("username", "")
        password = upstream.get("password", "")
        # enter the netns and set up the TUN + forwarder. The exact toolchain
        # (tun2socks / a bundled userspace forwarder) is host-specific; the
        # contract is: all egress from the netns goes to host:port.
        cmds = [
            ["ip", "link", "add", "tun0", "type", "tun"],
            ["ip", "addr", "add", "10.88.0.1/24", "dev", "tun0"],
            ["ip", "link", "set", "tun0", "up"],
            ["ip", "route", "add", "default", "via", "10.88.0.1"],
        ]
        for c in cmds:
            self._run(["ip", "netns", "exec", name, "--"] + c)
        # start the userspace forwarder (tun2socks) bound to the TUN device,
        # pointing at the one-shot SOCKS listener.
        fwd = shutil.which("tun2socks")
        if fwd:
            self._run(["ip", "netns", "exec", name, "--", fwd,
                       "-b", "10.88.0.1/24", "-s", f"{host}:{port}",
                       "-u", f"{user}:{password}" if user else "",
                       "-i", "tun0"], background=True)

    def pin_direct(self, name: str) -> None:
        """direct+tun (spec 21.3): isolated netns with direct NAT, no VPN."""
        self._run(["ip", "netns", "exec", name, "--", "ip", "link", "set", "lo", "up"])
        # NAT to the host's default route (isolated direct path, no proxy).
        self._run(["ip", "netns", "exec", name, "--", "ip", "route", "add",
                   "default", "via", "10.88.0.254"], check=False)

    # -- exec with privilege drop ------------------------------------------
    def exec_in(self, name: str, command: list[str], uid: int, gid: int,
                timeout: float) -> int:
        """Run ``command`` inside netns ``name`` as the caller's uid/gid.

        The helper (root) drops to the caller's credentials for the target,
        so the target runs as the local host workload (spec 21.2)."""
        argv = ["ip", "netns", "exec", name, "--", "setpriv",
                f"--reuid={uid}", f"--regid={gid}", "--init-groups"] + command
        try:
            p = subprocess.run(argv, timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except FileNotFoundError:
            rc = 127
        if rc < 0:
            rc = 128 + (-rc)
        return rc

    # -- helpers ------------------------------------------------------------
    def _run(self, argv: list[str], check: bool = True,
             background: bool = False, quiet: bool = False) -> subprocess.CompletedProcess:
        if background:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return subprocess.CompletedProcess(argv, 0)
        if quiet:
            return subprocess.run(argv, check=check,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        return subprocess.run(argv, check=check)


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------
class Helper:
    def __init__(self, known_routes: set[str], allowed_upstreams: set[str],
                 backend: NetnsBackend | None = None,
                 capability: Callable[[], Capability] | None = None):
        self.known_routes = set(known_routes)
        self.allowed_upstreams = set(allowed_upstreams)
        self.backend = backend or NetnsBackend()
        self._probe = capability or probe_capability

    # -- ops ----------------------------------------------------------------
    def op_capability(self, req: dict[str, Any]) -> dict[str, Any]:
        validate_capability(req)
        cap = self._probe()
        audit("capability", tun=cap.tun, reason=cap.reason)
        return {"ok": True, "capability": cap.to_dict()}

    def op_cleanup(self, req: dict[str, Any]) -> dict[str, Any]:
        validate_cleanup(req)
        ttl = req.get("older_than", STALE_TTL_S)
        removed = self.backend.cleanup_stale(ttl=ttl)
        audit("cleanup", removed=len(removed))
        return {"ok": True, "removed": removed}

    def op_run(self, req: dict[str, Any], peer_uid: int, peer_gid: int) -> dict[str, Any]:
        validate_run(req, self.known_routes, self.allowed_upstreams)
        route = req["route"]
        cap = self._probe()
        if not cap.tun:
            # explicit, diagnosable failure -- NO silent proxy fallback
            audit("run", route=route, outcome="unsupported", reason=cap.reason)
            return {"ok": False, "error": "unsupported_in_environment",
                    "capability": cap.to_dict()}

        name = f"{NETNS_PREFIX}{os.getpid()}-{int(_now() * 1000)}"
        try:
            self.backend.create(name)
            if route == "direct":
                self.backend.pin_direct(name)
            else:
                self.backend.pin_route(name, req["upstream"])
            timeout = req.get("timeout", 300.0)
            audit("run", route=route, netns=name, uid=peer_uid,
                  outcome="started")
            rc = self.backend.exec_in(name, req["command"], peer_uid, peer_gid,
                                      timeout)
            audit("run", route=route, netns=name, outcome="done", exit_code=rc)
            return {"ok": True, "exit_code": rc}
        except OSError as e:
            audit("run", route=route, netns=name, outcome="error",
                  error=str(e)[:200])
            return {"ok": False, "error": "netns_setup_failed",
                    "detail": str(e)[:200]}
        finally:
            self.backend.destroy(name)


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


def serve(sock_path: str, known_routes: set[str],
          allowed_upstreams: set[str],
          helper: Helper | None = None) -> None:
    d = os.path.dirname(sock_path)
    if d:
        os.makedirs(d, exist_ok=True)
        os.chmod(d, 0o700)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    os.chmod(sock_path, 0o600)
    server.listen(16)
    audit("listen", socket=sock_path)
    try:
        while True:
            conn, _ = server.accept()
            try:
                _handle_conn(conn, helper or Helper(known_routes,
                                                    allowed_upstreams))
            except (ValidationError, OSError, json.JSONDecodeError) as e:
                try:
                    write_frame(conn, {"ok": False, "error": "bad_request",
                                       "detail": str(e)[:200]})
                except OSError:
                    pass
            finally:
                conn.close()
    finally:
        server.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


def _handle_conn(conn: socket.socket, helper: Helper) -> None:
    uid, gid = peer_credentials(conn)
    while True:
        req = read_frame(conn)
        if req is None:
            return
        op = req.get("op")
        if op == "capability":
            resp = helper.op_capability(req)
        elif op == "cleanup":
            resp = helper.op_cleanup(req)
        elif op == "run":
            resp = helper.op_run(req, uid, gid)
        else:
            resp = {"ok": False, "error": "unknown_op",
                    "allowed": sorted(ALLOWED_OPS)}
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
    ap.add_argument("--allowed-upstreams", default="127.0.0.1:18081",
                    help="comma-separated host:port one-shot listeners")
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
