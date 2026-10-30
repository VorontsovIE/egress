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
        if a in ("-h", "--help"):
            print("""Usage:
  egress-run [--config PATH] [--timeout SECONDS] [--mode proxy|tun] [--default | ROUTE] -- COMMAND [ARG ...]
  egress-run direct -- COMMAND [ARG ...]

Run COMMAND locally through a route. An explicit ROUTE uses a temporary
session; --default uses the configured default route. 'direct' runs without
a proxy. With --default, the saved mode is used unless --mode overrides it.
An explicit route does not change the saved default.

Options:
  --config PATH       client configuration file
  --timeout SECONDS  timeout for local TUN bootstrap operations
  --mode MODE        override mode: proxy or tun
  --default           use the configured default route
  -h, --help          show this help and exit

Examples:
  egress-run provider:route -- curl https://example.com/
  egress-run --default -- command arg
  egress-run direct -- command arg
  egress-run --mode tun provider:route -- command arg""")
            raise SystemExit(0)
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


def _run_tun(cfg, api: EgressApi, route: str, command: list[str],
             timeout: float | None) -> int:
    """Run a local workload in a private per-invocation TUN namespace."""
    from . import tun_runtime

    if cfg.tun_backend != "local-userns":
        _die(EXIT_UNSUPPORTED,
             f"local TUN is disabled by tun_backend={cfg.tun_backend!r}; "
             "there is no silent fallback to proxy")
    capability = tun_runtime.probe()
    if capability.get("status") != "supported":
        _die(EXIT_UNSUPPORTED,
             f"TUN mode is not available in this environment "
             f"({capability.get('reason', 'unknown')}); "
             "there is no silent fallback to proxy")

    upstream = None
    session_id = None
    try:
        if route != "direct":
            try:
                session = api.oneshot_session(route)
            except ApiError as exc:
                if exc.status in (401, 403):
                    _die(EXIT_AUTH, f"authentication error: {exc.detail}")
                _die(EXIT_USAGE, f"cannot create one-shot session for {route!r}: {exc}")
            session_id = session.get("id")
            capability = session.get("capability", {})
            upstream = {
                "host": cfg.proxy_host,
                "port": int(cfg.oneshot_proxy_port),
                "username": capability.get("username", ""),
                "password": capability.get("password", ""),
            }
        child_env = {key: value for key, value in os.environ.items()
                     if key.lower() not in {"http_proxy", "https_proxy", "all_proxy",
                                            "no_proxy", "egress_egress_tun_engine"}}
        try:
            return tun_runtime.run(
                command, env=child_env, cwd=os.getcwd(),
                dns_servers=cfg.tun_dns_servers, upstream=upstream,
                timeout=timeout,
            )
        except (OSError, RuntimeError) as exc:
            _die(EXIT_UNSUPPORTED,
                 f"local TUN setup failed: {exc}; there is no silent fallback to proxy")
    finally:
        if session_id is not None:
            try:
                api.oneshot_close(session_id)
            except ApiError:
                pass


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    opts, command = parse_args(argv)
    if not command:
        _die(EXIT_USAGE, "no command after '--'")

    try:
        cfg = load_config(opts["config"])
    except ConfigError as e:
        _die(EXIT_CONFIG, f"config error: {e}")

    api = None
    mode = opts["mode"]
    if mode is None and opts["default"]:
        api = EgressApi(cfg)
        try:
            mode = api.get_default_mode().get("mode", "proxy")
        except ApiError as e:
            if e.status in (401, 403):
                _die(EXIT_AUTH, f"authentication error: {e.detail}")
            _die(EXIT_RUNTIME, f"cannot read default mode: {e}")
    mode = mode or "proxy"
    route = opts["route"]
    if route is not None and route.startswith("@"):
        try:
            route = EgressApi(cfg).resolve_route(route)
        except ApiError as e:
            if e.status in (401, 403):
                _die(EXIT_AUTH, f"authentication error: {e.detail}")
            _die(EXIT_RUNTIME, f"cannot resolve personal route: {e}")
        except ValueError as e:
            _die(EXIT_USAGE, str(e))

    # ---- local per-run TUN mode; failures never fall back to proxy. ----
    if mode == "tun":
        # resolve an explicit route (or --default) to a concrete route id
        api = api or EgressApi(cfg)
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
    api = api or EgressApi(cfg)
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
