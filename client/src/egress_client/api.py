"""Public egressd API client (stdlib-only, spec section 27/30).

``egressctl`` uses only documented public ``/v1`` endpoints. Every call is
authenticated with the client's own token; the client never sends a target
``client_id`` (identity is bound to the token server-side).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import re
from typing import Any

from .config import ClientConfig


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"API error {status}: {detail}")
        self.status = status
        self.detail = detail


class EgressApi:
    def __init__(self, cfg: ClientConfig, timeout: float = 15.0):
        self.cfg = cfg
        self.timeout = timeout
        self.base = cfg.control_url

    # ---- transport ----
    def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = self.base + path
        data = None
        headers = {"Authorization": f"Bearer {self.cfg.api_token()}"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode())
                detail = payload.get("detail", str(payload))
            except Exception:
                detail = str(e)
            raise ApiError(e.code, detail) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"cannot reach {self.base}: {e.reason}") from None

    # ---- identity / status (spec 27) ----
    def self_(self) -> dict:
        return self._request("GET", "/v1/self")

    def status(self) -> dict:
        return self._request("GET", "/v1/status")

    def capabilities(self) -> dict:
        return self._request("GET", "/v1/capabilities")

    def health(self) -> dict:
        return self._request("GET", "/v1/health")

    def doctor(self) -> dict:
        return self._request("GET", "/v1/doctor")

    # ---- routes / defaults (A3 endpoints; client contract fixed now) ----
    def list_routes(self, **filters: Any) -> dict:
        q = urllib.parse.urlencode({k: v for k, v in filters.items() if v is not None})
        return self._request("GET", f"/v1/routes{'?' + q if q else ''}")

    def get_route(self, route_id: str) -> dict:
        return self._request("GET", f"/v1/routes/{urllib.parse.quote(route_id)}")

    def get_default(self) -> dict:
        return self._request("GET", "/v1/default")

    def set_default(self, route: str) -> dict:
        return self._request("PUT", "/v1/default", {"route": route})

    def get_default_mode(self) -> dict:
        return self._request("GET", "/v1/default/mode")

    def set_default_mode(self, mode: str) -> dict:
        return self._request("PUT", "/v1/default/mode", {"mode": mode})

    # ---- providers ----
    def list_providers(self) -> dict:
        return self._request("GET", "/v1/providers")

    def list_personal(self) -> dict:
        return self._request("GET", "/v1/personal/providers")

    def resolve_route(self, route: str) -> str:
        """Resolve @personal-alias:node for this token to its canonical ID."""
        if not route.startswith("@"):
            return route
        match = re.fullmatch(r"@([A-Za-z0-9][A-Za-z0-9._-]{0,31}):([A-Za-z0-9._-]+)",
                             route)
        if not match:
            raise ValueError("personal route shorthand must look like @alias:node")
        alias, node = match.groups()
        entries = self.list_personal().get("providers", [])
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("alias") != alias:
                continue
            provider = entry.get("provider_id")
            if isinstance(provider, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,99}", provider):
                return f"{provider}:{node}"
        raise ValueError(f"unknown personal route alias @{alias}")

    def add_personal(self, name: str, kind: str, source: str) -> dict:
        return self._request("POST", "/v1/personal/providers",
                             {"name": name, "kind": kind, "source": source})

    def upload_personal(self, name: str, yaml_text: str) -> dict:
        return self._request("PUT", f"/v1/personal/providers/{urllib.parse.quote(name)}/snapshot",
                             {"yaml": yaml_text})

    def refresh_personal(self, name: str) -> dict:
        return self._request("POST", f"/v1/personal/providers/{urllib.parse.quote(name)}/refresh")

    def delete_personal(self, name: str) -> dict:
        return self._request("DELETE", f"/v1/personal/providers/{urllib.parse.quote(name)}")

    def update_provider(self, provider_id: str | None = None, via: str | None = None,
                        if_older_than: str | None = None) -> dict:
        body: dict[str, Any] = {}
        if via:
            body["via"] = via
        if if_older_than:
            body["if_older_than"] = if_older_than
        if provider_id:
            return self._request(
                "POST", f"/v1/providers/{urllib.parse.quote(provider_id)}/update", body
            )
        return self._request("POST", "/v1/providers/update-all", body)

    # ---- tests / probes / benchmarks ----
    def test(self, route_id: str | None = None) -> dict:
        if route_id:
            return self._request("POST", f"/v1/routes/{urllib.parse.quote(route_id)}/test")
        return self._request("POST", "/v1/test")

    def probe_domain(self, domain: str, all_routes: bool = False,
                     **options: Any) -> dict:
        body = {"domain": domain, "all": all_routes}
        body.update({key: value for key, value in options.items() if value is not None})
        return self._request("POST", "/v1/probe-domain", body)

    def benchmark(self, route_id: str, bytes: str | None = None) -> dict:
        body = {"route": route_id}
        if bytes is not None:
            body["bytes"] = bytes
        return self._request("POST", "/v1/benchmark", body)

    def benchmark_shortlist(self, **filters: Any) -> dict:
        """Shortlist benchmark (spec 25): health/latency -> shortlist -> bench.

        ``filters``: country, provider, top_latency, bytes.
        """
        return self._request("POST", "/v1/benchmark",
                             {k: v for k, v in filters.items() if v is not None})

    def benchmarks(self) -> dict:
        return self._request("GET", "/v1/benchmarks")

    def best(self, country: str | None = None, provider: str | None = None,
             domain: str | None = None, max_latency: int | None = None,
             speed_aware: bool = False) -> dict:
        q = urllib.parse.urlencode({
            k: v for k, v in {
                "country": country, "provider": provider, "domain": domain,
                "max_latency": max_latency,
                "speed_aware": "true" if speed_aware else None,
            }.items() if v is not None
        })
        return self._request("GET", f"/v1/best{'?' + q if q else ''}")

    # ---- one-shot ----
    def oneshot_session(self, route: str) -> dict:
        return self._request("POST", "/v1/oneshot/session", {"route": route})

    def oneshot_active(self) -> dict:
        return self._request("GET", "/v1/oneshot/active")

    def oneshot_close(self, session_id: str) -> dict:
        return self._request("DELETE", f"/v1/oneshot/session/{urllib.parse.quote(session_id)}")

    # ---- proxy capability ----
    def proxy_default_capability(self) -> dict:
        return self._request("GET", "/v1/proxy/default-capability")

    # ---- geo ----
    def geo_status(self) -> dict:
        return self._request("GET", "/v1/geo/status")

    def geo_update(self) -> dict:
        return self._request("POST", "/v1/geo/update")
