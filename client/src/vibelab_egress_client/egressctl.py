"""egressctl — public client CLI (spec section 30.1).

Uses only documented public egressd /v1 endpoints. Read commands support
``--json``. Exit codes (stable, documented):

  0  success
  1  runtime/API error (backend down, request failed)
  2  usage error (bad arguments)
  3  configuration error (config missing/invalid, secrets unreadable)
  4  authentication error (unknown/missing token)
  5  identity mismatch (local client_id != server-bound client_id)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .api import ApiError, EgressApi
from .config import ClientConfig, ConfigError, load_config

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4
EXIT_IDENTITY = 5
SOURCE_TIMEOUT = 300


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _emit(args: argparse.Namespace, data: Any, human: str) -> None:
    if getattr(args, "json", False):
        _print_json(data)
    else:
        print(human)


def _load(args: argparse.Namespace) -> ClientConfig:
    try:
        return load_config(getattr(args, "config", None))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)


def _api(args: argparse.Namespace) -> EgressApi:
    return EgressApi(_load(args))


def _call(args: argparse.Namespace, fn, human_fn) -> Any:
    """Run an API call with uniform error handling; return the payload."""
    try:
        data = fn()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    except ApiError as e:
        if e.status in (401, 403):
            print(f"authentication error: {e.detail}", file=sys.stderr)
            sys.exit(EXIT_AUTH)
        print(f"error: {e}", file=sys.stderr)
        sys.exit(EXIT_RUNTIME)
    except TimeoutError:
        print("error: request timed out; outcome unknown; check egressctl source list before retrying",
              file=sys.stderr)
        sys.exit(EXIT_RUNTIME)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"error: {type(e).__name__} in local source operation", file=sys.stderr)
        sys.exit(EXIT_RUNTIME)
    return data


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_status(args) -> None:
    api = _api(args)
    data = _call(args, api.status, None)
    m = data.get("mihomo", {})
    _emit(args, data,
          f"client={data.get('client_id')} mihomo={'up' if m.get('reachable') else 'down'}"
          + (f" v{m.get('version')}" if m.get("version") else ""))


def cmd_whoami(args) -> None:
    api = _api(args)
    cfg = _load(args)
    data = _call(args, api.self_, None)
    server_id = data.get("client_id")
    if server_id != cfg.client_id:
        print(f"identity mismatch: local client_id={cfg.client_id!r} "
              f"but token is bound to {server_id!r}", file=sys.stderr)
        sys.exit(EXIT_IDENTITY)
    _emit(args, data,
          f"client_id={server_id} environment={data.get('environment')} "
          f"modes={','.join(data.get('modes', []))}")


def cmd_route_current(args) -> None:
    api = _api(args)
    data = _call(args, api.get_default, None)
    _emit(args, data, f"default route: {data.get('route')}")


def cmd_route_set(args) -> None:
    api = _api(args)
    data = _call(args, lambda: api.set_default(args.route), None)
    _emit(args, data, f"default route set: {args.route}")


def cmd_mode_current(args) -> None:
    api = _api(args)
    data = _call(args, api.get_default_mode, None)
    _emit(args, data, f"default mode: {data.get('mode')}")


def cmd_mode_set(args) -> None:
    api = _api(args)
    data = _call(args, lambda: api.set_default_mode(args.mode), None)
    _emit(args, data, f"default mode set: {args.mode}")


def cmd_providers(args) -> None:
    api = _api(args)
    data = _call(args, api.list_providers, None)
    providers = data.get("providers", data if isinstance(data, list) else [])
    if getattr(args, "json", False):
        _print_json(data)
    else:
        for p in providers:
            print(f"{p.get('id', '?')}: type={p.get('type', '?')} "
                  f"nodes={p.get('node_count', '?')} "
                  f"last_success={p.get('last_success') or '-'} "
                  f"last_error={p.get('last_error') or '-'}")


def cmd_source_list(args) -> None:
    data = _call(args, _api(args).list_personal, None)
    if args.json:
        _print_json(data)
    else:
        for item in data.get("providers", []):
            skipped = item.get("skipped_nodes") or 0
            suffix = f", {skipped} skipped" if skipped else ""
            count = item["node_count"]
            label = "node" if count == 1 else "nodes"
            print(f"{item['alias']}\t{item['kind']}\t{count} {label}{suffix}")


def _source_result(args, data, action: str, name: str) -> None:
    if data.get("skipped"):
        _emit(args, data, f"source skipped: {name} (already exists unchanged)")
        return
    skipped = data.get("skipped_nodes") or 0
    count = data.get("node_count", 0)
    label = "node" if count == 1 else "nodes"
    suffix = f" ({count} {label}, {skipped} skipped)" if skipped else ""
    _emit(args, data, f"source {action}: {name}{suffix}")


def cmd_source_add(args) -> None:
    from . import personal_sources as local
    cfg = _load(args)
    api = EgressApi(cfg, timeout=SOURCE_TIMEOUT)
    if not local.ALIAS.fullmatch(args.name):
        raise SystemExit("invalid source name")
    if args.url:
        kind, value = "url", args.url
    elif args.uri is not None:
        kind = "uri"
        value = sys.stdin.readline(local.MAX_SOURCE_BYTES + 1).strip() if args.uri == "-" else args.uri
    else:
        try:
            kind, value = "file", Path(args.file).read_text()
        except (OSError, UnicodeError) as e:
            print(f"source file error: {e}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
    if len(value.encode()) > local.MAX_SOURCE_BYTES:
        raise SystemExit("source too large")
    if cfg.environment == "host":
        data = _call(args, lambda: api.add_personal(args.name, kind, value), None)
    else:
        path = local.path_for(args.name)
        if path.exists():
            existing = _call(args, lambda: local.load(args.name), None)
            if existing["kind"] == kind and existing["source"] == value:
                remote = _call(args, api.list_personal, None)
                for item in remote.get("providers", []):
                    if item.get("alias") == args.name:
                        _source_result(args, {**item, "skipped": True}, "added", args.name)
                        return
        local.save(args.name, kind, value)
        data = _call(args, lambda: api.upload_personal(args.name, local.normalize(args.name)), None)
    _source_result(args, data, "added", args.name)


def cmd_source_refresh(args) -> None:
    from . import personal_sources as local
    cfg = _load(args)
    api = EgressApi(cfg, timeout=SOURCE_TIMEOUT)
    if cfg.environment == "host":
        data = _call(args, lambda: api.refresh_personal(args.name), None)
    else:
        data = _call(args, lambda: api.upload_personal(args.name, local.normalize(args.name)), None)
    _source_result(args, data, "refreshed", args.name)


def cmd_source_remove(args) -> None:
    from . import personal_sources as local
    cfg = _load(args)
    data = _call(args, lambda: EgressApi(cfg).delete_personal(args.name), None)
    if cfg.environment != "host":
        local.remove(args.name)
    _emit(args, data, f"source removed: {args.name}")


def cmd_update(args) -> None:
    api = _api(args)
    data = _call(args,
                 lambda: api.update_provider(args.provider, via=args.via,
                                             if_older_than=args.if_older_than),
                 None)
    _emit(args, data, f"update done: {data.get('updated', data)}")


def cmd_list(args) -> None:
    api = _api(args)
    filters = {}
    if args.country:
        filters["country"] = args.country
    if args.provider:
        filters["provider"] = args.provider
    if args.healthy:
        filters["healthy"] = "true"
    data = _call(args, lambda: api.list_routes(**filters), None)
    routes = data.get("routes", data if isinstance(data, list) else [])
    if getattr(args, "json", False):
        _print_json(data)
    else:
        for r in routes:
            print(f"{r.get('id', '?')}\t{r.get('provider', '?')}/{r.get('country', '?')}\t"
                  f"delay={r.get('delay_ms', '?')}ms healthy={r.get('healthy', '?')}")


def cmd_test(args) -> None:
    api = _api(args)
    data = _call(args, lambda: api.test(args.route), None)
    _emit(args, data, f"test: {data.get('result', data)}")


def cmd_probe_domain(args) -> None:
    api = _api(args)
    data = _call(args, lambda: api.probe_domain(args.domain, all_routes=args.all), None)
    _emit(args, data, f"probe {args.domain}: {data.get('ok_count', '?')} ok / "
                      f"{data.get('total', '?')} routes")


def cmd_benchmark(args) -> None:
    api = _api(args)
    if args.route:
        data = _call(args, lambda: api.benchmark(args.route), None)
    else:
        # shortlist pipeline (spec 25): health/latency -> shortlist -> bench
        q = {"top_latency": args.top_latency}
        if args.country:
            q["country"] = args.country
        if args.provider:
            q["provider"] = args.provider
        if args.bytes:
            q["bytes"] = args.bytes
        data = _call(args, lambda: api.benchmark_shortlist(**q), None)
    _emit(args, data, _benchmark_human(data))


def _benchmark_human(data: Any) -> str:
    lines = []
    for r in data.get("results", []):
        if r.get("ok"):
            lines.append(f"{r.get('route')}\t{r.get('mbps')} Mbps"
                         f" ({r.get('bytes')} bytes)")
        else:
            lines.append(f"{r.get('route')}\tfailed: {r.get('error')}")
    return "\n".join(lines) if lines else "no benchmark results"


def cmd_best(args) -> None:
    api = _api(args)
    data = _call(args, lambda: api.best(
        country=args.country, provider=args.provider, domain=args.domain,
        max_latency=args.max_latency, speed_aware=args.speed_aware), None)
    if getattr(args, "json", False):
        _print_json(data)
        return
    best = data.get("best")
    if best is None:
        print("no candidate routes "
              f"({len(data.get('excluded', []))} excluded)", file=sys.stderr)
        sys.exit(EXIT_RUNTIME)
    print(f"best: {best}")
    for c in data.get("candidates", [])[: args.limit]:
        print(f"  #{c.get('rank')} {c.get('route')}\t"
              f"score={c.get('score')}\t{c.get('explanation')}")
    excluded = data.get("excluded", [])
    if excluded:
        print(f"  ({len(excluded)} excluded: "
              + ", ".join(f"{e.get('route')} [{e.get('reason')}]"
                          for e in excluded[:5])
              + ("..." if len(excluded) > 5 else "") + ")")


def cmd_oneshot_active(args) -> None:
    api = _api(args)
    data = _call(args, api.oneshot_active, None)
    sessions = data.get("sessions", [])
    if getattr(args, "json", False):
        _print_json(data)
    else:
        if not sessions:
            print("no active one-shot sessions")
        for s in sessions:
            print(f"{s.get('id', '?')}\troute={s.get('route', '?')}\t"
                  f"since={s.get('created', '?')}")


def cmd_geo_status(args) -> None:
    api = _api(args)
    data = _call(args, api.geo_status, None)
    _emit(args, data, f"geo: {data.get('status', data)}")


def cmd_geo_update(args) -> None:
    api = _api(args)
    data = _call(args, api.geo_update, None)
    _emit(args, data, "geo update triggered")


def cmd_config_show(args) -> None:
    cfg = _load(args)
    # spec 29: by default mask token/credentials.
    shown = {
        "client_id": cfg.client_id,
        "control_url": cfg.control_url,
        "proxy_host": cfg.proxy_host,
        "default_proxy_port": cfg.default_proxy_port,
        "oneshot_proxy_port": cfg.oneshot_proxy_port,
        "api_token_file": cfg.api_token_file,
        "default_proxy_auth_file": cfg.default_proxy_auth_file,
        "environment": cfg.environment,
        "tun_backend": cfg.tun_backend,
    }
    if getattr(args, "secrets", False):
        try:
            shown["api_token"] = cfg.api_token()
            shown["default_proxy_auth"] = ":".join(cfg.default_proxy_auth())
        except ConfigError as e:
            shown["secret_error"] = str(e)
    _emit(args, shown, "\n".join(f"{k}: {v}" for k, v in shown.items()))


def cmd_config_validate(args) -> None:
    cfg = _load(args)
    # structural validation already ran in load_config; also check secret
    # files are readable (without printing their contents).
    try:
        cfg.api_token()
        cfg.default_proxy_auth()
    except ConfigError as e:
        print(f"invalid: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    print(f"valid: client_id={cfg.client_id} environment={cfg.environment} "
          f"control={cfg.control_url}")


def cmd_doctor(args) -> None:
    api = _api(args)
    cfg = _load(args)
    checks: dict[str, str] = {}
    # 1. config + secrets (local)
    try:
        cfg.api_token()
        cfg.default_proxy_auth()
        checks["config"] = "ok"
    except ConfigError as e:
        checks["config"] = f"FAIL: {e}"
    # 2. control plane reachable
    try:
        health = api.health()
        checks["control_plane"] = f"ok (uptime {health.get('uptime_s')}s)"
    except ApiError as e:
        checks["control_plane"] = f"FAIL: {e}"
    # 3. identity
    try:
        self_ = api.self_()
        if self_.get("client_id") == cfg.client_id:
            checks["identity"] = f"ok ({self_.get('client_id')})"
        else:
            checks["identity"] = f"FAIL: token bound to {self_.get('client_id')!r}"
    except ApiError as e:
        checks["identity"] = f"FAIL: {e}"
    # 4. proxy endpoint reachable (TCP connect only)
    import socket
    try:
        with socket.create_connection((cfg.proxy_host, cfg.default_proxy_port), 3):
            checks["proxy_endpoint"] = "ok"
    except OSError as e:
        checks["proxy_endpoint"] = f"FAIL: {e}"
    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    for k, v in checks.items():
        print(f"{k}: {v}")
    sys.exit(EXIT_OK if not failed else EXIT_RUNTIME)


def _gvisor_kernel() -> bool:
    """True when running under gVisor ('gvisor' in /proc/version)."""
    try:
        return "gvisor" in Path("/proc/version").read_text(
            encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _tun_capability(cfg: ClientConfig) -> dict:
    """Local tun capability probe (spec 50.12 / phase A6).

    Purely local: config + helper UNIX socket only, no backend call. A socket
    path alone does not prove that the helper can create a network namespace.
    """
    from . import __version__

    if cfg.environment == "gvisor-container" or _gvisor_kernel():
        status = "unsupported_in_environment"
        reason = (f"gVisor environment (environment={cfg.environment!r}): "
                  "no /dev/net/tun / CAP_NET_ADMIN")
    elif cfg.tun_backend != "host-helper":
        status = "none"
        reason = f"tun_backend={cfg.tun_backend!r}"
    else:
        sock = cfg.tun_helper_socket()
        if not os.path.exists(sock):
            status = "helper_not_running"
            reason = f"host-helper socket missing: {sock}"
        else:
            from .egress_run import _tun_helper_call
            response = _tun_helper_call(sock, {"op": "capability"}, 2.0)
            capability = response.get("capability") if response.get("ok") else None
            if isinstance(capability, dict) and capability.get("status") in (
                    "supported", "unsupported_in_environment"):
                status = capability["status"]
                reason = str(capability.get("reason") or status)
            else:
                status = "helper_not_running"
                reason = ("host-helper capability probe failed: "
                          f"{response.get('error', 'invalid_response')}")
    return {
        "client_id": cfg.client_id,
        "client_version": __version__,
        "environment": cfg.environment,
        "tun": {
            "backend": cfg.tun_backend,
            "socket": cfg.tun_helper_socket()
                      if cfg.tun_backend == "host-helper" else None,
            "status": status,
            "reason": reason,
        },
    }


def cmd_capabilities(args) -> None:
    cfg = _load(args)
    data = _tun_capability(cfg)
    _emit(args, data,
          f"client={cfg.client_id} environment={cfg.environment} "
          f"tun={data['tun']['status']} ({data['tun']['reason']})")


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="egressctl",
                                description="vibelab-egress client control CLI")
    p.add_argument("--config", help="explicit client config path")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_=""):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "backend status for this client")
    add("whoami", cmd_whoami, "server-bound identity for this token")

    route = add("route", cmd_route_current, "default route management")
    rsub = route.add_subparsers(dest="route_cmd", required=True)
    rc = rsub.add_parser("current", help="show default route")
    rc.set_defaults(fn=cmd_route_current)
    rs = rsub.add_parser("set", help="set default route")
    rs.add_argument("route")
    rs.set_defaults(fn=cmd_route_set)

    mode = add("mode", cmd_mode_current, "default execution mode")
    msub = mode.add_subparsers(dest="mode_cmd", required=True)
    mc = msub.add_parser("current", help="show default mode")
    mc.set_defaults(fn=cmd_mode_current)
    ms = msub.add_parser("set", help="set default mode")
    ms.add_argument("mode", choices=["proxy", "tun"])
    ms.set_defaults(fn=cmd_mode_set)

    add("providers", cmd_providers, "list providers")

    source = add("source", cmd_source_list, "personal subscription sources")
    ssub = source.add_subparsers(dest="source_cmd", required=True)
    sl = ssub.add_parser("list", help="list personal sources")
    sl.set_defaults(fn=cmd_source_list)
    sa = ssub.add_parser("add", help="add a private subscription or proxy source")
    sa.add_argument("name")
    choice = sa.add_mutually_exclusive_group(required=True)
    choice.add_argument("--url")
    choice.add_argument("--file")
    choice.add_argument("--uri", help="one proxy URI; use '-' to read a line from stdin")
    sa.set_defaults(fn=cmd_source_add)
    sr = ssub.add_parser("refresh", help="refresh a private source")
    sr.add_argument("name")
    sr.set_defaults(fn=cmd_source_refresh)
    sd = ssub.add_parser("remove", help="remove a private source")
    sd.add_argument("name")
    sd.set_defaults(fn=cmd_source_remove)

    up = add("update", cmd_update, "trigger provider update")
    up.add_argument("--provider", help="specific provider id")
    up.add_argument("--via", help="route to fetch the subscription through")
    up.add_argument("--if-older-than", help="skip if updated within this duration, e.g. 6h")

    ls = add("list", cmd_list, "list routes")
    ls.add_argument("--country")
    ls.add_argument("--provider")
    ls.add_argument("--healthy", action="store_true")

    t = add("test", cmd_test, "test connectivity via a route")
    t.add_argument("route", nargs="?")

    pd = add("probe-domain", cmd_probe_domain, "probe a domain through routes")
    pd.add_argument("domain")
    pd.add_argument("--all", action="store_true", help="all routes, not just default")

    bm = add("benchmark", cmd_benchmark,
             "throughput benchmark: one route, or a shortlist (spec 25)")
    bm.add_argument("route", nargs="?",
                    help="route id; omit for the latency shortlist")
    bm.add_argument("--country")
    bm.add_argument("--provider")
    bm.add_argument("--top-latency", type=int, default=5,
                    help="shortlist size (default 5)")
    bm.add_argument("--bytes", help="download size, e.g. 8M (default 8M)")

    b = add("best", cmd_best, "best route by transparent ranking (spec 26)")
    b.add_argument("--country")
    b.add_argument("--provider")
    b.add_argument("--domain",
                   help="only routes with a fresh successful probe of this domain")
    b.add_argument("--max-latency", type=int, help="exclude routes slower than this (ms)")
    b.add_argument("--speed-aware", action="store_true",
                   help="include recent throughput in the score")
    b.add_argument("--limit", type=int, default=10,
                   help="how many candidates to print (default 10)")

    os_ = add("oneshot", cmd_oneshot_active, "one-shot sessions")
    osub = os_.add_subparsers(dest="oneshot_cmd", required=True)
    oa = osub.add_parser("active", help="list active sessions")
    oa.set_defaults(fn=cmd_oneshot_active)

    geo = add("geo", cmd_geo_status, "GEO data status/update")
    gsub = geo.add_subparsers(dest="geo_cmd", required=True)
    gs = gsub.add_parser("status", help="GEO status")
    gs.set_defaults(fn=cmd_geo_status)
    gu = gsub.add_parser("update", help="trigger GEO update")
    gu.set_defaults(fn=cmd_geo_update)

    cfg = add("config", cmd_config_show, "client config")
    csub = cfg.add_subparsers(dest="config_cmd", required=True)
    cs = csub.add_parser("show", help="show config (secrets masked)")
    cs.add_argument("--secrets", action="store_true", help="include secret values")
    cs.set_defaults(fn=cmd_config_show)
    cv = csub.add_parser("validate", help="validate config + secret files")
    cv.set_defaults(fn=cmd_config_validate)

    add("capabilities", cmd_capabilities,
         "local capabilities (tun availability, spec 50.12)")
    add("doctor", cmd_doctor, "local + backend health checks")
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
