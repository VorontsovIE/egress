"""egressd — vibelab-egress control plane (phase A3: full public API).

Implements the public narrow ``/v1`` API (spec section 27) on top of the A1
skeleton:

  * identity / status / capabilities (A1)
  * route catalog (stable route IDs, spec 13)
  * per-client default route / mode (spec 15/16)
  * provider inventory + update + reconcile (spec 11/12)
  * health/latency test (spec 23)
  * domain probe + error classification (spec 24)
  * bounded throughput benchmark (spec 25)
  * one-shot sessions (spec 18/19/20)
  * proxy default-capability (spec 27)
  * reconcile + doctor (spec 12/40)
  * GEO (spec 14/41, A2)

Security rules (spec section 27/28):
  * the authenticated token determines the client profile; a client can never
    name another ``client_id`` to read or change someone else's state;
  * responses never contain provider credentials or the mihomo admin secret;
  * the public API never executes an arbitrary user command and never exposes a
    raw Mihomo passthrough;
  * logs are structured JSON and never contain token/secret values.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request

API_VERSION = "v1"
COMPAT = {"api_version": "v1", "min_client": "0.1.0"}
# egressd API version, reported by /v1/status for client version
# negotiation (spec 49.42). Keep in sync with the client package
# __version__ (client/src/vibelab_egress_client/__init__.py).
VERSION = "0.1.0"

log = logging.getLogger("egressd")

# route IDs and mihomo names are constrained; reject anything else early.
_ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+:[A-Za-z0-9._\-]+$|^direct$")


# --------------------------------------------------------------------------
# structured JSON logging (spec section 40)
# --------------------------------------------------------------------------
# stdlib LogRecord attributes that are not user ``extra=`` fields.
_RESERVED_LOG_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
}


class JsonLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._exc_formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # structured fields passed via ``extra=`` (audit events, PUT-failure
        # detail). getMessage() already consumed them, so read them back off the
        # record -- without this the log line is just "audit" with no payload.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_ATTRS or key.startswith("_"):
                continue
            entry[key] = value
        if record.exc_info:
            entry["exc"] = self._exc_formatter.format(record)
        print(json.dumps(entry, default=str), flush=True)


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(os.environ.get("EGRESSD_LOG_LEVEL", "INFO").upper())
    handler = JsonLogHandler()
    root.handlers = [handler]


# --------------------------------------------------------------------------
# client profile registry: token -> profile (loaded from mounted files)
# --------------------------------------------------------------------------
class ClientRegistry:
    """Maps API tokens to client profiles.

    Files (in ``clients_dir``):
      ``<client_id>.token``          -- the raw token (one line)
      ``<client_id>.profile.json``   -- non-secret profile metadata
      ``<client_id>.proxy-creds``    -- ``user:password`` for the client's
                                        default-proxy listener identity

    The server never trusts a client-supplied client_id; identity is derived
    from the presented token.
    """

    def __init__(self, clients_dir: str | Path):
        self.clients_dir = Path(clients_dir)
        self._token_to_client: dict[str, str] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._creds: dict[str, tuple[str, str]] = {}
        self._sig: tuple[int, ...] | None = None
        self.reload()

    def _state_sig(self) -> tuple[int, ...] | None:
        """Signature of the clients dir: dir mtime + per-file (mtime, size).

        Watching only the dir mtime misses in-place rewrites:
        scripts/provision-client.sh rotates a token by overwriting the existing
        <id>.token file, which bumps the file mtime but not the dir mtime -- a
        running egressd would keep authenticating the OLD token forever.
        Per-file stats catch rotation of existing clients as well as adding
        and removing clients. Returns None when the dir is unreadable (the
        last good registry is kept instead of reload-churning on every call).
        """
        try:
            parts = [self.clients_dir.stat().st_mtime_ns]
            for entry in sorted(self.clients_dir.iterdir()):
                st = entry.stat()
                parts.extend((st.st_mtime_ns, st.st_size))
            return tuple(parts)
        except OSError:
            return None

    def reload(self) -> None:
        token_to_client: dict[str, str] = {}
        profiles: dict[str, dict[str, Any]] = {}
        creds: dict[str, tuple[str, str]] = {}
        # egressd runs as a NON-root user (see egressd/Dockerfile) and reads the
        # backend client secrets from a root-provisioned dir. If that dir (or a
        # file in it) is not readable by the egressd uid, is_dir()/glob()/
        # read_text() raise PermissionError. Historically that propagated out of
        # create_app() and crash-looped the whole container with a bare
        # traceback. Catch it, log an actionable error, and degrade to an empty
        # registry (every API call 401s until ownership is fixed) instead of
        # taking the control plane down.
        try:
            if self.clients_dir.is_dir():
                for tok in sorted(self.clients_dir.glob("*.token")):
                    client_id = tok.name[: -len(".token")]
                    raw = tok.read_text().strip()
                    if not raw:
                        continue
                    token_to_client[raw] = client_id
                    prof_path = self.clients_dir / f"{client_id}.profile.json"
                    profile: dict[str, Any] = {
                        "client_id": client_id,
                        "environment": "unknown",
                        "modes": ["proxy"],
                    }
                    if prof_path.exists():
                        try:
                            meta = json.loads(prof_path.read_text())
                            profile.update(
                                {k: meta[k] for k in ("environment", "modes", "description") if k in meta}
                            )
                        except (json.JSONDecodeError, OSError):
                            pass
                    profiles[client_id] = profile
                    pc = self.clients_dir / f"{client_id}.proxy-creds"
                    if pc.exists():
                        u, _, p = pc.read_text().strip().partition(":")
                        if u and p:
                            creds[client_id] = (u, p)
        except OSError as e:
            log.error(
                "client registry: cannot read clients dir %s (%s). egressd "
                "runs as a non-root user and must be able to read the backend "
                "client secrets. Fix ownership, e.g. "
                "chown <egressd-uid>:<egressd-gid> %s and its files "
                "(scripts/install-backend-host.sh does this). Starting with an "
                "EMPTY registry: all authenticated API calls will return 401 "
                "until this is fixed.",
                self.clients_dir, e, self.clients_dir,
            )
        self._token_to_client = token_to_client
        self._profiles = profiles
        self._creds = creds
        self._sig = self._state_sig()
        log.info("client registry loaded", extra={"clients": len(profiles)})

    def maybe_reload(self) -> None:
        sig = self._state_sig()
        if sig is not None and sig != self._sig:
            self.reload()

    def resolve(self, token: str) -> dict[str, Any] | None:
        self.maybe_reload()
        client_id = self._token_to_client.get(token)
        if client_id is None:
            return None
        return self._profiles[client_id]

    def proxy_creds(self, client_id: str) -> tuple[str, str] | None:
        self.maybe_reload()
        return self._creds.get(client_id)


# --------------------------------------------------------------------------
# mihomo admin client (egressd is the only component allowed to use it)
# --------------------------------------------------------------------------
class MihomoAdmin:
    """Thin client for the Mihomo external-controller API.

    Only egressd holds the mihomo admin secret (spec 28). The methods cover the
    subset the control plane uses: version, proxy/group selection, per-node
    delay, and native provider refresh. Each method returns ``(status, data)``
    where ``status`` is 0 on transport failure.
    """

    def __init__(self) -> None:
        self.base_url = os.environ.get("MIHOMO_API_URL", "http://127.0.0.1:9090")
        secret_file = os.environ.get("MIHOMO_SECRET_FILE", "/etc/vibelab-egress/secrets/mihomo_api_secret")
        try:
            self.secret = Path(secret_file).read_text().strip()
        except OSError:
            self.secret = ""

    def _request(self, method: str, path: str, data: Any = None,
                 timeout: float = 8.0) -> tuple[int, Any]:
        body = None
        headers = {}
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, (json.loads(raw) if raw else {"error": str(e)})
            except json.JSONDecodeError:
                return e.code, {"error": raw.decode(errors="replace")}
        except OSError as e:
            return 0, {"error": str(e)}

    def get(self, path: str, timeout: float = 8.0) -> tuple[int, Any]:
        return self._request("GET", path, None, timeout)

    def put(self, path: str, data: Any = None, timeout: float = 8.0) -> tuple[int, Any]:
        st, resp = self._request("PUT", path, data, timeout)
        if st not in (200, 204):
            log.warning("mihomo PUT failed", extra={"path": path, "status": st,
                                                    "resp": str(resp)[:200]})
        return st, resp

    def delay(self, node_name: str, url: str, timeout_ms: int = 5000) -> tuple[int, Any]:
        """Per-node HTTP delay test (spec 23). Mihomo: /proxies/<name>/delay."""
        from urllib.parse import quote

        return self._request(
            "GET",
            f"/proxies/{quote(node_name)}/delay?url={quote(url)}&timeout={timeout_ms}",
            None, 10.0,
        )

    def update_provider(self, provider_id: str) -> tuple[int, Any]:
        """Trigger a native (http) provider refresh (best-effort)."""
        from urllib.parse import quote

        return self._request("PUT", f"/providers/{quote(provider_id)}/config",
                             {"force": True}, 10.0)


# --------------------------------------------------------------------------
# provider inventory (file providers egressd controls)
# --------------------------------------------------------------------------
def _read_provider_file(path: Path) -> tuple[list[str], dict[str, str], int]:
    """Read a provider YAML file -> (clean_names, {clean: type}, mtime).

    The file is the adapter output: ``proxies: [{name: <clean>, type: ...}]``.
    Clean names are what the catalog uses; the mihomo-visible name is
    ``<prefix><clean>`` (additional-prefix, spec 11.1).
    """
    import yaml

    data = yaml.safe_load(path.read_text())
    proxies = data.get("proxies", []) if isinstance(data, dict) else []
    names: list[str] = []
    types: dict[str, str] = {}
    for p in proxies:
        if isinstance(p, dict) and p.get("name"):
            names.append(str(p["name"]))
            types[str(p["name"])] = str(p.get("type", "unknown"))
    return names, types, int(path.stat().st_mtime)


def scan_providers(providers_dir: Path) -> dict[str, dict[str, Any]]:
    """Scan the providers dir -> {provider: {type, path, prefix, mtime}}."""
    manifest: dict[str, dict[str, Any]] = {}
    # egressd runs non-root; the providers dir may be unreadable (PermissionError
    # on is_dir/glob). Degrade to an empty manifest instead of crashing.
    try:
        if not providers_dir.is_dir():
            return manifest
        provider_files = sorted(providers_dir.glob("*.yaml"))
    except OSError as e:
        log.warning("scan_providers: cannot read providers dir %s (%s)",
                    providers_dir, e)
        return manifest
    from .provider_sources import ID_RE, load_source, read_status, source_dir
    from .personal import load_metadata

    ids = {p.stem for p in provider_files if ID_RE.fullmatch(p.stem)}
    try:
        ids.update(p.stem for p in source_dir().glob("*.json") if ID_RE.fullmatch(p.stem))
    except OSError:
        pass
    for provider_id in sorted(ids):
        p = providers_dir / f"{provider_id}.yaml"
        try:
            _, _, mtime = _read_provider_file(p)
        except Exception:
            mtime = 0
        source = load_source(provider_id)
        personal = load_metadata(provider_id)
        manifest[provider_id] = {
            "type": source["type"] if source else (personal or {}).get("kind", "file"),
            "path": str(p),
            "prefix": f"{provider_id}-",
            "source": source or os.environ.get(f"EGRESSD_PROVIDER_SOURCE_{provider_id}"),
            "mtime": mtime,
            "status": read_status(provider_id),
            "owner": (personal or {}).get("client_id"),
            "kind": (personal or {}).get("kind"),
            "personal": personal,
        }
    return manifest


def rebuild_catalog(app: FastAPI) -> Any:
    """Rebuild the route catalog from the provider files + sync the metrics DB.

    Returns the catalog. The same object is referenced by the ClientState, so
    a rebuild is visible to the per-client default logic immediately.
    """
    from .catalog import RouteCatalog

    providers_dir = Path(os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers"))
    manifest = scan_providers(providers_dir)
    app.state.provider_manifest = manifest

    providers: dict[str, list[str]] = {}
    prefixes: dict[str, str] = {}
    protocols: dict[str, dict[str, str]] = {}
    owners: dict[str, str] = {}
    for prov, meta in manifest.items():
        try:
            names, types, _ = _read_provider_file(Path(meta["path"]))
        except Exception:
            log.warning("provider read failed", extra={"provider": prov})
            names, types = [], {}
        providers[prov] = names
        prefixes[prov] = meta.get("prefix", f"{prov}-")
        protocols[prov] = types
        if meta.get("owner"):
            owners[prov] = meta["owner"]

    catalog = RouteCatalog().build(providers, prefixes, protocols, owners)
    # enrich with the most recent benchmark sample per route (spec 25/26:
    # the throughput score in `best --speed-aware` and the route listing).
    try:
        rows = app.state.metrics._query(
            "SELECT route_id, mbps, ts FROM benchmark_samples b"
            " WHERE ok=1 AND mbps IS NOT NULL AND ts = ("
            "   SELECT MAX(ts) FROM benchmark_samples b2"
            "   WHERE b2.route_id = b.route_id AND b2.ok=1 AND b2.mbps IS NOT NULL)")
        for row in rows:
            r = catalog.get(row["route_id"])
            if r is not None:
                r.benchmark_mbps = row["mbps"]
                r.benchmark_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(row["ts"]))
    except Exception as e:
        log.warning("benchmark enrichment failed", extra={"error": str(e)})
    app.state.catalog = catalog
    # keep the ClientState and the probe engine pointing at the same catalog
    # object (both captured the initial empty catalog at startup; without this
    # a rebuild is invisible to them and probes see every route as unknown).
    app.state.state.catalog = catalog
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        engine.catalog = catalog
    app.state.metrics.sync_routes([r.to_dict() for r in catalog.all()])
    log.info("catalog rebuilt", extra={
        "providers": {p: len(n) for p, n in providers.items()},
        "routes": len(catalog.all_route_ids()),
    })
    return catalog


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
def create_app() -> FastAPI:
    _setup_logging()
    # docs/redoc/openapi disabled: the public API is exactly the documented
    # /v1 set (spec 27); auto-generated schema endpoints would expose the
    # surface to unauthenticated callers (A8 surface audit).
    app = FastAPI(title="egressd", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.registry = ClientRegistry(
        os.environ.get("EGRESSD_CLIENTS_DIR", "/etc/vibelab-egress/secrets/clients")
    )
    app.state.mihomo = MihomoAdmin()
    app.state.started_at = time.time()

    # GEO manager (spec 14/41)
    from .geo_update import DATASET_NAMES, GeoConfig, GeoManager

    def _geo_reload_hook(names: list[str]) -> None:
        log.info("geo update written; mihomo picks up on native auto-update",
                 extra={"datasets": names})

    app.state.geo = GeoManager(GeoConfig(
        geo_dir=Path(os.environ.get("EGRESSD_GEO_DIR", "/var/lib/egressd/geo")),
        interval_hours=int(os.environ.get("EGRESSD_GEO_INTERVAL_HOURS", "24")),
        reload_hook=_geo_reload_hook,
    ))

    # ---- A3 state ---------------------------------------------------------
    from .catalog import DIRECT, RouteCatalog
    from .metrics import MetricsDB
    from .probe import ProbeEngine, SlotAllocator, load_slots
    from .state import ClientState

    state_dir = Path(os.environ.get("EGRESSD_STATE_DIR", "/var/lib/egressd/state"))
    app.state.metrics = MetricsDB(os.environ.get("EGRESSD_METRICS_DB",
                                                 str(state_dir / "metrics.sqlite")))
    app.state.catalog = RouteCatalog()
    app.state.state = ClientState(state_dir / "defaults.json", app.state.catalog)

    # one-shot slot allocator
    slots_count = int(os.environ.get("EGRESSD_ONESHOT_SLOTS", "8"))
    slots_file = os.environ.get("EGRESSD_ONESHOT_SLOTS_FILE")
    app.state.allocator = SlotAllocator(load_slots(slots_count, slots_file))
    app.state.sessions: dict[str, dict[str, Any]] = {}

    # data-plane listener addresses (for capability responses + probe engine)
    app.state.proxy_host = os.environ.get("EGRESSD_PROXY_HOST", "127.0.0.1")
    # Probe engine + benchmark downloads execute INSIDE the egressd
    # container, where the default proxy_host (127.0.0.1) points at nothing:
    # mihomo's SOCKS listeners live in the mihomo container. compose.yaml
    # sets EGRESSD_PROBE_HOST to the mihomo service name. Client-facing
    # capability endpoints keep reporting proxy_host — each client reaches
    # the published endpoint from its OWN vantage point.
    app.state.probe_host = os.environ.get(
        "EGRESSD_PROBE_HOST", app.state.proxy_host)
    app.state.default_proxy_port = int(os.environ.get("EGRESSD_DEFAULT_PROXY_PORT", "18080"))
    app.state.oneshot_proxy_port = int(os.environ.get("EGRESSD_ONESHOT_PROXY_PORT", "18081"))
    app.state.health_url = os.environ.get(
        "EGRESSD_HEALTH_URL", "https://www.gstatic.com/generate_204")
    app.state.test_max_routes = int(os.environ.get("EGRESSD_TEST_MAX_ROUTES", "50"))

    def _select(group: str, mihomo_name: str,
                timeout: float | None = None) -> bool:
        """Point the select group at ``mihomo_name`` and verify the switch.

        Mihomo loads file providers asynchronously after its initial config (and
        again after a hot reload), so a selector PUT aimed at a node that has not
        been loaded into the group yet is rejected with 400 ``proxy not exist``
        and the group silently stays on its previous selection (DIRECT). A 204
        only proves the node was a valid member at the moment of the PUT. We
        therefore PUT, read the group's ``now`` back, and retry through the load
        window until the selection actually takes effect. Returns True when the
        group reflects the requested node.
        """
        if timeout is None:
            timeout = float(os.environ.get("EGRESSD_SELECT_TIMEOUT", "15"))
        deadline = time.monotonic() + timeout
        last_status, last_resp = 0, {}
        while True:
            st, resp = app.state.mihomo.put(f"/proxies/{group}",
                                            {"name": mihomo_name})
            last_status, last_resp = st, resp
            if st == 0:
                # mihomo controller unreachable (offline / degraded). The
                # selection is still persisted in state and will be re-applied
                # on the next reconcile; do not hard-fail the control plane.
                return True
            if st in (200, 204):
                gst, g = app.state.mihomo.get(f"/proxies/{group}")
                if gst == 200 and isinstance(g, dict) and g.get("now") == mihomo_name:
                    return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        # mihomo was reachable but the node never became a valid group member
        # (still loading, or no longer present) -> the switch did NOT take.
        log.warning("selector switch not verified",
                    extra={"group": group, "name": mihomo_name,
                           "last_status": last_status,
                           "last_resp": str(last_resp)[:200]})
        return False

    def _point_group(group: str, mihomo_name: str) -> None:
        _select(group, mihomo_name)

    app.state.engine = ProbeEngine(
        allocator=app.state.allocator,
        socks_host=app.state.probe_host,
        socks_port=app.state.oneshot_proxy_port,
        point_group=_point_group,
        catalog=app.state.catalog,
    )

    # build the initial catalog from the provider files
    rebuild_catalog(app)

    # live config generation + hot reload (spec A3: egressd owns config gen).
    # None in the offline/test path (no mihomo secret + clients) -> provider
    # updates still write the file + rebuild the catalog, just without reload.
    from .configctl import ConfigController, build_egress_config

    egress_cfg = build_egress_config()
    if egress_cfg is not None:
        app.state.configctl = ConfigController(
            egress_cfg,
            os.environ.get("EGRESSD_CONFIG_OUT", "/root/.config/mihomo/config.yaml"),
            app.state.mihomo,
            os.environ.get("EGRESSD_MIHO_CONFIG_PATH"),
        )
    else:
        app.state.configctl = None

    # A single background loop checks URL sources every ten minutes. It uses
    # the same update function as the public manual trigger below.
    import threading
    app.state.provider_update_lock = threading.RLock()

    # ---- auth -------------------------------------------------------------
    def auth_profile(request: Request) -> dict[str, Any]:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        profile = app.state.registry.resolve(header[len("Bearer "):].strip())
        if profile is None:
            raise HTTPException(status_code=401, detail="unknown token")
        return profile

    def client_id(profile: dict[str, Any]) -> str:
        return profile["client_id"]

    def visible(profile: dict[str, Any], route: str) -> bool:
        return app.state.catalog.visible(route, client_id(profile))

    def require_visible(profile: dict[str, Any], route: str) -> None:
        if not app.state.catalog.exists(route):
            raise HTTPException(status_code=400, detail="unknown route")
        if not visible(profile, route):
            raise HTTPException(status_code=404, detail="unknown route")

    def audit(event: str, client_id: str | None = None, **fields: Any) -> None:
        """Structured audit event (spec 40). Never logs secret values."""
        rec: dict[str, Any] = {"event": event}
        if client_id is not None:
            rec["client_id"] = client_id
        rec.update(fields)
        log.info("audit", extra=rec)

    # ---- unauthenticated (compose healthchecks, liveness) ----
    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "uptime_s": round(time.time() - app.state.started_at, 1)}

    @app.get("/v1/ready")
    def ready() -> dict[str, Any]:
        st, _ = app.state.mihomo.get("/version")
        mihomo_ok = st == 200
        return {
            "ready": mihomo_ok,
            "mihomo": "up" if mihomo_ok else "down",
            "clients_registered": len(app.state.registry._token_to_client),
        }

    # ---- identity / status / capabilities (spec 27) ----
    @app.get("/v1/self")
    def self_(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        return {
            "client_id": profile["client_id"],
            "environment": profile.get("environment", "unknown"),
            "modes": profile.get("modes", ["proxy"]),
            "capabilities": [
                "self", "status", "capabilities",
                "routes.list", "routes.get",
                "default.get", "default.set",
                "default.mode.get", "default.mode.set",
                "providers.list", "providers.update",
                "personal.providers.list", "personal.providers.add",
                "personal.providers.snapshot", "personal.providers.refresh",
                "personal.providers.delete",
                "test", "probe-domain", "benchmark", "benchmarks", "best",
                "oneshot.session", "oneshot.list", "oneshot.close",
                "proxy.default-capability",
                "reconcile", "doctor",
                "geo.status", "geo.update",
            ],
            "compat": COMPAT,
        }

    @app.get("/v1/status")
    def status(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        st, ver = app.state.mihomo.get("/version")
        return {
            "client_id": profile["client_id"],
            # top-level version: the egressd API version, present even when
            # mihomo is unreachable (2026-10-15 live run: no version at all
            # -> client cannot negotiate, spec 49.42 check failed)
            "version": VERSION,
            "mihomo": {
                "reachable": st == 200,
                "version": ver.get("version") if isinstance(ver, dict) else None,
            },
            "catalog": {"routes": len(app.state.catalog.filter(client_id=client_id(profile))),
                        "providers": sorted({r.provider for r in app.state.catalog.filter(
                            client_id=client_id(profile)) if r.id != "direct"})},
        }

    @app.get("/v1/capabilities")
    def capabilities(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        modes = profile.get("modes", ["proxy"])
        # A6 capability matrix (spec 21.5): tun availability is per-client,
        # set at provisioning from the environment's real capability
        # (host-main: host-helper if installed; openscience-main: none, since
        # gVisor has no /dev/net/tun / CAP_NET_ADMIN). Consistent with the
        # `mode set tun` / one-shot `mode=tun` checks below.
        tun_available = "tun" in modes
        return {
            "client_id": profile["client_id"],
            "modes": modes,
            "oneshot_slots": len(app.state.allocator),
            "oneshot_free": app.state.allocator.free_count(),
            "tun": {
                "available": tun_available,
                "backend": "host-helper" if tun_available else "none",
                "status": "supported" if tun_available
                          else "unsupported_in_environment",
            },
            "compat": COMPAT,
        }

    # ---- route catalog (spec 13) ----
    @app.get("/v1/routes")
    def list_routes(request: Request, country: str | None = None,
                    provider: str | None = None,
                    healthy: bool | None = None) -> dict[str, Any]:
        profile = auth_profile(request)
        routes = app.state.catalog.filter(country=country, provider=provider,
                                          client_id=client_id(profile))
        if healthy is True:
            routes = [r for r in routes if r.healthy is True]
        elif healthy is False:
            routes = [r for r in routes if r.healthy is False]
        return {
            "client_id": profile["client_id"],
            "count": len(routes),
            "routes": [r.to_dict() for r in routes],
        }

    @app.get("/v1/routes/{route_id}")
    def get_route(route_id: str, request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        r = app.state.catalog.get(route_id)
        if r is None:
            raise HTTPException(status_code=404, detail="unknown route")
        require_visible(profile, route_id)
        out = r.to_dict()
        out["client_id"] = profile["client_id"]
        return out

    # ---- per-client default route / mode (spec 15/16) ----
    def _set_default_route(profile: dict[str, Any], route_id: str,
                           reason: str) -> dict[str, Any]:
        cid = client_id(profile)
        if not _ROUTE_ID_RE.fullmatch(route_id):
            raise HTTPException(status_code=400, detail=f"invalid route id {route_id!r}")
        require_visible(profile, route_id)
        prev, changed = app.state.state.set_route(cid, route_id, reason)
        selector_applied = True
        if changed:
            selector_applied = _select(cid, app.state.catalog.mihomo_name(route_id))
            app.state.metrics.add_default_selection(cid, prev, route_id, reason)
            audit("default_route_change", cid, from_route=prev, to_route=route_id,
                  reason=reason, selector_applied=selector_applied)
        return {
            "client_id": cid,
            "route": route_id,
            "mode": app.state.state.get(cid)["mode"],
            "previous": prev,
            "changed": changed,
            "selector_applied": selector_applied,
        }

    @app.get("/v1/default")
    def get_default(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        st = app.state.state.get(cid)
        r = app.state.catalog.get(st["route"])
        return {
            "client_id": cid,
            "route": st["route"],
            "provider": r.provider if r else None,
            "display_name": r.display_name if r else None,
            "healthy": r.healthy if r else None,
            "mode": st["mode"],
            "reconciled": True,
        }

    @app.put("/v1/default")
    async def set_default(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        route = body.get("route")
        if not route:
            raise HTTPException(status_code=400, detail="'route' is required")
        return _set_default_route(profile, route, "set")

    @app.get("/v1/default/mode")
    def get_default_mode(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        return {"client_id": cid, "mode": app.state.state.get(cid)["mode"]}

    @app.put("/v1/default/mode")
    async def set_default_mode(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        mode = body.get("mode")
        if mode not in ("proxy", "tun"):
            raise HTTPException(status_code=400, detail=f"invalid mode {mode!r}")
        cid = client_id(profile)
        # spec 16: if tun is not available, return explicit unsupported -- no
        # silent fallback to proxy.
        if mode == "tun" and "tun" not in profile.get("modes", ["proxy"]):
            raise HTTPException(status_code=400,
                                detail="unsupported_in_environment: tun not available for this client")
        prev, changed = app.state.state.set_mode(cid, mode)
        if changed:
            audit("default_mode_change", cid, from_mode=prev, to_mode=mode)
        return {"client_id": cid, "mode": mode, "previous": prev, "changed": changed}

    # ---- providers (spec 11/12) ----
    @app.get("/v1/providers")
    def list_providers(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        out = []
        for prov, meta in app.state.provider_manifest.items():
            if meta.get("owner") not in (None, client_id(profile)):
                continue
            nodes = app.state.catalog.filter(provider=prov)
            out.append({
                "id": prov,
                "type": meta.get("type"),
                "node_count": len(nodes),
                "last_update": meta.get("mtime"),
                "last_success": meta.get("status", {}).get("last_success"),
                "last_error": meta.get("status", {}).get("last_error"),
            })
        return {"client_id": client_id(profile), "providers": out}

    # Personal sources are scoped by the authenticated profile. Host profiles
    # keep their source in the backend; container profiles upload a normalized
    # snapshot and retain their URL/source inside the container.
    def _personal_view(meta: dict[str, Any]) -> dict[str, Any]:
        return {key: meta.get(key) for key in
                ("alias", "provider_id", "kind", "node_count", "updated_at")}

    def _commit_personal(meta: dict[str, Any], yaml_text: str) -> dict[str, Any]:
        from .personal import atomic_bytes, load_metadata, metadata_path, save_metadata
        provider = meta["provider_id"]
        path = Path(os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers")) / f"{provider}.yaml"
        old = path.read_bytes() if path.exists() else None
        old_meta = load_metadata(provider)
        try:
            atomic_bytes(path, yaml_text.encode())
            save_metadata(meta)
            if app.state.configctl is not None:
                app.state.configctl.regenerate()
                ok, _ = app.state.configctl.hot_reload()
                if not ok:
                    raise RuntimeError("mihomo reload failed")
            rebuild_catalog(app)
        except Exception:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, old)
            if old_meta is None:
                metadata_path(meta["client_id"], meta["alias"]).unlink(missing_ok=True)
            else:
                save_metadata(old_meta)
            if app.state.configctl is not None:
                app.state.configctl.regenerate()
                app.state.configctl.hot_reload()
            rebuild_catalog(app)
            raise HTTPException(status_code=503, detail="personal provider unavailable")
        affected = app.state.state.reconcile(app.state.catalog)
        for rec in affected:
            _select(rec["client_id"], "DIRECT", timeout=5.0)
        _terminate_sessions_for_removed_routes()
        return _personal_view(meta)

    @app.get("/v1/personal/providers")
    def personal_providers(request: Request) -> dict[str, Any]:
        from .personal import list_owned
        profile = auth_profile(request)
        return {"client_id": client_id(profile),
                "providers": [_personal_view(m) for m in list_owned(client_id(profile))]}

    @app.post("/v1/personal/providers")
    async def add_personal_provider(request: Request) -> dict[str, Any]:
        from adapters.common import normalize_source
        from .personal import (MAX_PROVIDERS, MAX_SOURCE_BYTES, fetch_url,
                               list_owned, load_metadata, provider_id, validate_url,
                               validate_yaml)
        profile = auth_profile(request)
        if profile.get("environment") != "host":
            raise HTTPException(status_code=403, detail="host source operation only")
        body = await _json_body(request)
        alias, kind = body.get("name"), body.get("kind")
        try:
            provider = provider_id(client_id(profile), alias)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid personal provider name") from None
        if load_metadata(provider) or len(list_owned(client_id(profile))) >= MAX_PROVIDERS:
            raise HTTPException(status_code=409, detail="personal provider exists or limit reached")
        source = body.get("source")
        if kind not in ("url", "file") or not isinstance(source, str) or \
                len(source.encode()) > MAX_SOURCE_BYTES:
            raise HTTPException(status_code=400, detail="invalid personal source")
        try:
            if kind == "url":
                validate_url(source)
                raw = fetch_url(source).decode("utf-8")
            else:
                raw = source
            yaml_text, _ = normalize_source(raw)
            yaml_text, count = validate_yaml(yaml_text)
        except (OSError, ValueError, UnicodeError, yaml.YAMLError):
            raise HTTPException(status_code=400, detail="personal source rejected") from None
        meta = {"provider_id": provider, "client_id": client_id(profile),
                "alias": alias, "kind": kind, "source": source,
                "node_count": count, "updated_at": int(time.time())}
        with app.state.provider_update_lock:
            return _commit_personal(meta, yaml_text)

    @app.put("/v1/personal/providers/{alias}/snapshot")
    async def upload_personal_snapshot(alias: str, request: Request) -> dict[str, Any]:
        from .personal import (MAX_PROVIDERS, list_owned, load_metadata,
                               provider_id, validate_yaml)
        profile = auth_profile(request)
        if profile.get("environment") == "host":
            raise HTTPException(status_code=403, detail="container snapshot operation only")
        body = await _json_body(request)
        try:
            provider = provider_id(client_id(profile), alias)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid personal provider name") from None
        old = load_metadata(provider)
        if old is None and len(list_owned(client_id(profile))) >= MAX_PROVIDERS:
            raise HTTPException(status_code=409, detail="personal provider limit reached")
        if old is not None and old.get("client_id") != client_id(profile):
            raise HTTPException(status_code=404, detail="unknown personal provider")
        if not isinstance(body.get("yaml"), str):
            raise HTTPException(status_code=400, detail="normalized YAML required")
        try:
            yaml_text, count = validate_yaml(body["yaml"])
        except (ValueError, yaml.YAMLError):
            raise HTTPException(status_code=400, detail="personal snapshot rejected") from None
        meta = {"provider_id": provider, "client_id": client_id(profile),
                "alias": alias, "kind": "snapshot", "node_count": count,
                "updated_at": int(time.time())}
        with app.state.provider_update_lock:
            return _commit_personal(meta, yaml_text)

    @app.delete("/v1/personal/providers/{alias}")
    def delete_personal_provider(alias: str, request: Request) -> dict[str, Any]:
        from .personal import load_metadata, metadata_path, provider_id
        profile = auth_profile(request)
        try:
            provider = provider_id(client_id(profile), alias)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="unknown personal provider") from None
        meta = load_metadata(provider)
        if meta is None or meta.get("client_id") != client_id(profile):
            raise HTTPException(status_code=404, detail="unknown personal provider")
        path = Path(os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers")) / f"{provider}.yaml"
        with app.state.provider_update_lock:
            old = path.read_bytes() if path.exists() else None
            path.unlink(missing_ok=True)
            metadata_path(client_id(profile), alias).unlink(missing_ok=True)
            try:
                if app.state.configctl is not None:
                    app.state.configctl.regenerate()
                    ok, _ = app.state.configctl.hot_reload()
                    if not ok:
                        raise RuntimeError("mihomo reload failed")
            except Exception:
                from .personal import atomic_bytes, save_metadata
                if old is not None:
                    atomic_bytes(path, old)
                save_metadata(meta)
                if app.state.configctl is not None:
                    try:
                        app.state.configctl.regenerate()
                        app.state.configctl.hot_reload()
                    except Exception:
                        log.error("personal provider rollback reload failed",
                                  extra={"provider": provider})
                rebuild_catalog(app)
                raise HTTPException(status_code=503, detail="personal provider unavailable") from None
            rebuild_catalog(app)
            affected = app.state.state.reconcile(app.state.catalog)
            for rec in affected:
                _select(rec["client_id"], "DIRECT", timeout=5.0)
            _terminate_sessions_for_removed_routes()
        return {"deleted": True, "name": alias}

    @app.post("/v1/personal/providers/{alias}/refresh")
    def refresh_personal_provider(alias: str, request: Request) -> dict[str, Any]:
        from adapters.common import normalize_source
        from .personal import fetch_url, load_metadata, provider_id, validate_yaml
        profile = auth_profile(request)
        try:
            provider = provider_id(client_id(profile), alias)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="unknown personal provider") from None
        meta = load_metadata(provider)
        if meta is None or meta.get("client_id") != client_id(profile):
            raise HTTPException(status_code=404, detail="unknown personal provider")
        if meta.get("kind") != "url":
            raise HTTPException(status_code=400, detail="provider has no backend URL")
        try:
            raw = fetch_url(meta["source"]).decode("utf-8")
            normalized, _ = normalize_source(raw)
            normalized, count = validate_yaml(normalized)
        except (OSError, ValueError, UnicodeError, yaml.YAMLError):
            raise HTTPException(status_code=502, detail="personal source refresh failed") from None
        updated = {**meta, "node_count": count, "updated_at": int(time.time())}
        with app.state.provider_update_lock:
            return _commit_personal(updated, normalized)

    def _update_provider(profile: dict[str, Any], provider_id: str,
                         via: str | None, if_older_than: float | None) -> dict[str, Any]:
        with app.state.provider_update_lock:
            return _update_provider_locked(profile, provider_id, via, if_older_than)

    def _update_provider_locked(profile: dict[str, Any], provider_id: str,
                                via: str | None, if_older_than: float | None) -> dict[str, Any]:
        cid = client_id(profile)
        if via is not None and (not isinstance(via, str) or not _ROUTE_ID_RE.fullmatch(via)):
            raise HTTPException(status_code=400, detail="invalid transport route")
        # re-scan so a changed provider source/config is picked up (spec 12)
        app.state.provider_manifest = scan_providers(
            Path(os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers")))
        manifest = app.state.provider_manifest
        if provider_id not in manifest:
            raise HTTPException(status_code=404, detail=f"unknown provider {provider_id!r}")
        meta = manifest[provider_id]
        if meta.get("owner") not in (None, cid):
            raise HTTPException(status_code=404, detail="unknown provider")
        if meta.get("owner") is not None:
            if via is not None:
                raise HTTPException(status_code=400,
                                    detail="personal source refresh uses direct transport")
            if meta.get("kind") != "url":
                return {"provider": provider_id, "ok": True, "updated": False,
                        "reason": "local_snapshot"}
            from adapters.common import normalize_source
            from .personal import fetch_url, load_metadata, validate_yaml
            personal_meta = load_metadata(provider_id)
            if personal_meta is None:
                raise HTTPException(status_code=404, detail="unknown provider")
            try:
                raw = fetch_url(personal_meta["source"]).decode("utf-8")
                normalized, _ = normalize_source(raw)
                normalized, count = validate_yaml(normalized)
            except (OSError, ValueError, UnicodeError, yaml.YAMLError):
                raise HTTPException(status_code=502, detail="personal source refresh failed") from None
            updated = {**personal_meta, "node_count": count,
                       "updated_at": int(time.time())}
            result = _commit_personal(updated, normalized)
            return {"provider": provider_id, "ok": True, "updated": True,
                    "node_count": result["node_count"]}
        if via and via != "direct":
            require_visible(profile, via)
        # if_older_than (seconds): skip if the artifact is fresh enough
        if if_older_than is not None and meta.get("mtime"):
            age = time.time() - meta["mtime"]
            if age < if_older_than:
                return {"provider": provider_id, "ok": True, "updated": False,
                        "reason": "fresh_enough", "age_s": round(age, 1)}

        nodes_before = [r.id for r in app.state.catalog.filter(provider=provider_id)]
        method = "native"
        # fetch + adapter (controlled fetch; via-route when requested)
        if meta.get("source"):
            method = "adapter"
            try:
                _fetch_and_write_provider(provider_id, meta, via, app)
            except Exception:
                if isinstance(meta["source"], dict):
                    from .provider_sources import write_status
                    write_status(provider_id, ok=False)
                    manifest[provider_id]["status"] = {**meta.get("status", {}),
                                                        "last_error": "update_failed"}
                audit("provider_update", cid, provider=provider_id, ok=False,
                      method=method, via=via, error="update_failed")
                raise HTTPException(status_code=502, detail="provider update failed")
        elif meta.get("type") == "http":
            st, _ = app.state.mihomo.update_provider(provider_id)
            if st not in (200, 204):
                audit("provider_update", cid, provider=provider_id, ok=False,
                      method=method, via=via, error=f"mihomo status {st}")
                raise HTTPException(status_code=502, detail=f"mihomo provider update failed ({st})")

        # re-read inventory + rebuild catalog (spec 12 step 2/3)
        rebuild_catalog(app)
        if isinstance(meta.get("source"), dict):
            from .provider_sources import write_status
            write_status(provider_id, ok=True)
            app.state.provider_manifest[provider_id]["status"] = {
                "last_success": int(time.time()), "last_error": None}
        # hot-reload mihomo so the new node set is live (spec A3: egressd owns
        # config generation + reload). store-selected:true preserves selectors.
        reload_ok, reload_detail = False, "no config controller (offline mode)"
        if app.state.configctl is not None:
            try:
                app.state.configctl.regenerate()
                reload_ok, reload_detail = app.state.configctl.hot_reload()
            except Exception as e:
                reload_ok, reload_detail = False, str(e)
        nodes_after = [r.id for r in app.state.catalog.filter(provider=provider_id)]

        # reconcile: fallback affected clients + terminate affected sessions
        # (spec 12 steps 5/6/9)
        affected = app.state.state.reconcile(app.state.catalog)
        for rec in affected:
            app.state.metrics.add_default_selection(
                rec["client_id"], rec["from_route"], rec["to_route"], rec["reason"])
            audit("reconciliation_fallback_to_direct", rec["client_id"],
                  from_route=rec["from_route"], to_route=rec["to_route"],
                  reason=rec["reason"])
        terminated = _terminate_sessions_for_removed_routes()

        # fast health-check of new/changed nodes (best-effort, spec 12 step 7)
        new_nodes = [n for n in nodes_after if n not in nodes_before]
        _quick_health_check(new_nodes)

        changed = sorted(set(nodes_after) ^ set(nodes_before))
        app.state.metrics.add_provider_update(
            provider_id, cid, True, len(nodes_before), len(nodes_after),
            {"method": method, "via": via})
        audit("provider_update", cid, provider=provider_id, ok=True, method=method,
              via=via, nodes_before=len(nodes_before), nodes_after=len(nodes_after),
              changed=changed, fallbacks=affected, terminated_sessions=terminated,
              reload=reload_ok, reload_detail=reload_detail)
        return {
            "provider": provider_id, "ok": True, "updated": True,
            "method": method, "via": via,
            "nodes_before": len(nodes_before), "nodes_after": len(nodes_after),
            "changed": changed,
            "fallbacks": affected, "terminated_sessions": terminated,
            "reloaded": reload_ok, "reload_detail": reload_detail,
        }

    @app.post("/v1/providers/{provider_id}/update")
    async def update_provider(provider_id: str, request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        return _update_provider(profile, provider_id, body.get("via"),
                                _parse_duration(body.get("if_older_than")))

    @app.post("/v1/providers/update-all")
    async def update_all_providers(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        results = []
        for prov, meta in list(app.state.provider_manifest.items()):
            if meta.get("owner") not in (None, client_id(profile)):
                continue
            try:
                results.append(_update_provider(profile, prov, body.get("via"),
                                                _parse_duration(body.get("if_older_than"))))
            except HTTPException as e:
                results.append({"provider": prov, "ok": False, "updated": False,
                                "error": e.detail})
        return {"client_id": client_id(profile), "results": results}

    def _periodic_updates() -> None:
        from .provider_sources import REFRESH_INTERVAL
        while True:
            time.sleep(REFRESH_INTERVAL)
            manifest = scan_providers(Path(os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers")))
            for provider_id, meta in manifest.items():
                if meta.get("kind") == "url" and meta.get("owner"):
                    try:
                        _update_provider({"client_id": meta["owner"]}, provider_id,
                                         None, REFRESH_INTERVAL)
                    except Exception:
                        log.warning("scheduled personal provider update failed",
                                    extra={"provider": provider_id})
                elif meta.get("type") == "url":
                    try:
                        _update_provider({"client_id": "scheduler"}, provider_id, None, REFRESH_INTERVAL)
                    except Exception:
                        log.warning("scheduled provider update failed", extra={"provider": provider_id})

    @app.on_event("startup")
    def start_provider_updates() -> None:
        threading.Thread(target=_periodic_updates, daemon=True, name="provider-updates").start()
        if app.state.configctl is not None:
            def watch_clients() -> None:
                signature = app.state.registry._state_sig()
                while True:
                    time.sleep(1)
                    current = app.state.registry._state_sig()
                    if current is None or current == signature:
                        continue
                    with app.state.provider_update_lock:
                        try:
                            app.state.registry.reload()
                            app.state.configctl.regenerate()
                            ok, _ = app.state.configctl.hot_reload()
                            if ok:
                                signature = current
                        except Exception:
                            log.warning("client inventory reload failed")
            threading.Thread(target=watch_clients, daemon=True,
                             name="client-inventory").start()

    # ---- health/latency test (spec 23) ----
    @app.post("/v1/test")
    async def test_all(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        return _run_tests(profile, body)

    @app.post("/v1/routes/{route_id}/test")
    async def test_route(route_id: str, request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        return _run_tests(profile, {"route": route_id})

    def _run_tests(profile: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        cid = client_id(profile)
        if "route" in body and body["route"]:
            existing = app.state.catalog.get(body["route"])
            if existing is not None and not visible(profile, body["route"]):
                raise HTTPException(status_code=404, detail="unknown route")
            route_ids = [body["route"]]
        else:
            routes = app.state.catalog.filter(country=body.get("country"),
                                              provider=body.get("provider"), client_id=cid)
            route_ids = [r.id for r in routes][: app.state.test_max_routes]
        results = []
        for rid in route_ids:
            res = _test_one(cid, rid)
            results.append(res)
        return {"client_id": cid, "count": len(results), "results": results}

    def _test_one(cid: str, route_id: str) -> dict[str, Any]:
        r = app.state.catalog.get(route_id)
        if r is None:
            return {"route": route_id, "healthy": False, "latency_ms": None,
                    "error": "unknown_route"}
        if route_id == "direct":
            # baseline: direct HTTP to the health endpoint (spec 23)
            t0 = time.time()
            try:
                req = urllib.request.Request(app.state.health_url, method="HEAD")
                with urllib.request.urlopen(req, timeout=6):
                    pass
                ms = int((time.time() - t0) * 1000)
                healthy = True
            except Exception:
                ms = None
                healthy = False
            app.state.catalog.get(route_id).latency_ms = ms
            app.state.catalog.get(route_id).healthy = healthy
            app.state.metrics.add_health(route_id, cid, healthy, ms, "direct")
            return {"route": route_id, "healthy": healthy, "latency_ms": ms,
                    "source": "direct"}
        # mihomo per-node delay (spec 23)
        st, data = app.state.mihomo.delay(app.state.catalog.mihomo_name(route_id),
                                          app.state.health_url)
        ms = data.get("delay") if isinstance(data, dict) else None
        healthy = st == 200 and isinstance(ms, int)
        r.healthy = healthy
        r.latency_ms = ms if isinstance(ms, int) else None
        r.last_test_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        app.state.metrics.add_health(route_id, cid, healthy,
                                     ms if isinstance(ms, int) else None, "delay")
        return {"route": route_id, "healthy": healthy, "latency_ms": ms,
                "source": "mihomo-delay"}

    # ---- domain probe (spec 24) ----
    @app.post("/v1/probe-domain")
    async def probe_domain(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        domain = body.get("domain")
        if not domain:
            raise HTTPException(status_code=400, detail="'domain' is required")
        # input validation: a domain is a DNS name (RFC 1035-ish) of bounded
        # length; reject anything else before it touches the resolver/URLs.
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                            r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*"
                            r"(?::\d{1,5})?", str(domain)) or len(str(domain)) > 255:
            raise HTTPException(status_code=400, detail=f"invalid domain {str(domain)[:50]!r}")
        expected = _parse_expected(body.get("expected_status", "200-399"))
        check = tuple(body.get("check", ["dns", "tcp", "tls", "http"]))
        parallel = int(body.get("parallel", 8))
        max_routes = int(body.get("max_routes", app.state.test_max_routes))

        if body.get("all"):
            routes = app.state.catalog.filter(country=body.get("country"),
                                              provider=body.get("provider"),
                                              client_id=client_id(profile))
            route_ids = [r.id for r in routes][:max_routes]
        elif body.get("routes"):
            route_ids = list(body["routes"])
        else:
            route_ids = [app.state.state.get(client_id(profile))["route"]]

        cid = client_id(profile)
        for rid in route_ids:
            require_visible(profile, rid)
        kw = dict(scheme=body.get("scheme", "https"), port=body.get("port"),
                  path=body.get("path", "/"), method=body.get("method", "GET"),
                  check=check, expected=expected)
        outcomes = app.state.engine.probe_routes(route_ids, domain, parallel=parallel, **kw)
        results = []
        for o in outcomes:
            d = o.to_dict()
            app.state.metrics.add_probe(
                o.route_id, cid, domain, o.reachable, o.status, o.latency_ms,
                o.error, d)
            results.append(d)
        audit("probe-domain", cid, domain=domain, routes=len(route_ids),
              reachable=sum(1 for o in outcomes if o.reachable))
        return {"client_id": cid, "domain": domain, "count": len(results),
                "results": results}

    # ---- benchmark (spec 25) ----
    @app.post("/v1/benchmark")
    async def benchmark(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        body = await _json_body(request)
        nbytes = _parse_bytes(body.get("bytes", "8M"))
        top = int(body.get("top_latency", 5))
        if body.get("route"):
            existing = app.state.catalog.get(body["route"])
            if existing is not None and not visible(profile, body["route"]):
                raise HTTPException(status_code=404, detail="unknown route")
            route_ids = [body["route"]]
        else:
            # shortlist by latency (spec 25 pipeline: health -> shortlist -> bench)
            routes = app.state.catalog.filter(country=body.get("country"),
                                              provider=body.get("provider"), client_id=cid)
            ranked = [r for r in routes if r.id != "direct" and r.healthy
                      and r.latency_ms is not None]
            ranked.sort(key=lambda r: r.latency_ms)
            route_ids = [r.id for r in ranked[:top]]
            if route_ids:
                route_ids.append("direct")  # baseline
        results = []
        for rid in route_ids:
            res = _benchmark_one(cid, rid, nbytes)
            results.append(res)
        audit("benchmark", cid, routes=len(route_ids), bytes=nbytes)
        return {"client_id": cid, "count": len(results), "results": results}

    def _benchmark_one(cid: str, route_id: str, nbytes: int) -> dict[str, Any]:
        from .probe import OK, SLOT_EXHAUSTED

        r = app.state.catalog.get(route_id)
        if r is None:
            return {"route": route_id, "ok": False, "error": "unknown_route"}
        slot = app.state.allocator.allocate(5.0)
        if slot is None:
            return {"route": route_id, "ok": False, "error": SLOT_EXHAUSTED}
        try:
            if not _select(slot.group, app.state.catalog.mihomo_name(route_id)):
                return {"route": route_id, "ok": False, "mbps": None, "bytes": 0,
                        "error": "selector_not_applied"}
            mbps, got, err = _download_through_socks(
                app.state.probe_host, app.state.oneshot_proxy_port,
                slot.user, slot.password, nbytes)
            ok = err is None
            app.state.metrics.add_benchmark(route_id, cid, mbps, got, ok,
                                            {"error": err})
            return {"route": route_id, "ok": ok, "mbps": mbps, "bytes": got,
                    "error": err}
        finally:
            app.state.allocator.release(slot)

    @app.get("/v1/benchmarks")
    def benchmarks(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        rows = app.state.metrics._query(
            "SELECT ts, route_id, mbps, bytes, ok FROM benchmark_samples"
            " WHERE client_id=? ORDER BY ts DESC LIMIT 100", (cid,))
        return {"client_id": cid, "benchmarks": rows}

    # ---- best (spec 26) ----
    @app.get("/v1/best")
    def best(request: Request, country: str | None = None,
             provider: str | None = None, domain: str | None = None,
             max_latency: int | None = None,
             speed_aware: bool = False) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        routes = app.state.catalog.filter(country=country, provider=provider,
                                          client_id=cid)

        # spec 26: with --domain the route must have a fresh successful probe
        # for that domain, or be in the on-demand probed shortlist. We reuse
        # the stored probe samples (freshness window from the ranking module);
        # routes without a fresh sample are probed on demand (bounded shortlist)
        # so the answer is based on an actual probe, not GEO heuristics.
        fresh_probes: dict[str, dict[str, Any]] = {}
        if domain:
            from .ranking import PROBE_FRESH_S
            now = time.time()
            rows = app.state.metrics._query(
                "SELECT route_id, reachable, status, latency_ms, error_class, ts"
                " FROM probe_samples WHERE domain=? AND client_id=? AND ts>=?"
                " ORDER BY ts DESC", (domain, cid, now - PROBE_FRESH_S))
            seen: set[str] = set()
            for row in rows:
                if row["route_id"] not in seen:
                    seen.add(row["route_id"])
                    fresh_probes[row["route_id"]] = {
                        "reachable": bool(row["reachable"]),
                        "status": row["status"],
                        "latency_ms": row["latency_ms"],
                        "error": row["error_class"],
                        "ts": row["ts"],
                    }
            # on-demand probe for candidate routes without a fresh sample:
            # shortlist = healthy (or untested) routes, bounded, via the
            # route-specific one-shot path (probe engine, safe in parallel).
            missing = [r.id for r in routes
                       if r.id not in fresh_probes
                       and r.healthy is not False]
            missing = missing[: app.state.test_max_routes]
            if missing:
                outcomes = app.state.engine.probe_routes(missing, domain,
                                                         parallel=8)
                for o in outcomes:
                    if o.route_id in seen:
                        continue
                    seen.add(o.route_id)
                    fresh_probes[o.route_id] = {
                        "reachable": o.reachable,
                        "status": o.status,
                        "latency_ms": o.latency_ms,
                        "error": o.error,
                        "ts": time.time(),
                    }
                    app.state.metrics.add_probe(
                        o.route_id, cid, domain, o.reachable, o.status,
                        o.latency_ms, o.error, o.to_dict())

        # GEO verification cache (route_id -> entry) for the confidence tier
        geo_rows = app.state.metrics._query(
            "SELECT route_id, exit_country, verified_at FROM geo_cache")
        geo_cache = {r["route_id"]: r for r in geo_rows}

        from .ranking import rank_routes
        result = rank_routes(routes, domain=domain, fresh_probes=fresh_probes,
                             speed_aware=speed_aware,
                             max_latency_ms=max_latency, geo_cache=geo_cache)
        result["client_id"] = cid
        result["filters"] = {k: v for k, v in
                             (("country", country), ("provider", provider),
                              ("domain", domain), ("max_latency", max_latency),
                              ("speed_aware", speed_aware)) if v is not None}
        audit("best", cid, **result["filters"], best=result["best"],
              candidates=result["count"])
        return result

    # ---- one-shot sessions (spec 18/19/20) ----
    @app.post("/v1/oneshot/session")
    async def oneshot_session(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        body = await _json_body(request)
        route = body.get("route")
        mode = body.get("mode", "proxy")
        if not route:
            raise HTTPException(status_code=400, detail="'route' is required")
        require_visible(profile, route)
        if mode == "tun":
            raise HTTPException(status_code=400,
                                detail="unsupported_in_environment: tun one-shot not available")
        slot = app.state.allocator.allocate(5.0)
        if slot is None:
            raise HTTPException(status_code=503, detail="no free one-shot slot")
        session_id = f"job-{int(time.time() * 1000)}-{slot.index}"
        try:
            # point the slot group at the route (private path, spec 19); verify
            # the switch actually took effect (the node may still be loading).
            if not _select(slot.group, app.state.catalog.mihomo_name(route)):
                raise HTTPException(status_code=503,
                                    detail="slot selector not applied: node not loaded")
        except HTTPException:
            app.state.allocator.release(slot)
            raise
        except Exception:
            app.state.allocator.release(slot)
            raise
        sess = {
            "id": session_id,
            "client_id": cid,
            "route": route,
            "mode": mode,
            "slot": slot.index,
            "started_at": time.time(),
        }
        app.state.sessions[session_id] = sess
        app.state.metrics.open_session(session_id, cid, route, mode, slot.index)
        audit("oneshot_start", cid, session=session_id, route=route, mode=mode,
              slot=slot.index)
        return {
            "id": session_id,
            "client_id": cid,
            "route": route,
            "mode": mode,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "capability": {
                "host": app.state.proxy_host,
                "port": app.state.oneshot_proxy_port,
                "username": slot.user,
                "password": slot.password,
                "scheme": "socks5h",
            },
        }

    @app.delete("/v1/oneshot/session/{session_id}")
    def oneshot_close(session_id: str, request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        sess = app.state.sessions.get(session_id)
        if sess is None or sess["client_id"] != cid:
            raise HTTPException(status_code=404, detail="session not found")
        _release_session(sess)
        audit("oneshot_end", cid, session=session_id, route=sess["route"],
              reason="client_close")
        return {"id": session_id, "closed": True}

    @app.get("/v1/oneshot/active")
    def oneshot_active(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        sessions = [s for s in app.state.sessions.values() if s["client_id"] == cid]
        return {"client_id": cid, "count": len(sessions),
                "sessions": [{
                    "id": s["id"], "client_id": s["client_id"], "route": s["route"],
                    "mode": s["mode"], "slot": s["slot"],
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(s["started_at"])),
                } for s in sessions]}

    def _release_session(sess: dict[str, Any]) -> None:
        app.state.sessions.pop(sess["id"], None)
        app.state.metrics.close_session(sess["id"], "closed")
        # release the slot back to the allocator
        for s in app.state.allocator._slots.values():  # noqa: SLF001
            if s.index == sess["slot"]:
                app.state.allocator.release(s)
                break

    def _terminate_sessions_for_removed_routes() -> int:
        """Terminate active sessions whose route no longer exists (spec 19)."""
        terminated = 0
        for sess in list(app.state.sessions.values()):
            if not app.state.catalog.exists(sess["route"]):
                app.state.metrics.close_session(sess["id"], "route_removed")
                app.state.sessions.pop(sess["id"], None)
                for s in app.state.allocator._slots.values():  # noqa: SLF001
                    if s.index == sess["slot"]:
                        app.state.allocator.release(s)
                        break
                audit("oneshot_end", sess["client_id"], session=sess["id"],
                      route=sess["route"], reason="route_removed")
                terminated += 1
        return terminated

    # ---- proxy default-capability (spec 27) ----
    @app.get("/v1/proxy/default-capability")
    def proxy_default_capability(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        creds = app.state.registry.proxy_creds(cid)
        if creds is None:
            raise HTTPException(status_code=409,
                                detail="no default-proxy credential provisioned for this client")
        user, password = creds
        return {
            "client_id": cid,
            "host": app.state.proxy_host,
            "port": app.state.default_proxy_port,
            "username": user,
            "password": password,
            "scheme": "socks5h",
        }

    # ---- reconcile (spec 12) ----
    @app.post("/v1/reconcile")
    def reconcile(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        rebuild_catalog(app)
        if app.state.configctl is not None:
            app.state.configctl.regenerate()
            app.state.configctl.hot_reload()
        affected = app.state.state.reconcile(app.state.catalog)
        for rec in affected:
            app.state.metrics.add_default_selection(
                rec["client_id"], rec["from_route"], rec["to_route"], rec["reason"])
            # re-point the affected client's selector at direct
            _select(rec["client_id"], "DIRECT", timeout=5.0)
            audit("reconciliation_fallback_to_direct", rec["client_id"],
                  from_route=rec["from_route"], to_route=rec["to_route"],
                  reason=rec["reason"])
        terminated = _terminate_sessions_for_removed_routes()
        # re-assert every client's current default on its selector (idempotent)
        for c, st in app.state.state.all().items():
            _select(c, app.state.catalog.mihomo_name(st["route"]), timeout=5.0)
        audit("reconcile", cid, fallbacks=len(affected), terminated_sessions=terminated)
        return {"client_id": cid, "routes": len(app.state.catalog.filter(client_id=cid)),
                "fallbacks": affected, "terminated_sessions": terminated}

    # ---- doctor (spec 40) ----
    @app.get("/v1/doctor")
    def doctor(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        cid = client_id(profile)
        st, ver = app.state.mihomo.get("/version")
        mihomo_ok = st == 200
        geo = app.state.geo.status()
        checks = {
            "mihomo": "up" if mihomo_ok else "down",
            "mihomo_version": ver.get("version") if isinstance(ver, dict) else None,
            "catalog_routes": len(app.state.catalog.filter(client_id=cid)),
            "providers": sorted({r.provider for r in app.state.catalog.filter(
                client_id=cid) if r.id != "direct"}),
            "default": app.state.state.get(cid),
            "oneshot_slots": len(app.state.allocator),
            "oneshot_free": app.state.allocator.free_count(),
            "geo_all_present": geo.get("all_present"),
            "geo_stale": [n for n, d in geo.get("datasets", {}).items() if d.get("stale")],
        }
        return {"client_id": cid, "ok": mihomo_ok, "checks": checks}

    # ---- GEO (spec 14/41) ----
    @app.get("/v1/geo/status")
    def geo_status(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        st = app.state.geo.status()
        st["client_id"] = profile["client_id"]
        return st

    @app.post("/v1/geo/update")
    async def geo_update(request: Request) -> dict[str, Any]:
        profile = auth_profile(request)
        body = await _json_body(request)
        force = bool(body.get("force", False))
        names = body.get("datasets") or list(DATASET_NAMES)
        try:
            res = app.state.geo.update(names=tuple(names), force=force)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        audit("geo_update", client_id(profile), datasets=names, force=force)
        return {
            "client_id": profile["client_id"],
            "updated": [n for n, r in res.items() if r.get("updated")],
            "results": res,
        }

    return app


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
async def _json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_duration(value: Any) -> float | None:
    """Parse '6h' / '30m' / '120' -> seconds (None if absent/invalid)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|m|h|d)?", s)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _parse_bytes(value: Any) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip().upper()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)B?", s)
    if not m:
        return 8 * 1024 * 1024
    n = float(m.group(1))
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]
    return int(n * mult)


def _parse_expected(value: Any) -> tuple[int, int]:
    if isinstance(value, tuple):
        return value
    s = str(value)
    m = re.fullmatch(r"\s*(\d{3})\s*-\s*(\d{3})\s*", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"\s*(\d{3})\s*", s)
    if m:
        return int(m.group(1)), int(m.group(1))
    return 200, 399


def _fetch_and_write_provider(provider_id: str, meta: dict[str, Any],
                              via: str | None, app: FastAPI | None = None) -> None:
    """Controlled fetch of a provider subscription + adapter + atomic write (spec 12).

    ``via`` optionally names the egress route the fetch is transported through
    (a route-specific one-shot path, never the shared default selector). The
    fetched bytes are normalized by the provider adapter and written atomically
    (with last-known-good) to the provider file.
    """
    from adapters import atomic_write, get_adapter
    from adapters.common import normalize_source
    from .provider_sources import source_dir

    source = meta["source"]
    if isinstance(source, dict):
        if source["type"] == "file":
            if via not in (None, "direct"):
                raise ValueError("static source does not use a transport route")
            raw = (source_dir() / f"{provider_id}.source").read_bytes()
        else:
            url = source["url"]
            if via and via != "direct":
                if app is None or not app.state.catalog.exists(via):
                    raise ValueError("unknown transport route")
                slot = app.state.allocator.allocate(5.0)
                if slot is None:
                    raise ValueError("no free transport slot")
                try:
                    if not app.state.mihomo or not app.state.catalog.mihomo_name(via):
                        raise ValueError("transport route unavailable")
                    name = app.state.catalog.mihomo_name(via)
                    st, _ = app.state.mihomo.put(f"/proxies/{slot.group}", {"name": name})
                    selected, group = app.state.mihomo.get(f"/proxies/{slot.group}")
                    if st not in (200, 204) or selected != 200 or not isinstance(group, dict) or group.get("now") != name:
                        raise ValueError("transport route unavailable")
                    from .provider_fetch import socks_fetch
                    raw = socks_fetch(url, app.state.probe_host, app.state.oneshot_proxy_port,
                                      slot.user, slot.password)
                finally:
                    app.state.allocator.release(slot)
            else:
                from .provider_fetch import direct_fetch
                raw = direct_fetch(url)
    else:
        raw = _fetch_bytes(source, via)
    if isinstance(source, dict):
        yaml_text, _meta = normalize_source(raw.decode("utf-8"))
    else:
        yaml_text, _meta = get_adapter(provider_id).normalize(raw.decode("utf-8"))
    atomic_write(Path(meta["path"]), yaml_text)


def _fetch_bytes(source: str, via: str | None) -> bytes:
    """Fetch ``source`` (a URL or local file). ``via`` is accepted for the
    route-transport contract; the offline/test path reads directly."""
    if source.startswith(("http://", "https://")):
        req = urllib.request.Request(source)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    return Path(source).read_bytes()


def _quick_health_check(route_ids: list[str]) -> None:
    """Best-effort fast delay check of new/changed nodes (spec 12 step 7)."""
    # Kept minimal: the full /v1/test path is the supported health surface.
    # (No-op here to avoid a second mihomo dependency in the update path.)
    return


def _download_through_socks(host: str, port: int, user: str, password: str,
                            nbytes: int) -> tuple[float | None, int, str | None]:
    """Download ``nbytes`` through an authenticated SOCKS5 listener -> (mbps, got, err)."""
    import socket

    url = os.environ.get("EGRESSD_BENCH_URL",
                         "http://speedtest.vibelab.local/download")
    host_part, _, path_part = url.partition("://")
    # connect to the bench host through the socks listener
    s = None
    try:
        s = socket.create_connection((host, port), timeout=15)
        s.sendall(b"\x05\x02\x00\x02")
        g = s.recv(2)
        if g[1] == 0x02:
            u, p = user.encode(), password.encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            r = s.recv(2)
            if r[1] != 0x00:
                return None, 0, "auth rejected"
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_part)]) + host_part.encode()
                  + (80).to_bytes(2, "big"))
        resp = s.recv(4)
        if resp[1] != 0x00:
            return None, 0, f"connect failed {resp[1]}"
        atyp = resp[3]
        if atyp == 1:
            s.recv(4)
        elif atyp == 3:
            ln = s.recv(1)[0]; s.recv(ln)
        elif atyp == 4:
            s.recv(16)
        s.recv(2)
        req = (f"GET {path_part} HTTP/1.1\r\nHost: {host_part}\r\n"
               f"Range: bytes=0-{nbytes - 1}\r\nConnection: close\r\n\r\n")
        s.sendall(req.encode())
        got = 0
        t0 = time.time()
        while got < nbytes:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += len(chunk)
        dt = time.time() - t0
        mbps = (got * 8) / (dt * 1_000_000) if dt > 0 else 0.0
        return round(mbps, 2), got, None
    except OSError as e:
        return None, 0, str(e)
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


app = create_app()


def run() -> None:
    import uvicorn

    listen = os.environ.get("EGRESSD_LISTEN", "127.0.0.1:9091")
    host, _, port = listen.rpartition(":")
    uvicorn.run(app, host=host or "127.0.0.1", port=int(port or "9091"), log_level="warning")


if __name__ == "__main__":
    run()
