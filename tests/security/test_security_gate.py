"""A8 — independent security / integration gate (spec section 46 A8).

Adversarial checks against the public API and the deployment artifacts:

  1. API surface audit: the public API is exactly the documented /v1 set —
     no raw Mihomo passthrough, no arbitrary config patch, no remote exec,
     no client provisioning (spec 27/28, acceptance 15);
  2. every authenticated endpoint rejects missing/unknown tokens (31);
  3. client impersonation: a client cannot read or change another client's
     state by any naming vector (body, query, path) (10, 24);
  4. one-shot session isolation: a client cannot close/list another's
     sessions;
  5. input injection: route IDs with traversal/special characters are
     rejected; oversized inputs handled;
  6. secret scan: provider private keys never reach API responses, the
     metrics DB, or the structured logs (34);
  7. malformed provider input: a broken subscription update fails closed
     and preserves last-known-good (16);
  8. compose static audit: no public binds, no Docker socket, no
     privileged, mihomo admin API unpublished, image pinned (32, 33);
  9. client package audit: the client carries no mihomo admin client and
     no backend secrets; the example configs carry no secrets (34);
 10. CLI/API parity surface: every egressctl control operation maps to a
     documented public endpoint (27.1).

Run:  python -m pytest tests/security/ -v
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

# a provider private key that must NEVER leak (spec 34)
WG_SECRET = "SECURITY-GATE-WG-PRIVATE-KEY-DO-NOT-LEAK-9999"

# ---------------------------------------------------------------------------
# the documented public API (spec 27 + A5 best). Anything else on the app is
# a surface violation.
# ---------------------------------------------------------------------------
DOCUMENTED_ENDPOINTS = {
    ("GET", "/v1/health"),
    ("GET", "/v1/ready"),
    ("GET", "/v1/self"),
    ("GET", "/v1/status"),
    ("GET", "/v1/capabilities"),
    ("GET", "/v1/routes"),
    ("GET", "/v1/routes/{route_id}"),
    ("GET", "/v1/default"),
    ("PUT", "/v1/default"),
    ("GET", "/v1/default/mode"),
    ("PUT", "/v1/default/mode"),
    ("GET", "/v1/providers"),
    ("POST", "/v1/providers/{provider_id}/update"),
    ("POST", "/v1/providers/update-all"),
    ("POST", "/v1/test"),
    ("POST", "/v1/routes/{route_id}/test"),
    ("POST", "/v1/probe-domain"),
    ("POST", "/v1/benchmark"),
    ("GET", "/v1/benchmarks"),
    ("GET", "/v1/best"),
    ("GET", "/v1/proxy/default-capability"),
    ("POST", "/v1/oneshot/session"),
    ("DELETE", "/v1/oneshot/session/{session_id}"),
    ("GET", "/v1/oneshot/active"),
    ("POST", "/v1/reconcile"),
    ("GET", "/v1/doctor"),
    ("GET", "/v1/geo/status"),
    ("POST", "/v1/geo/update"),
}


def _write_blanc(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "FI-Helsinki-1", "type": "socks5", "server": "1.1.1.1", "port": 1080},
        {"name": "FI-WG-1", "type": "wireguard", "server": "wg.example.com",
         "port": 51820, "ip": "10.66.0.2", "private-key": WG_SECRET,
         "public-key": "pub-key-not-a-secret", "allowed-ips": ["0.0.0.0/0"]},
    ]}, sort_keys=False))


def _write_xeovo(path: Path) -> None:
    path.write_text(yaml.safe_dump({"proxies": [
        {"name": "US-New-York-1", "type": "socks5", "server": "2.2.2.1", "port": 1080},
    ]}, sort_keys=False))


@pytest.fixture()
def egress(tmp_path, monkeypatch):
    clients = tmp_path / "clients"
    clients.mkdir()
    for cid, env in (("host-main", "host"), ("openscience-main", "gvisor-container")):
        (clients / f"{cid}.token").write_text(f"tok-{cid}\n")
        (clients / f"{cid}.profile.json").write_text(json.dumps(
            {"client_id": cid, "environment": env, "modes": ["proxy"]}))
        (clients / f"{cid}.proxy-creds").write_text(f"{cid}-user:{cid}-pass\n")

    provs = tmp_path / "providers"
    provs.mkdir()
    _write_blanc(provs / "blanc.yaml")
    _write_xeovo(provs / "xeovo.yaml")

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
        "app": app,
        "client": TestClient(app),
        "provs": provs,
        "tmp": tmp_path,
        "host": {"Authorization": "Bearer tok-host-main"},
        "os": {"Authorization": "Bearer tok-openscience-main"},
    }


def _dump_all_tables(app) -> str:
    db = sqlite3.connect(str(app.state.metrics.path))
    out = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        for row in db.execute(f"SELECT * FROM {t}"):
            out.append(str(row))
    db.close()
    return "\n".join(out)


# ===========================================================================
# 1. API surface audit
# ===========================================================================
class TestApiSurface:
    def test_no_undocumented_endpoints(self, egress):
        """The public API is exactly the documented /v1 set (spec 27)."""
        app = egress["app"]
        actual = set()
        for route in app.routes:
            methods = getattr(route, "methods", None)
            if not methods:
                continue
            for m in methods:
                if m in ("HEAD", "OPTIONS"):
                    continue
                actual.add((m, route.path))
        extra = actual - DOCUMENTED_ENDPOINTS
        assert not extra, f"undocumented public endpoints: {sorted(extra)}"

    def test_no_raw_mihomo_passthrough(self, egress):
        """No endpoint proxies arbitrary mihomo paths (spec 28)."""
        c, host = egress["client"], egress["host"]
        # classic mihomo admin paths must not exist on egressd
        for path in ("/proxies", "/proxies/direct", "/rules", "/configs",
                     "/providers/blanc/config", "/version", "/groups"):
            r = c.get("http://testserver" + path, headers=host)
            assert r.status_code == 404, f"mihomo passthrough at {path}"

    def test_no_arbitrary_config_patch(self, egress):
        """There is no endpoint that accepts a raw config document."""
        c, host = egress["client"], egress["host"]
        for path, body in (("/v1/config", {"rule-providers": {}}),
                           ("/v1/config/patch", {"rules": ["MATCH,DIRECT"]}),
                           ("/v1/reconcile", {"config": {"tun": {"enable": True}}})):
            r = c.post("http://testserver" + path, headers=host, json=body)
            if path == "/v1/reconcile":
                # reconcile exists but ignores any config payload
                assert r.status_code == 200
                assert "config" not in r.json()
            else:
                assert r.status_code == 404, f"config patch endpoint at {path}"

    def test_no_remote_exec(self, egress):
        """No endpoint executes a command (spec 28: arbitrary host command)."""
        c, host = egress["client"], egress["host"]
        for path, body in (("/v1/exec", {"command": "id"}),
                           ("/v1/run", {"argv": ["id"]}),
                           ("/v1/oneshot/session", {"route": "direct",
                                                    "command": "id"})):
            r = c.post("http://testserver" + path, headers=host, json=body)
            if path == "/v1/oneshot/session":
                # the session endpoint exists; a 'command' field is ignored
                assert r.status_code == 200
                assert "command" not in r.json()
            else:
                assert r.status_code == 404, f"exec endpoint at {path}"

    def test_no_client_provisioning(self, egress):
        """Client provisioning is host-side bootstrap, not public API."""
        c, host = egress["client"], egress["host"]
        for path in ("/v1/clients", "/v1/clients/host-main", "/v1/tokens"):
            r = c.post("http://testserver" + path, headers=host, json={})
            assert r.status_code == 404, f"provisioning endpoint at {path}"
            r = c.get("http://testserver" + path, headers=host)
            assert r.status_code == 404


# ===========================================================================
# 2. authentication
# ===========================================================================
class TestAuth:
    def test_all_authenticated_endpoints_require_token(self, egress):
        c = egress["client"]
        for method, path in DOCUMENTED_ENDPOINTS:
            if (method, path) in (("GET", "/v1/health"), ("GET", "/v1/ready")):
                continue
            # parameterize placeholders
            p = path.replace("{route_id}", "direct") \
                    .replace("{provider_id}", "blanc") \
                    .replace("{session_id}", "job-x")
            r = c.request(method, "http://testserver" + p)
            assert r.status_code == 401, f"{method} {path} unauthenticated"

    def test_unknown_token_rejected(self, egress):
        r = egress["client"].get("http://testserver/v1/self",
                                 headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_malformed_authorization_header(self, egress):
        for header in ("Basic tok-host-main", "Bearer", "Bearer  ", "tok-host-main"):
            r = egress["client"].get("http://testserver/v1/self",
                                     headers={"Authorization": header})
            assert r.status_code == 401, f"accepted {header!r}"

    def test_health_requires_no_auth(self, egress):
        assert egress["client"].get("http://testserver/v1/health").status_code == 200
        assert egress["client"].get("http://testserver/v1/ready").status_code == 200


# ===========================================================================
# 3. impersonation / isolation
# ===========================================================================
class TestImpersonation:
    def test_body_client_id_ignored_on_default_set(self, egress):
        c, host, os_ = egress["client"], egress["host"], egress["os"]
        r = c.put("http://testserver/v1/default", headers=host,
                  json={"route": "blanc:FI-Helsinki-1",
                        "client_id": "openscience-main"})
        assert r.status_code == 200
        assert r.json()["client_id"] == "host-main"
        assert c.get("http://testserver/v1/default", headers=os_).json()["route"] == "direct"

    def test_query_client_id_ignored(self, egress):
        c, host, os_ = egress["client"], egress["host"], egress["os"]
        r = c.put("http://testserver/v1/default?client_id=openscience-main",
                  headers=host, json={"route": "blanc:FI-Helsinki-1"})
        assert r.status_code == 200
        assert r.json()["client_id"] == "host-main"
        assert c.get("http://testserver/v1/default", headers=os_).json()["route"] == "direct"

    def test_no_endpoint_names_another_client(self, egress):
        """No public endpoint takes a target client_id in the path."""
        c, host = egress["client"], egress["host"]
        for path in ("/v1/clients/openscience-main/default",
                     "/v1/defaults/openscience-main",
                     "/v1/clients/openscience-main",
                     "/v1/oneshot/active?client_id=host-main"):
            r = c.get("http://testserver" + path, headers=host)
            if path.endswith("client_id=host-main"):
                # the query param is ignored; only own sessions are listed
                assert r.status_code == 200
                assert r.json()["client_id"] == "host-main"
            else:
                assert r.status_code == 404, f"cross-client endpoint at {path}"

    def test_oneshot_session_isolation(self, egress):
        c, host, os_ = egress["client"], egress["host"], egress["os"]
        # openscience creates a session
        r = c.post("http://testserver/v1/oneshot/session", headers=os_,
                   json={"route": "blanc:FI-Helsinki-1"})
        assert r.status_code == 200
        sid = r.json()["id"]
        # host cannot see it
        r = c.get("http://testserver/v1/oneshot/active", headers=host)
        assert r.json()["count"] == 0
        # host cannot close it
        r = c.delete(f"http://testserver/v1/oneshot/session/{sid}", headers=host)
        assert r.status_code == 404
        # the session is still active for openscience
        r = c.get("http://testserver/v1/oneshot/active", headers=os_)
        assert r.json()["count"] == 1
        # cleanup
        c.delete(f"http://testserver/v1/oneshot/session/{sid}", headers=os_)

    def test_default_capability_is_own_only(self, egress):
        c, host, os_ = egress["client"], egress["host"], egress["os"]
        r = c.get("http://testserver/v1/proxy/default-capability", headers=host)
        assert r.json()["client_id"] == "host-main"
        assert r.json()["username"] == "host-main-user"
        # the other client's credential must not be obtainable
        assert "openscience-main-user" not in json.dumps(r.json())


# ===========================================================================
# 4. input injection
# ===========================================================================
class TestInjection:
    @pytest.mark.parametrize("bad", [
        "../etc/passwd",
        "a/b",
        "a%00b",
        "a\nb",
        "a b",
        "a:b:c",
        "a\\b",
        "a;b",
        "a|b",
        "$(id)",
        "`id`",
        "a" * 5000,
    ])
    def test_bad_route_id_rejected(self, egress, bad):
        c, host = egress["client"], egress["host"]
        r = c.put("http://testserver/v1/default", headers=host, json={"route": bad})
        assert r.status_code in (400, 404), f"route id {bad!r} accepted: {r.status_code}"
        # the default is unchanged
        assert c.get("http://testserver/v1/default", headers=host).json()["route"] == "direct"

    def test_bad_mode_rejected(self, egress):
        c, host = egress["client"], egress["host"]
        for mode in ("proxyx", "TUN", "carrier-pigeon", ""):
            r = c.put("http://testserver/v1/default/mode", headers=host,
                      json={"mode": mode})
            assert r.status_code == 400, f"mode {mode!r} accepted"

    def test_oversized_domain_rejected(self, egress):
        c, host = egress["client"], egress["host"]
        r = c.post("http://testserver/v1/probe-domain", headers=host,
                   json={"domain": "a" * 100000})
        # bounded input validation: rejected with 400, no 500 crash
        assert r.status_code == 400

    def test_malformed_domain_rejected(self, egress):
        c, host = egress["client"], egress["host"]
        for bad in ("exa mple.com", "-bad.com", "exa..mple.com", "exa mple.com:80",
                    "example.com/../../etc"):
            r = c.post("http://testserver/v1/probe-domain", headers=host,
                       json={"domain": bad})
            assert r.status_code == 400, f"domain {bad!r} accepted"

    def test_non_json_body_handled(self, egress):
        c, host = egress["client"], egress["host"]
        r = c.request("PUT", "http://testserver/v1/default", headers=host,
                      content=b"not json")
        assert r.status_code == 400


# ===========================================================================
# 5. secret scan (spec 34)
# ===========================================================================
class TestSecretScan:
    def test_secret_not_in_api_responses(self, egress):
        """Exercise the main read endpoints; the WG private key must not
        appear in any response body."""
        c, host = egress["client"], egress["host"]
        bodies = []
        for method, path in (("GET", "/v1/routes"),
                             ("GET", "/v1/routes/blanc:FI-WG-1"),
                             ("GET", "/v1/providers"),
                             ("GET", "/v1/status"),
                             ("GET", "/v1/doctor"),
                             ("GET", "/v1/default"),
                             ("GET", "/v1/best")):
            r = c.request(method, "http://testserver" + path, headers=host)
            bodies.append(r.text)
        for body in bodies:
            assert WG_SECRET not in body

    def test_secret_not_in_metrics_db(self, egress):
        c, host, app = egress["client"], egress["host"], egress["app"]
        # generate some activity (health samples, provider list, etc.)
        c.post("http://testserver/v1/routes/blanc:FI-WG-1/test", headers=host)
        c.get("http://testserver/v1/best", headers=host)
        dump = _dump_all_tables(app)
        assert WG_SECRET not in dump

    def test_secret_not_in_logs(self, egress, monkeypatch):
        """Structured logs must never contain the secret (spec 40)."""
        import egressd.main as m
        captured = []
        real_emit = m.JsonLogHandler.emit

        def spy_emit(self, record):
            captured.append(record.getMessage())
            for k, v in record.__dict__.items():
                if not k.startswith("_") and k not in (
                        "name", "msg", "args", "levelname", "levelno",
                        "pathname", "filename", "module", "exc_info",
                        "exc_text", "stack_info", "lineno", "funcName",
                        "created", "msecs", "relativeCreated", "thread",
                        "threadName", "processName", "process", "taskName"):
                    captured.append(str(v))
            real_emit(self, record)

        monkeypatch.setattr(m.JsonLogHandler, "emit", spy_emit)
        c, host = egress["client"], egress["host"]
        c.get("http://testserver/v1/routes", headers=host)
        c.post("http://testserver/v1/routes/blanc:FI-WG-1/test", headers=host)
        c.get("http://testserver/v1/best", headers=host)
        joined = "\n".join(captured)
        assert WG_SECRET not in joined

    def test_provider_file_never_returned_by_api(self, egress):
        """No endpoint serves the raw provider file (adapter output with
        node details)."""
        c, host = egress["client"], egress["host"]
        for path in ("/v1/providers/blanc/file", "/v1/providers/blanc/raw",
                     "/v1/providers/blanc/subscription",
                     "/v1/providers/blanc.yaml"):
            r = c.get("http://testserver" + path, headers=host)
            assert r.status_code == 404, f"provider file served at {path}"


# ===========================================================================
# 6. malformed provider input (spec 16: failed update preserves LKG)
# ===========================================================================
class TestMalformedProvider:
    def test_broken_subscription_update_fails_closed(self, egress, monkeypatch):
        """A provider source that yields garbage must fail the update with
        502 and leave the last-known-good provider file untouched."""
        c, host, app = egress["client"], egress["host"], egress["app"]
        provs = egress["provs"]
        before = (provs / "blanc.yaml").read_text()

        bad = egress["tmp"] / "bad-sub.txt"
        bad.write_text("this is not a subscription at all\n\x00\x01\x02\n")
        monkeypatch.setenv("EGRESSD_PROVIDER_SOURCE_blanc", str(bad))

        r = c.post("http://testserver/v1/providers/blanc/update", headers=host)
        assert r.status_code == 502
        # last-known-good preserved
        assert (provs / "blanc.yaml").read_text() == before
        # catalog still has the old nodes
        r = c.get("http://testserver/v1/routes", headers=host,
                  params={"provider": "blanc"})
        assert r.json()["count"] == 2

    def test_update_all_reports_per_provider_errors(self, egress, monkeypatch):
        c, host = egress["client"], egress["host"]
        bad = egress["tmp"] / "bad-sub.txt"
        bad.write_text("garbage\n")
        monkeypatch.setenv("EGRESSD_PROVIDER_SOURCE_blanc", str(bad))
        r = c.post("http://testserver/v1/providers/update-all", headers=host)
        assert r.status_code == 200
        results = {x["provider"]: x for x in r.json()["results"]}
        assert results["blanc"]["ok"] is False
        assert results["xeovo"]["ok"] is True  # unaffected provider still updates


# ===========================================================================
# 7. compose static audit (spec 32/33)
# ===========================================================================
@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


class TestCompose:

    def test_no_public_binds(self, compose):
        """Every published port binds to 127.0.0.1 or the Docker gateway
        (spec 36: never 0.0.0.0 / public IP)."""
        for name, svc in compose["services"].items():
            for p in svc.get("ports", []):
                s = str(p)
                # "HOST:HOSTPORT:CONTAINERPORT". The only allowed bind
                # addresses are the host loopback and the Docker gateway
                # variable (whose ${VAR:-default} interpolation contains
                # colons, so it must be recognized before splitting).
                if s.startswith("${"):
                    assert s.startswith("${DOCKER_GATEWAY"), \
                        f"{name}: unexpected bind variable {s!r}"
                else:
                    assert s.split(":")[0] == "127.0.0.1", \
                        f"{name}: public bind {s!r}"

    def test_no_docker_socket(self, compose):
        for name, svc in compose["services"].items():
            for v in svc.get("volumes", []):
                assert "docker.sock" not in str(v), f"{name}: docker socket mounted"

    def test_no_privileged(self, compose):
        for name, svc in compose["services"].items():
            assert svc.get("privileged") is not True, f"{name}: privileged"

    def test_mihomo_admin_api_unpublished(self, compose):
        mihomo = compose["services"]["vibelab-egress-mihomo"]
        for p in mihomo.get("ports", []):
            # the admin controller port 9090 must not be published
            assert "9090" not in str(p).split(":")[-1], \
                f"mihomo admin API published: {p}"

    def test_image_pinned(self, compose):
        img = compose["services"]["vibelab-egress-mihomo"]["image"]
        assert re.search(r":v?\d+\.\d+\.\d+", img), f"mihomo image not pinned: {img}"

    def test_secrets_not_in_environment(self, compose):
        """Secrets must be file mounts, not env vars (docker inspect leak)."""
        for name, svc in compose["services"].items():
            env = svc.get("environment", {})
            env_items = env if isinstance(env, dict) else {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env}
            for k, v in env_items.items():
                assert "SECRET" not in k.upper() or "FILE" in k.upper(), \
                    f"{name}: secret in environment: {k}"
                assert "TOKEN" not in k.upper(), f"{name}: token in environment: {k}"


# ===========================================================================
# 8. client package audit
# ===========================================================================
class TestClientPackage:
    def test_client_has_no_mihomo_admin_client(self):
        """The client package must not contain mihomo admin API code
        (spec 28: the admin secret is egressd-only)."""
        src = ROOT / "client" / "src"
        for f in src.rglob("*.py"):
            text = f.read_text()
            assert "9090" not in text, f"{f}: mihomo admin port referenced"
            assert "/proxies/" not in text, f"{f}: mihomo admin path referenced"
            assert "MIHOMO_SECRET" not in text, f"{f}: mihomo secret referenced"

    def test_example_configs_have_no_secrets(self):
        for f in (ROOT / "client" / "examples").glob("*.yaml"):
            text = f.read_text()
            # no inline token/password values (only file paths)
            assert "password:" not in text
            assert "token:" not in text or "token_file" in text
            assert "Bearer" not in text

    def test_client_uses_only_public_api(self):
        """Every endpoint the client calls must be in the documented set."""
        api_src = (ROOT / "client" / "src" / "vibelab_egress_client" / "api.py").read_text()
        # endpoints appear in double-quoted (f-)strings like
        # f"/v1/providers/{id}/update"; a {..} placeholder is a path
        # parameter unless its expression builds a query string (contains ?)
        raws = re.findall(r'"(/v1/[^"]*)"', api_src)
        normalized = set()
        for s in raws:
            def repl(mm):
                return "" if "?" in mm.group(1) else "{x}"
            normalized.add(re.sub(r"\{([^{}]*)\}", repl, s))
        doc = {re.sub(r"\{[^}]+\}", "{x}", p) for _, p in DOCUMENTED_ENDPOINTS}
        undoc = normalized - doc
        assert not undoc, f"client calls undocumented endpoints: {sorted(undoc)}"


# ===========================================================================
# 8b. strict/TUN helper privilege review (spec 21.4 / 46 A8)
# ===========================================================================
class TestTunHelperPrivilege:
    """The TUN helper is the single privilege escalation in the stack. It must
    stay a narrow, local-only, fixed-protocol interface -- never a generic
    root-exec API (spec 21.4, A8 'bootstrap/helper privilege review')."""

    HELPER = ROOT / "host" / "strict-helper" / "egress-tun-helper.py"

    def test_helper_is_local_only(self):
        """No TCP bind: the helper exposes only a UNIX socket."""
        src = self.HELPER.read_text()
        assert "AF_UNIX" in src
        # it must never create a TCP (AF_INET) socket to serve clients
        assert "AF_INET" not in src, "helper must not open a TCP listener"

    def test_helper_uses_os_credentials(self):
        """Caller identity comes from SO_PEERCRED, never from the body."""
        src = self.HELPER.read_text()
        assert "SO_PEERCRED" in src
        assert "peer_credentials" in src

    def test_helper_fixed_protocol(self):
        """Only the fixed ops exist; there is no free-form command path."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("th_priv", self.HELPER)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["th_priv"] = mod
        spec.loader.exec_module(mod)
        assert mod.ALLOWED_OPS == {"capability", "run", "cleanup"}
        # 'run' is the only op that executes a command, and it validates the
        # route format + upstream allowlist (no arbitrary proxy/route).
        with pytest.raises(mod.ValidationError):
            mod.validate_run({"op": "run", "route": "nope:no",
                              "upstream": {"host": "8.8.8.8", "port": 1080},
                              "command": ["id"]}, set(), set())
        # an unknown op is rejected
        with pytest.raises(mod.ValidationError):
            mod.validate_run({"op": "exec", "command": ["id"]}, set(), set())

    def test_helper_drops_privileges(self):
        """The native backend drops caller UID/GID and all capabilities."""
        src = (ROOT / "host/strict-helper/native_netns.py").read_text()
        assert "setpriv" in src and "--reuid" in src and "--regid" in src
        assert "--bounding-set=-all" in src

    def test_helper_no_command_in_audit(self):
        """Audit events must not log the command argv or environment."""
        src = self.HELPER.read_text()
        # the audit() call sites must not pass the raw command list
        assert "audit(" in src
        # the run op logs route/netns/uid/outcome but never command=
        assert "command=" not in src.split("def op_run")[1]

    def test_host_tun_unit_capability_boundary(self):
        unit = (ROOT / "host/units/vibelab-egress-tun-helper.service").read_text()
        assert "CapabilityBoundingSet=CAP_SYS_ADMIN CAP_NET_ADMIN CAP_SETUID CAP_SETGID CAP_CHOWN CAP_KILL CAP_SETPCAP" in unit
        assert "NoNewPrivileges=true" in unit
        assert "docker.sock" not in unit
        assert "--socket /run/vibelab-egress/tun-helper.sock" in unit

    def test_openscience_client_cannot_reach_helper(self):
        """The OpenScience client config has tun_backend=none and no helper
        socket, so it cannot reach the privileged helper (spec 21.5)."""
        cfg = (ROOT / "client" / "examples" / "client.openscience.yaml").read_text()
        assert "tun_backend: none" in cfg
        assert "tun_helper_socket" not in cfg


# ===========================================================================
# 9. CLI/API parity surface (spec 27.1)
# ===========================================================================
class TestCliApiParity:
    """Every egressctl control operation must map to a documented public
    endpoint. The mapping is the contract; a missing endpoint means the CLI
    feature has no API equivalent (or vice versa)."""

    # (egressctl command, (method, endpoint))
    PARITY = [
        ("status", ("GET", "/v1/status")),
        ("whoami", ("GET", "/v1/self")),
        ("route current", ("GET", "/v1/default")),
        ("route set", ("PUT", "/v1/default")),
        ("mode current", ("GET", "/v1/default/mode")),
        ("mode set", ("PUT", "/v1/default/mode")),
        ("providers", ("GET", "/v1/providers")),
        ("update", ("POST", "/v1/providers/{provider_id}/update")),
        ("list", ("GET", "/v1/routes")),
        ("test", ("POST", "/v1/test")),
        ("probe-domain", ("POST", "/v1/probe-domain")),
        ("benchmark", ("POST", "/v1/benchmark")),
        ("best", ("GET", "/v1/best")),
        ("oneshot active", ("GET", "/v1/oneshot/active")),
        ("geo status", ("GET", "/v1/geo/status")),
        ("geo update", ("POST", "/v1/geo/update")),
        ("doctor", ("GET", "/v1/doctor")),
    ]

    def test_every_cli_command_has_an_endpoint(self, egress):
        app = egress["app"]
        actual = set()
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            for m in methods:
                if m in ("HEAD", "OPTIONS"):
                    continue
                actual.add((m, route.path))
        for cmd, (method, path) in self.PARITY:
            assert (method, path) in actual, \
                f"egressctl {cmd!r} has no API endpoint {method} {path}"

    def test_egress_run_uses_only_data_plane_endpoints(self, egress):
        """egress-run may only call the one-shot session endpoints + the
        default-capability endpoint (never a control mutation)."""
        src = (ROOT / "client" / "src" / "vibelab_egress_client" /
               "egress_run.py").read_text()
        # it must not reference the default-set endpoint
        assert "/v1/default" not in src
        # and it does use the session endpoints (via the api module)
        assert "oneshot_session" in src
