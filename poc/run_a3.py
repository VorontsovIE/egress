"""A3 live-Mihomo checks (spec A3 "Обязательные tests", data-plane paths).

Brings up a real Mihomo (pinned v1.19.32, ``poc/mihomo``) and a real ``egressd``
(uvicorn), then exercises the public ``/v1`` API against the live data plane:

  1. config + catalog        -- egressd generates the config, mihomo starts,
                                /v1/routes returns the stable route catalog
  2. per-client defaults     -- PUT /v1/default -> the client's default proxy
                                data plane actually hits the selected node
                                (isolation: the other client is untouched)
  3. concurrent one-shot     -- 3 parallel sessions -> 3 distinct nodes through
                                the real one-shot SOCKS listener
  4. provider hot-reload     -- POST /v1/providers/blanc/update -> egressd
                                regenerates the config + PUT /configs; the new
                                node is live and its data plane works
  5. per-client fallback     -- a node is removed; only the client whose default
                                was that node falls back to direct
  6. domain probe            -- POST /v1/probe-domain through the one-shot path
                                -> 200 + NODE:<node> (real data plane)
  7. doctor                  -- /v1/doctor reports mihomo up

Run:  poc/run_a3.py   (from the repo root)
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POC = ROOT / "poc"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))

import yaml  # noqa: E402
from adapters import run_adapter  # noqa: E402
from egressd.configctl import ConfigController, build_egress_config  # noqa: E402

MIHOMO = POC / "mihomo"
FIX = ROOT / "tests" / "fixtures"
GEO_SRC = POC / "geo"
WORK = POC / "run_a3"
RESULTS: dict = {"checks": {}, "meta": {}}

# Ports (distinct from the A2 run so both can coexist if needed).
CTRL = "127.0.0.1:22090"
DEF_PORT = 22080
OS_PORT = 22081
DNS_PORT = 22053
NODE_BASE = 22000
EGRESSD_PORT = 22091
SECRET = "a3-mihomo-secret"

CLIENTS = {
    "host-main": ("host-user", "host-pass"),
    "openscience-main": ("os-user", "os-pass"),
}
OS_SLOTS = 4
OS_SECRETS = [f"os-pass-{i}" for i in range(OS_SLOTS)]

# Non-LAN, non-routable public IP (TEST-NET-3): not in any hard-direct CIDR, so
# a proxy selection is unambiguous (a DIRECT route to it yields no response).
E2E_HOST = "203.0.113.1"
E2E_PORT = 80
# A resolvable domain for the probe (mihomo fake-ip resolves it at the exit).
PROBE_DOMAIN = "example.com"


def log(msg: str):
    print(f"[a3] {msg}", flush=True)


def record(name: str, ok: bool, detail: str):
    RESULTS["checks"][name] = {"ok": bool(ok), "detail": detail}
    log(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def http(method, url, data=None, headers=None, timeout=15):
    headers = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return r.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")
    except Exception as e:
        return None, str(e)


def mihomo_api(method, path, data=None, secret=SECRET, ctrl=CTRL, timeout=15):
    return http(method, f"http://{ctrl}{path}", data,
                {"Authorization": f"Bearer {secret}"}, timeout)


def egress(method, path, data=None, token=None, timeout=25):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http(method, f"http://127.0.0.1:{EGRESSD_PORT}{path}", data, headers, timeout)


def wait_port(host, port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def wait_http(url, timeout=30, secret=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            if secret:
                req.add_header("Authorization", f"Bearer {secret}")
            with urllib.request.urlopen(req, timeout=2) as r:
                if r.status < 500:
                    return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.3)
    return False


def socks_fetch(host, port, user, password, dst_host, dst_port, path="/", timeout=15):
    """Minimal SOCKS5 client -> (status, body) or (None, reason)."""
    try:
        s = socket.create_connection((host, port), timeout)
    except OSError as e:
        return None, f"connect-listener-failed: {e}"
    try:
        s.sendall(b"\x05\x02\x00\x02")
        g = s.recv(2)
        if g[0] != 0x05:
            return None, f"bad greeting {g!r}"
        if g[1] == 0x02:
            u, p = user.encode(), password.encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            r = s.recv(2)
            if r[1] != 0x00:
                return None, f"auth rejected {r!r}"
        elif g[1] != 0x00:
            return None, f"no acceptable method {g!r}"
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(dst_host)]) + dst_host.encode()
                  + dst_port.to_bytes(2, "big"))
        resp = s.recv(4)
        if resp[1] != 0x00:
            return None, f"connect failed code={resp[1]}"
        atyp = resp[3]
        if atyp == 1:
            s.recv(4)
        elif atyp == 3:
            ln = s.recv(1)[0]; s.recv(ln)
        elif atyp == 4:
            s.recv(16)
        s.recv(2)
        req = (f"GET {path} HTTP/1.1\r\nHost: {dst_host}\r\nConnection: close\r\n\r\n")
        s.sendall(req.encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        text = data.decode(errors="replace")
        status = text.split(" ", 2)[1] if " " in text else "?"
        return status, text
    except OSError as e:
        return None, f"transport-error: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


class Proc:
    def __init__(self, name, cmd, cwd=None, env=None, **kw):
        self.name, self.cmd, self.cwd, self.env = name, cmd, cwd, env
        self.kw = kw
        self.proc = None

    def start(self):
        self.logpath = WORK / f"log_{self.name}.txt"
        self._lf = open(self.logpath, "w")
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env,
                                     stdout=self._lf, stderr=subprocess.STDOUT, **self.kw)
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
        if getattr(self, "_lf", None):
            try:
                self._lf.close()
            except Exception:
                pass

    def output(self):
        if getattr(self, "logpath", None) and self.logpath.exists():
            try:
                return self.logpath.read_text(errors="replace")
            except Exception:
                return ""
        return ""


def write_provider_files(home_dir: Path):
    """Produce the provider YAML files via the A2 adapters from the fixtures,
    retargeting the node addresses to the local fake SOCKS5 nodes."""
    prov_dir = home_dir / "providers"
    prov_dir.mkdir(parents=True, exist_ok=True)
    idx = 0

    def retarget(socks5_nodes):
        nonlocal idx
        for p in socks5_nodes:
            p["server"] = "127.0.0.1"
            p["port"] = NODE_BASE + idx
            idx += 1
        return socks5_nodes

    blanc_src = (FIX / "blanc_clash.yaml").read_text()
    blanc_data = yaml.safe_load(blanc_src)
    blanc_data["proxies"] = retarget([p for p in blanc_data["proxies"] if p["type"] == "socks5"])
    blanc_yaml, _ = run_adapter("blanc", yaml.safe_dump(blanc_data, sort_keys=False))
    (prov_dir / "blanc.yaml").write_text(blanc_yaml)

    xeovo_src = (FIX / "xeovo_base64.txt").read_text()
    xeovo_data = yaml.safe_load(base64.b64decode(xeovo_src).decode())
    xeovo_data["proxies"] = retarget([p for p in xeovo_data["proxies"] if p["type"] == "socks5"])
    xeovo_yaml, _ = run_adapter(
        "xeovo",
        base64.b64encode(yaml.safe_dump(xeovo_data, sort_keys=False).encode()).decode())
    (prov_dir / "xeovo.yaml").write_text(xeovo_yaml)

    blanc_clean = [p["name"] for p in yaml.safe_load(blanc_yaml)["proxies"]]
    xeovo_clean = [p["name"] for p in yaml.safe_load(xeovo_yaml)["proxies"]]
    return blanc_clean, xeovo_clean, prov_dir


def setup_clients_dir(clients_dir: Path) -> None:
    clients_dir.mkdir(parents=True, exist_ok=True)
    for cid, (u, p) in CLIENTS.items():
        (clients_dir / f"{cid}.token").write_text(f"tok-{cid}\n")
        (clients_dir / f"{cid}.profile.json").write_text(json.dumps(
            {"client_id": cid,
             "environment": "host" if cid == "host-main" else "gvisor-container",
             "modes": ["proxy"]}))
        (clients_dir / f"{cid}.proxy-creds").write_text(f"{u}:{p}\n")
    (clients_dir / "oneshot-slots.json").write_text(
        json.dumps({f"os_{i}": OS_SECRETS[i] for i in range(OS_SLOTS)}))


def write_blanc_source(home: Path) -> None:
    """Write the blanc subscription source (3 nodes, incl. FI-Helsinki-3) and
    point egressd at it. Must run BEFORE egressd starts: egressd is a separate
    uvicorn subprocess, so the source env var has to be in the environment at
    spawn time for its provider manifest to carry it (spec 12 controlled fetch).
    The first two nodes keep their live fake-node ports; FI-Helsinki-3 gets its
    own fake node at NODE_BASE+4 (started in check 4, before its data-plane use)."""
    blanc_src = (FIX / "blanc_clash.yaml").read_text()
    blanc_data = yaml.safe_load(blanc_src)
    socks = [p for p in blanc_data["proxies"] if p["type"] == "socks5"]
    for i, p in enumerate(socks[:2]):
        p["server"] = "127.0.0.1"
        p["port"] = NODE_BASE + i
    socks.append({"name": "FI - Helsinki 3", "type": "socks5",
                  "server": "127.0.0.1", "port": NODE_BASE + 4})
    src_file = home / "blanc_source.yaml"
    src_file.write_text(yaml.safe_dump({"proxies": socks}, sort_keys=False))
    os.environ["EGRESSD_PROVIDER_SOURCE_blanc"] = str(src_file)


def set_egress_env(home: Path, clients_dir: Path, secrets_dir: Path) -> None:
    os.environ["MIHOMO_API_URL"] = f"http://{CTRL}"
    os.environ["MIHOMO_SECRET_FILE"] = str(secrets_dir / "mihomo_api_secret")
    os.environ["EGRESSD_CLIENTS_DIR"] = str(clients_dir)
    os.environ["EGRESSD_PROVIDERS_DIR"] = str(home / "providers")
    os.environ["EGRESSD_STATE_DIR"] = str(WORK / "state")
    os.environ["EGRESSD_METRICS_DB"] = str(WORK / "state" / "metrics.sqlite")
    os.environ["EGRESSD_GEO_DIR"] = str(WORK / "geo")
    os.environ["EGRESSD_ONESHOT_SLOTS"] = str(OS_SLOTS)
    os.environ["EGRESSD_ONESHOT_SLOTS_FILE"] = str(clients_dir / "oneshot-slots.json")
    os.environ["EGRESSD_CONFIG_OUT"] = str(home / "config.yaml")
    os.environ["EGRESSD_PROXY_HOST"] = "127.0.0.1"
    os.environ["EGRESSD_DEFAULT_PROXY_PORT"] = str(DEF_PORT)
    os.environ["EGRESSD_ONESHOT_PROXY_PORT"] = str(OS_PORT)
    os.environ["EGRESSD_LISTENER_LISTEN"] = "127.0.0.1"
    os.environ["EGRESSD_DNS_LISTEN"] = f"127.0.0.1:{DNS_PORT}"
    os.environ["EGRESSD_HEALTH_URL"] = "https://www.gstatic.com/generate_204"


def start_nodes(names, offset=0, logname="nodes.jsonl"):
    """Start fake SOCKS5 nodes at NODE_BASE+offset+i for names[i]."""
    base = NODE_BASE + offset
    logpath = WORK / logname
    if logpath.exists():
        logpath.unlink()
    p = Proc(f"nodes_{offset}", [sys.executable, str(POC / "fake_node.py"),
                                 "--base-port", str(base),
                                 "--nodes", ",".join(names), "--log", str(logpath)])
    p.start()
    return p, wait_port("127.0.0.1", base, 15)


def start_mihomo(home: Path, cfg_out: Path, name="mihomo", ctrl=CTRL):
    for f in ("GeoIP.dat", "GeoSite.dat", "asn.mmdb"):
        dst = home / f
        if not dst.exists():
            shutil.copy(GEO_SRC / f, dst)
    p = Proc(name, [str(MIHOMO), "-d", str(home), "-f", str(cfg_out)], cwd=str(home))
    p.start()
    return p, wait_http(f"http://{ctrl}/version", 25, secret=SECRET)


def start_egressd():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + str(ROOT / "egressd" / "src")
    p = Proc("egressd",
             [sys.executable, "-m", "uvicorn", "egressd.main:app",
              "--host", "127.0.0.1", "--port", str(EGRESSD_PORT), "--log-level", "warning"],
             cwd=str(ROOT), env=env)
    p.start()
    return p, wait_http(f"http://127.0.0.1:{EGRESSD_PORT}/v1/health", 30)


def wait_providers_loaded(ctrl=CTRL, want=(), timeout=20):
    want = set(want)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, pr = mihomo_api("GET", "/proxies", ctrl=ctrl)
        names = set(pr.get("proxies", {}).keys()) if isinstance(pr, dict) else set()
        # provider nodes are group members; read from the host-main group `all`
        _, hm = mihomo_api("GET", "/proxies/host-main", ctrl=ctrl)
        members = set(hm.get("all", [])) if isinstance(hm, dict) else set()
        if want.issubset(names | members):
            return True
        time.sleep(0.4)
    return False


PROCS: list[Proc] = []


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    procs: list[Proc] = PROCS

    home = WORK / "mihomo"
    home.mkdir(parents=True)
    clients_dir = WORK / "clients"
    secrets_dir = WORK / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "mihomo_api_secret").write_text(SECRET + "\n")
    setup_clients_dir(clients_dir)
    set_egress_env(home, clients_dir, secrets_dir)

    # provider files must exist before egressd generates the config (in the
    # real deployment the subscriptions are already on disk at install time).
    blanc_clean, xeovo_clean, _ = write_provider_files(home)
    log(f"blanc clean nodes: {blanc_clean}")
    log(f"xeovo clean nodes: {xeovo_clean}")

    # ---- initial config: egressd owns generation (spec A3) ----
    egress_cfg = build_egress_config()
    if egress_cfg is None:
        log("FATAL: build_egress_config returned None (missing secret/clients)")
        return 1
    egress_cfg.default_port = DEF_PORT
    egress_cfg.oneshot_port = OS_PORT
    egress_cfg.controller = CTRL
    egress_cfg.dns_listen = f"127.0.0.1:{DNS_PORT}"
    egress_cfg.listener_listen = "127.0.0.1"
    egress_cfg.hard_direct_cidrs = (["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                     "169.254.0.0/16", "172.17.0.0/16"])
    cc = ConfigController(egress_cfg, home / "config.yaml", None)
    cc.regenerate()
    log("egressd generated the initial mihomo config")
    # blanc subscription source for the check-4 hot-reload (env must be set
    # before egressd spawns so its manifest carries the source).
    write_blanc_source(home)
    blanc0 = "blanc-" + blanc_clean[0]
    blanc1 = "blanc-" + blanc_clean[1]
    xeovo0 = "xeovo-" + xeovo_clean[0]
    blanc0_c, blanc1_c, xeovo0_c = blanc_clean[0], blanc_clean[1], xeovo_clean[0]

    # ---- fake nodes (one SOCKS5 server per provider node, named by clean id) ----
    all_nodes = blanc_clean + xeovo_clean
    node_proc, nodes_ok = start_nodes(all_nodes)
    procs.append(node_proc)
    if not nodes_ok:
        log("fake nodes failed to start")
        return 1

    mihomo, up = start_mihomo(home, home / "config.yaml")
    procs.append(mihomo)
    if not up:
        log("mihomo did not come up")
        log(mihomo.output()[-2000:])
        return 1
    wait_providers_loaded(want={blanc0, blanc1, xeovo0})

    egressd, egress_up = start_egressd()
    procs.append(egressd)
    if not egress_up:
        log("egressd did not come up")
        log(egressd.output()[-2000:])
        return 1
    log("egressd is up")

    H = "tok-host-main"
    O = "tok-openscience-main"

    # =========================================================================
    # 1. config + catalog
    # =========================================================================
    st, ver = mihomo_api("GET", "/version")
    _, routes = egress("GET", "/v1/routes", token=H)
    route_ids = {r["id"] for r in routes.get("routes", [])}
    need = {"direct", f"blanc:{blanc0_c}", f"blanc:{blanc1_c}", f"xeovo:{xeovo0_c}"}
    record("config_catalog",
           st == 200 and need.issubset(route_ids) and routes.get("count") == len(all_nodes) + 1,
           f"mihomo={ver.get('version') if isinstance(ver, dict) else '?'}, "
           f"routes={routes.get('count')}, ids={sorted(route_ids)}")

    # =========================================================================
    # 2. per-client defaults (real data plane)
    # =========================================================================
    _, r = egress("PUT", "/v1/default", {"route": f"blanc:{blanc0_c}"}, token=H)
    _, r2 = egress("PUT", "/v1/default", {"route": f"xeovo:{xeovo0_c}"}, token=O)
    time.sleep(0.5)
    _, gh = egress("GET", "/v1/default", token=H)
    _, go = egress("GET", "/v1/default", token=O)
    # debug: what does mihomo think the selectors are right now?
    _, dbg_h = mihomo_api("GET", "/proxies/host-main")
    _, dbg_o = mihomo_api("GET", "/proxies/openscience-main")
    log(f"DEBUG mihomo now: host-main={dbg_h.get('now') if isinstance(dbg_h, dict) else dbg_h} "
        f"os={dbg_o.get('now') if isinstance(dbg_o, dict) else dbg_o}")
    # data plane: each client's default proxy hits its own node
    sh, bh = socks_fetch("127.0.0.1", DEF_PORT, "host-user", "host-pass", E2E_HOST, E2E_PORT)
    so, bo = socks_fetch("127.0.0.1", DEF_PORT, "os-user", "os-pass", E2E_HOST, E2E_PORT)
    record("per_client_defaults",
           gh.get("route") == f"blanc:{blanc0_c}" and go.get("route") == f"xeovo:{xeovo0_c}"
           and sh == "200" and f"NODE:{blanc0_c}" in bh
           and so == "200" and f"NODE:{xeovo0_c}" in bo,
           f"host={gh.get('route')}(NODE:{blanc0_c if sh == '200' else '?'}) "
           f"os={go.get('route')}(NODE:{xeovo0_c if so == '200' else '?'})")

    # =========================================================================
    # 3. concurrent one-shot (real one-shot SOCKS listener)
    # =========================================================================
    sess = []
    for rid in (f"blanc:{blanc0_c}", f"xeovo:{xeovo0_c}", f"blanc:{blanc1_c}"):
        _, s = egress("POST", "/v1/oneshot/session", {"route": rid}, token=H)
        sess.append(s)
    active = egress("GET", "/v1/oneshot/active", token=H)[1]
    active_slots = {s["slot"] for s in active.get("sessions", [])}

    def fetch_one(s):
        cap = s["capability"]
        return socks_fetch(cap["host"], cap["port"], cap["username"], cap["password"],
                           E2E_HOST, E2E_PORT)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(fetch_one, s) for s in sess]
        fetched = [f.result() for f in futs]
    # each session must hit its own node
    want_nodes = [blanc0_c, xeovo0_c, blanc1_c]
    ok3 = len(active_slots) == 3 and all(
        f[0] == "200" and f"NODE:{want_nodes[i]}" in f[1] for i, f in enumerate(fetched))
    record("concurrent_oneshot",
           ok3,
           " | ".join(f"sess{i}->NODE:{want_nodes[i]}:{'ok' if f[0] == '200' and f'NODE:{want_nodes[i]}' in f[1] else 'FAIL'}"
                      for i, f in enumerate(fetched)))
    # close the sessions
    for s in sess:
        egress("DELETE", f"/v1/oneshot/session/{s['id']}", token=H)

    # =========================================================================
    # 4. provider hot-reload (egressd regenerates config + PUT /configs)
    # =========================================================================
    # The blanc subscription source (3 nodes, incl. FI-Helsinki-3) was written
    # at startup (write_blanc_source) so egressd's manifest carries it. Bring up
    # the live fake node for the new node, then trigger the controlled update:
    # egressd fetches the source, rewrites blanc.yaml (3 nodes), rebuilds the
    # catalog, hot-reloads mihomo, and the new node's data plane becomes real.
    new_node_c = "FI-Helsinki-3"
    # the source declares FI-Helsinki-3 at NODE_BASE+4 -> start its fake node
    # there (offset 4) so the data plane is real and doesn't collide with the
    # first node set (NODE_BASE..NODE_BASE+2).
    new_node_proc, new_node_ok = start_nodes([new_node_c], offset=4,
                                             logname="nodes_new.jsonl")
    procs.append(new_node_proc)
    if not new_node_ok:
        log("new-node fake node failed to start")
        return 1
    st4, upd = egress("POST", "/v1/providers/blanc/update", {}, token=H)
    # wait for the new node to be live in mihomo (post hot-reload)
    new_node = "blanc-FI-Helsinki-3"
    loaded = wait_providers_loaded(want={new_node}, timeout=25)
    # data plane for the new node via a one-shot session
    _, s4 = egress("POST", "/v1/oneshot/session", {"route": f"blanc:{new_node_c}"}, token=H)
    cap = (s4 or {}).get("capability", {})
    s4st, s4body = socks_fetch(cap.get("host", "127.0.0.1"), cap.get("port", OS_PORT),
                               cap.get("username", ""), cap.get("password", ""),
                               E2E_HOST, E2E_PORT)
    if isinstance(s4, dict) and s4.get("id"):
        egress("DELETE", f"/v1/oneshot/session/{s4['id']}", token=H)
    record("provider_hot_reload",
           st4 == 200 and upd.get("reloaded") is True
           and upd.get("nodes_after") == 3
           and f"blanc:{new_node_c}" in (upd.get("changed") or [])
           and loaded
           and s4st == "200" and f"NODE:{new_node_c}" in (s4body or ""),
           f"reloaded={upd.get('reloaded')} ({upd.get('reload_detail')}), "
           f"nodes_after={upd.get('nodes_after')}, changed={upd.get('changed')}, "
           f"new_node_live={loaded}, "
           f"new_node_data_plane={'NODE:' + new_node_c if s4st == '200' else s4st}")

    # =========================================================================
    # 5. per-client fallback to direct (only the affected client)
    # =========================================================================
    # host -> blanc:FI-Helsinki-1 (will be removed); os -> xeovo:US-New-York-1 (stays)
    egress("PUT", "/v1/default", {"route": f"blanc:{blanc0_c}"}, token=H)
    egress("PUT", "/v1/default", {"route": f"xeovo:{xeovo0_c}"}, token=O)
    # rewrite blanc without FI-Helsinki-1 (keep FI-Helsinki-2 + FI-Helsinki-3)
    prov = home / "providers" / "blanc.yaml"
    cur = yaml.safe_load(prov.read_text())
    keep = [p for p in cur["proxies"] if p["name"] != blanc0_c]
    (prov).write_text(yaml.safe_dump({"proxies": keep}, sort_keys=False))
    st5, rec5 = egress("POST", "/v1/reconcile", None, token=H)
    time.sleep(0.5)
    # re-point host's selector (reconcile does this) and verify data plane = direct
    sd, bd = socks_fetch("127.0.0.1", DEF_PORT, "host-user", "host-pass",
                         E2E_HOST, E2E_PORT, timeout=5)
    _, gh5 = egress("GET", "/v1/default", token=H)
    _, go5 = egress("GET", "/v1/default", token=O)
    record("per_client_fallback",
           st5 == 200 and len(rec5.get("fallbacks", [])) == 1
           and rec5["fallbacks"][0]["client_id"] == "host-main"
           and rec5["fallbacks"][0]["to_route"] == "direct"
           and gh5.get("route") == "direct"
           and go5.get("route") == f"xeovo:{xeovo0_c}"
           and sd is None and "NODE:" not in (bd or ""),
           f"fallbacks={rec5.get('fallbacks')}, host={gh5.get('route')} "
           f"(direct data plane status={sd}), os={go5.get('route')} (untouched)")

    # =========================================================================
    # 6. domain probe (real one-shot data plane)
    # =========================================================================
    st6, probe = egress("POST", "/v1/probe-domain",
                        {"domain": PROBE_DOMAIN, "routes": [f"blanc:{blanc1_c}"],
                         "scheme": "http", "port": E2E_PORT,
                         "check": ["tcp", "http"]}, token=H, timeout=30)
    res6 = probe.get("results", [{}])[0]
    record("domain_probe",
           st6 == 200 and res6.get("reachable") is True and res6.get("status") == 200
           and res6.get("error") == "ok",
           f"route={res6.get('route')}, reachable={res6.get('reachable')}, "
           f"status={res6.get('status')}, error={res6.get('error')}, "
           f"latency_ms={res6.get('latency_ms')}")

    # =========================================================================
    # 7. doctor
    # =========================================================================
    st7, doc = egress("GET", "/v1/doctor", token=H)
    record("doctor",
           st7 == 200 and doc.get("ok") is True
           and doc.get("checks", {}).get("mihomo") == "up",
           f"ok={doc.get('ok')}, mihomo={doc.get('checks', {}).get('mihomo')}, "
           f"routes={doc.get('checks', {}).get('catalog_routes')}")

    # ---- results ----
    RESULTS["meta"] = {
        "mihomo": ver.get("version") if isinstance(ver, dict) else "?",
        "blanc_nodes": blanc_clean, "xeovo_nodes": xeovo_clean,
    }
    (WORK / "results.json").write_text(json.dumps(RESULTS, indent=2))
    n_pass = sum(1 for c in RESULTS["checks"].values() if c["ok"])
    n_tot = len(RESULTS["checks"])
    log(f"\nA3 LIVE: {n_pass}/{n_tot} passed")
    return 0 if n_pass == n_tot else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for p in PROCS:
            p.stop()
    sys.exit(rc)
