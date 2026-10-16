"""egress-run — local command execution through a route (spec section 30.2).

Usage:
    egress-run --default -- command ...
    egress-run ROUTE -- command ...
    egress-run direct -- command ...
    egress-run --mode proxy ROUTE -- command ...

Security invariant (spec section 1.1): the target command is executed
LOCALLY in the same execution environment where egress-run runs. The
backend API is only used to obtain data-plane capabilities (default proxy
credentials, or a one-shot session for an explicit route). The command
argv is never sent to the backend.

Guarantees:
  * argv is passed as a list (no shell re-parse);
  * the child's exit status is preserved;
  * SIGINT/SIGTERM are proxied to the child;
  * one-shot sessions are closed on exit (finally);
  * an explicit route/mode never changes the persistent default;
  * concurrent invocations are independent (no shared state).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import NoReturn

from .api import ApiError, EgressApi
from .config import ConfigError, load_config

EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_UNSUPPORTED = 6

# Destinations that must never go through the proxy (mirrors the backend
# hard-direct list; keeps localhost/Docker traffic local).
NO_PROXY = "localhost,127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,169.254.0.0/16"


def _die(code: int, msg: str) -> NoReturn:
    print(f"egress-run: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_args(argv: list[str]) -> tuple[dict, list[str]]:
    """Split global flags from the target command at the first `--`."""
    opts = {"route": None, "mode": None, "default": False, "config": None,
            "timeout": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            if not opts["default"] and opts["route"] is None:
                _die(EXIT_USAGE,
                     "usage: egress-run [--mode proxy|tun] [--default | ROUTE] -- command ...")
            return opts, argv[i + 1:]
        if a == "--default":
            opts["default"] = True
        elif a == "--mode":
            i += 1
            if i >= len(argv):
                _die(EXIT_USAGE, "--mode requires a value (proxy|tun)")
            if argv[i] not in ("proxy", "tun"):
                _die(EXIT_USAGE, f"invalid --mode {argv[i]!r} (proxy|tun)")
            opts["mode"] = argv[i]
        elif a == "--config":
            i += 1
            if i >= len(argv):
                _die(EXIT_USAGE, "--config requires a path")
            opts["config"] = argv[i]
        elif a == "--timeout":
            i += 1
            if i >= len(argv):
                _die(EXIT_USAGE, "--timeout requires a value (seconds)")
            try:
                opts["timeout"] = float(argv[i])
            except ValueError:
                _die(EXIT_USAGE, f"invalid --timeout {argv[i]!r}")
        elif a.startswith("--"):
            _die(EXIT_USAGE, f"unknown option {a!r}")
        else:
            if opts["route"] is not None or opts["default"]:
                _die(EXIT_USAGE, "at most one route (or --default) may be given")
            opts["route"] = a
        i += 1
    if not opts["default"] and opts["route"] is None:
        _die(EXIT_USAGE, "usage: egress-run [--mode proxy|tun] [--default | ROUTE] -- command ...")
    return opts, []


def _proxy_env(host: str, port: int, user: str, password: str) -> dict[str, str]:
    url = f"socks5h://{user}:{password}@{host}:{port}"
    return {
        "http_proxy": url,
        "https_proxy": url,
        "all_proxy": url,
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "ALL_PROXY": url,
        "no_proxy": NO_PROXY,
        "NO_PROXY": NO_PROXY,
    }


def _run_local(env: dict[str, str], command: list[str]) -> int:
    """Run the command locally, preserving exit status and signals."""
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("http_proxy", "https_proxy", "all_proxy",
                              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                              "no_proxy", "NO_PROXY")}
    child_env.update(env)
    try:
        proc = subprocess.Popen(command, env=child_env)
    except FileNotFoundError:
        _die(EXIT_USAGE, f"command not found: {command[0]!r}")
    except OSError as e:
        _die(EXIT_USAGE, f"cannot start {command[0]!r}: {e}")

    def _forward(signum, frame):
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)
    rc = proc.wait()
    # subprocess returns a negative value when the child is killed by a
    # signal (e.g. -15 for SIGTERM). Normalize to the conventional
    # 128+signum (143) so the caller sees the standard shell convention.
    if rc < 0:
        rc = 128 + (-rc)
    return rc


def _tun_helper_call(sock_path: str, req: dict, timeout: float) -> dict:
    """Talk to the local strict/TUN helper over its UNIX socket (A6).

    The helper is a local-only privileged helper (spec 21.4); the command
    argv is passed to it but it runs the command LOCALLY as the caller's
    UID/GID after setting up a per-run netns. It is not a remote-exec API.
    """
    import array
    import json
    import socket
    import struct

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        body = json.dumps(req).encode("utf-8")
        frame = struct.pack(">I", len(body)) + body
        if req.get("op") == "run":
            stdio = array.array("i", (0, 1, 2))
            sent = s.sendmsg([frame], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                        stdio.tobytes())])
            if sent < len(frame):
                s.sendall(frame[sent:])
        else:
            s.sendall(frame)
        # read length-prefixed response
        hdr = b""
        while len(hdr) < 4:
            chunk = s.recv(4 - len(hdr))
            if not chunk:
                break
            hdr += chunk
        if len(hdr) < 4:
            return {"ok": False, "error": "no_response"}
        (length,) = struct.unpack(">I", hdr)
        if length < 2 or length > 1024 * 1024:
            return {"ok": False, "error": "invalid_response"}
        data = b""
        while len(data) < length:
            chunk = s.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) != length:
            return {"ok": False, "error": "no_response"}
        response = json.loads(data.decode("utf-8"))
        return response if isinstance(response, dict) else {"ok": False, "error": "invalid_response"}
    except (ValueError, UnicodeError):
        return {"ok": False, "error": "invalid_response"}
    except FileNotFoundError:
        return {"ok": False, "error": "helper_not_running",
                "detail": f"tun helper socket not found: {sock_path}"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "helper_not_running",
                "detail": f"tun helper socket not accepting: {sock_path}"}
    except (socket.timeout, OSError) as e:
        return {"ok": False, "error": "helper_error", "detail": str(e)}
    finally:
        s.close()


def _run_tun(cfg, api: EgressApi, route: str, command: list[str],
             timeout: float | None) -> int:
    """Execute ``command`` in strict/TUN mode via the local helper (A6).

    Flow (spec 21.2): obtain a route-specific one-shot upstream from the
    public egressd API, then ask the LOCAL helper to run the command in a
    per-run netns pinned to that upstream. For ``direct`` there is no
    upstream (isolated direct path, spec 21.3).

    There is NO silent fallback to proxy: any failure here is reported
    explicitly (spec 21.1/21.5).
    """
    sock = cfg.tun_helper_socket()
    timeout = timeout or 300.0

    ready = _tun_helper_call(sock, {"op": "capability"}, 10.0)
    cap_status = (ready.get("capability") or {}).get("status")
    if not ready.get("ok") or cap_status != "supported" or \
            (ready.get("capability") or {}).get("protocol") != 2:
        reason = (ready.get("capability") or {}).get("reason") or \
                 ready.get("detail") or ready.get("error") or "unknown"
        _die(EXIT_UNSUPPORTED,
             f"tun mode is not available in this environment ({reason}); "
             "there is no silent fallback to proxy")

    upstream = None
    session_id = None
    try:
        if route != "direct":
            try:
                sess = api.oneshot_session(route)
            except ApiError as e:
                if e.status in (401, 403):
                    _die(EXIT_AUTH, f"authentication error: {e.detail}")
                _die(EXIT_RUNTIME,
                     f"cannot create one-shot session for {route!r}: {e}")
            session_id = sess.get("id")
            cap = sess.get("capability", {})
            # The capability host/port describe the backend's own vantage
            # (inside the egressd container). The LOCAL helper reaches the
            # published endpoint from its own vantage, so take host/port
            # from the client config and only the credential from the
            # server capability.
            upstream = {
                "host": cfg.proxy_host,
                "port": int(cfg.oneshot_proxy_port),
                "username": cap.get("username", ""),
                "password": cap.get("password", ""),
            }

        # The helper is native to the host, but a service does not inherit
        # the invoking shell's working directory or environment. Carry these
        # as bounded protocol fields. Identity still comes only from
        # SO_PEERCRED, never from these caller-controlled values.
        child_env = {k: v for k, v in os.environ.items()
                     if k not in ("http_proxy", "https_proxy", "all_proxy",
                                  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                  "no_proxy", "NO_PROXY")}
        try:
            mask = int(next(line.split(":", 1)[1].strip() for line in
                            open("/proc/self/status", encoding="utf-8")
                            if line.startswith("Umask:")), 8)
        except (OSError, StopIteration, ValueError):
            mask = 0o022
        req = {"op": "run", "protocol": 2, "route": route,
               "command": command, "timeout": timeout,
               "cwd": os.getcwd(), "env": child_env, "umask": mask}
        if upstream is not None:
            req["upstream"] = upstream
        resp = _tun_helper_call(sock, req, timeout + 30.0)
    finally:
        if session_id is not None:
            try:
                api.oneshot_close(session_id)
            except ApiError:
                pass  # best-effort; server-side expiry also cleans up

    if not resp.get("ok"):
        err = resp.get("error", "unknown")
        detail = resp.get("detail", "")
        if err == "unsupported_in_environment":
            cap = resp.get("capability", {})
            _die(EXIT_UNSUPPORTED,
                 f"tun mode is not available in this environment "
                 f"({cap.get('reason', 'unknown')}); use --mode proxy. "
                 "There is no silent fallback to proxy.")
        if err == "helper_not_running":
            _die(EXIT_UNSUPPORTED,
                 f"tun helper is not running ({detail}); start "
                 "egress-tun-helper or use --mode proxy. "
                 "There is no silent fallback to proxy.")
        _die(EXIT_RUNTIME, f"tun run failed: {err} {detail}".strip())

    return int(resp.get("exit_code", 0))


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    opts, command = parse_args(argv)
    if not command:
        _die(EXIT_USAGE, "no command after '--'")

    try:
        cfg = load_config(opts["config"])
    except ConfigError as e:
        _die(EXIT_CONFIG, f"config error: {e}")

    mode = opts["mode"] or "proxy"
    route = opts["route"]

    # ---- tun mode (A6): local privileged helper, no silent proxy fallback.
    #      Checked before the direct short-circuit: an explicit --mode tun
    #      request is handled by the helper regardless of the route. ----
    if mode == "tun":
        if cfg.tun_backend != "host-helper":
            _die(EXIT_UNSUPPORTED,
                 f"tun mode is not available in environment {cfg.environment!r} "
                 f"(tun_backend={cfg.tun_backend}); use --mode proxy. "
                 "There is no silent fallback to proxy.")
        # resolve an explicit route (or --default) to a concrete route id
        api = EgressApi(cfg)
        if route is None:
            try:
                route = api.get_default().get("route") or "direct"
            except ApiError as e:
                if e.status in (401, 403):
                    _die(EXIT_AUTH, f"authentication error: {e.detail}")
                _die(EXIT_RUNTIME, f"cannot read default route: {e}")
        sys.exit(_run_tun(cfg, api, route, command, opts["timeout"]))

    # ---- direct: no proxy at all ----
    if route == "direct":
        sys.exit(_run_local({}, command))

    # ---- proxy mode ----
    api = EgressApi(cfg)
    session_id = None
    try:
        if opts["default"] or route is None:
            # persistent default route: use the default proxy endpoint with
            # the client's default-proxy credential.
            try:
                user, password = cfg.default_proxy_auth()
            except ConfigError as e:
                _die(EXIT_CONFIG, str(e))
            env = _proxy_env(cfg.proxy_host, cfg.default_proxy_port, user, password)
        else:
            # explicit route: one-shot session (never touches the default).
            try:
                sess = api.oneshot_session(route)
            except ApiError as e:
                if e.status in (401, 403):
                    _die(EXIT_AUTH, f"authentication error: {e.detail}")
                _die(EXIT_USAGE, f"cannot create one-shot session for {route!r}: {e}")
            session_id = sess.get("id")
            cap = sess.get("capability", {})
            # The capability host/port describe the backend's own vantage
            # (inside the egressd container) — a container client cannot
            # use them. The client reaches the published endpoint from its
            # OWN vantage point (compose.yaml publish), so take host/port
            # from the client config and only the credential from the
            # server capability.
            host = cfg.proxy_host
            port = int(cfg.oneshot_proxy_port)
            user = cap.get("username", "")
            password = cap.get("password", "")
            env = _proxy_env(host, port, user, password)
        sys.exit(_run_local(env, command))
    finally:
        if session_id is not None:
            try:
                api.oneshot_close(session_id)
            except ApiError:
                # best-effort cleanup; the session also expires server-side
                pass


if __name__ == "__main__":
    main()
