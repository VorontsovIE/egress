"""A2 live-Mihomo checks (spec section 46, A2 verification list).

Brings up a real Mihomo (pinned v1.19.32, ``poc/mihomo``) with the production
config generator and provider files produced by the A2 adapters from the
fixtures, then runs the live checks:

  1. config validation            -- mihomo starts, /version, groups+rules present
  2. node collisions              -- duplicate node name -> mihomo refuses to start
  3. client selector isolation    -- per-client defaults independent
  4. direct route                 -- client set to DIRECT -> no node response
  5. concurrent one-shot          -- 3 parallel slots -> 3 different nodes
  6. mapping scale                -- 300 nodes -> fixed rule count (independent)
  7. GEO update                   -- geo-auto-update set, files present, update
                                     rewrites files, mihomo stays healthy
  8. Docker bypass precedence     -- Docker CIDR rule before IN-USER; a Docker
                                     destination stays DIRECT even via a proxy

Run:  poc/run_a2.py   (from the repo root)
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
from egressd.configgen import (  # noqa: E402
    ClientDef, EgressConfig, OneShotSlot, ProviderDef,
)
from egressd.geo_update import GeoConfig, GeoManager  # noqa: E402

MIHOMO = POC / "mihomo"
FIX = ROOT / "tests" / "fixtures"
GEO_SRC = POC / "geo"
WORK = POC / "run_a2"
RESULTS: dict = {"checks": {}, "meta": {}}

# Ports (distinct from the PoC run so both can coexist if needed).
CTRL = "127.0.0.1:21090"
DEF_PORT = 21080
OS_PORT = 21081
DNS_PORT = 21053
NODE_BASE = 21000
SECRET = "a2-mihomo-secret"

CLIENTS = {
    "host-main": ("host-user", "host-pass"),
    "container-main": ("os-user", "os-pass"),
}
OS_SLOTS = 4
OS_SECRETS = [f"os-pass-{i}" for i in range(OS_SLOTS)]

# Non-LAN, non-routable public IP (TEST-NET-3): not in any hard-direct CIDR,
# so a proxy selection is unambiguous (a DIRECT route to it yields no response).
E2E_HOST = "203.0.113.1"
E2E_PORT = 80
# A Docker-bridge address (in the hard-direct Docker CIDR 172.17.0.0/16).
DOCKER_HOST = "172.17.0.99"


def log(msg: str):
    print(f"[a2] {msg}", flush=True)


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
    def __init__(self, name, cmd, cwd=None, **kw):
        self.name, self.cmd, self.cwd, self.kw = name, cmd, cwd, kw
        self.proc = None

    def start(self):
        self.logpath = WORK / f"log_{self.name}.txt"
        self._lf = open(self.logpath, "w")
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdout=self._lf,
                                     stderr=subprocess.STDOUT, **self.kw)
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
    """Produce the provider YAML files via the A2 adapters from the fixtures.

    Mihomo only accepts provider paths that are a subpath of its home dir
    (``-d``), so the files are written into ``<home>/providers/`` -- matching
    the real deployment where providers are mounted into the mihomo config dir.

    The fixture node addresses (TEST-NET-2) are rewritten to the local fake
    SOCKS5 nodes (``127.0.0.1:21000+``) so the live data plane actually
    reaches them. The adapters emit clean node names; the ``blanc-``/``xeovo-``
    tag is applied by the provider config's ``additional-prefix`` (spec 11.1),
    so the mihomo-visible name is ``<prefix><clean>``.
    """
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


def build_cfg(providers, hard_direct=None):
    clients = [ClientDef(cid, u, p) for cid, (u, p) in CLIENTS.items()]
    slots = [OneShotSlot(i, f"os_{i}", OS_SECRETS[i]) for i in range(OS_SLOTS)]
    return EgressConfig(
        mihomo_secret=SECRET, clients=clients, providers=providers,
        oneshot_slots=slots, default_port=DEF_PORT, oneshot_port=OS_PORT,
        controller=CTRL, dns_listen=f"127.0.0.1:{DNS_PORT}",
        hard_direct_cidrs=hard_direct or ["10.0.0.0/8", "172.16.0.0/12",
                                          "192.168.0.0/16", "169.254.0.0/16",
                                          "172.17.0.0/16"],
    )


def start_nodes(names):
    logpath = WORK / "nodes.jsonl"
    if logpath.exists():
        logpath.unlink()
    p = Proc("nodes", [sys.executable, str(POC / "fake_node.py"),
                       "--base-port", str(NODE_BASE),
                       "--nodes", ",".join(names), "--log", str(logpath)])
    p.start()
    return p, wait_port("127.0.0.1", NODE_BASE, 15)


def start_mihomo(cfg: EgressConfig, name="mihomo", ctrl=CTRL):
    d = WORK / name
    d.mkdir(exist_ok=True)
    # GEO files (real datasets from the PoC) so geodata-mode has data to load.
    for f in ("GeoIP.dat", "GeoSite.dat", "asn.mmdb"):
        dst = d / f
        if not dst.exists():
            shutil.copy(GEO_SRC / f, dst)
    cfg.controller = ctrl
    data = cfg.generate()
    (d / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    p = Proc(name, [str(MIHOMO), "-d", str(d), "-f", str(d / "config.yaml")], cwd=str(d))
    p.start()
    return p, wait_http(f"http://{ctrl}/version", 25, secret=SECRET)


def wait_providers_loaded(ctrl=CTRL, want=(), timeout=20):
    """Wait until every wanted provider node name appears in /proxies.

    Mihomo loads file providers asynchronously after the controller is up, so
    querying /proxies immediately after start can miss the nodes.
    """
    want = set(want)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, pr = mihomo_api("GET", "/proxies", ctrl=ctrl)
        names = set(pr.get("proxies", {}).keys()) if isinstance(pr, dict) else set()
        if want.issubset(names):
            return True
        time.sleep(0.4)
    return False


PROCS: list[Proc] = []


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    procs: list[Proc] = PROCS

    # ---- mihomo home dir + providers via adapters (inside home) ----
    home = WORK / "mihomo"
    home.mkdir(parents=True, exist_ok=True)
    blanc_clean, xeovo_clean, prov_dir = write_provider_files(home)
    log(f"blanc clean nodes: {blanc_clean}")
    log(f"xeovo clean nodes: {xeovo_clean}")
    # clean names = what the fake nodes are called (-> NODE:<clean> body);
    # prefixed names = what mihomo sees (additional-prefix, spec 11.1).
    blanc0, blanc1 = "blanc-" + blanc_clean[0], "blanc-" + blanc_clean[1]
    xeovo0 = "xeovo-" + xeovo_clean[0]
    blanc0_c, blanc1_c, xeovo0_c = blanc_clean[0], blanc_clean[1], xeovo_clean[0]

    # ---- fake nodes (one SOCKS5 server per provider node, named by clean id) ----
    all_nodes = blanc_clean + xeovo_clean
    node_proc, nodes_ok = start_nodes(all_nodes)
    procs.append(node_proc)
    if not nodes_ok:
        log("fake nodes failed to start")
        return 1

    providers = [
        ProviderDef(name="blanc", type="file", path=str(prov_dir / "blanc.yaml"),
                    prefix="blanc-"),
        ProviderDef(name="xeovo", type="file", path=str(prov_dir / "xeovo.yaml"),
                    prefix="xeovo-"),
    ]
    cfg = build_cfg(providers)
    mihomo, up = start_mihomo(cfg)
    procs.append(mihomo)
    if not up:
        log("mihomo did not come up")
        log(mihomo.output()[-2000:])
        return 1
    # file providers load async; wait for the nodes before any check.
    wait_providers_loaded(want={blanc0, blanc1, xeovo0})

    # =========================================================================
    # 1. config validation
    # =========================================================================
    st, ver = mihomo_api("GET", "/version")
    st2, groups = mihomo_api("GET", "/proxies")
    group_names = set(groups.get("proxies", {}).keys()) if isinstance(groups, dict) else set()
    need_groups = {"host-main", "container-main", "os_0", "os_1", "os_2", "os_3"}
    st3, rules = mihomo_api("GET", "/rules")
    rule_list = rules.get("rules", []) if isinstance(rules, dict) else []
    # provider nodes are members of the select groups (not top-level /proxies
    # keys); read them from the host-main group's `all` field.
    _, hm = mihomo_api("GET", "/proxies/host-main")
    node_names = [n for n in (hm.get("all", []) if isinstance(hm, dict) else [])
                  if n.startswith(("blanc-", "xeovo-"))]
    record("config_validation",
           st == 200 and need_groups.issubset(group_names) and len(rule_list) > 0
           and len(node_names) == len(all_nodes),
           f"version={ver.get('version') if isinstance(ver, dict) else '?'}, "
           f"groups={sorted(need_groups & group_names)}, rules={len(rule_list)}, "
           f"provider_nodes={sorted(node_names)}")

    # =========================================================================
    # 2. node collisions
    # Mihomo does NOT refuse to start on duplicate node names (verified: it
    # silently loads them -- a routing hazard). So the guard is the adapter:
    # it must reject a subscription with a duplicate node name, and the live
    # config must be collision-free (provider node count == expected, no dupes).
    # =========================================================================
    dup_yaml = yaml.safe_dump({"proxies": [
        {"name": "dup-node", "type": "socks5", "server": "127.0.0.1", "port": NODE_BASE, "udp": True},
        {"name": "dup-node", "type": "socks5", "server": "127.0.0.1", "port": NODE_BASE + 1, "udp": True},
    ]}, sort_keys=False)
    dup_rejected = False
    try:
        run_adapter("blanc", dup_yaml)
    except ValueError:
        dup_rejected = True
    # live config collision-free: unique provider node names, count == expected
    live_names = node_names  # from check 1 (host-main group members)
    live_unique = len(live_names) == len(set(live_names))
    record("node_collisions",
           dup_rejected and live_unique and len(live_names) == len(all_nodes),
           f"adapter_rejects_dup={dup_rejected}, live_nodes={len(live_names)} "
           f"unique={live_unique} (mihomo silently loads dupes; adapter guards it)")

    # =========================================================================
    # 3. client selector isolation
    # =========================================================================
    mihomo_api("PUT", "/proxies/host-main", {"name": blanc0})
    mihomo_api("PUT", "/proxies/container-main", {"name": xeovo0})
    time.sleep(0.5)
    _, gh = mihomo_api("GET", "/proxies/host-main")
    _, go = mihomo_api("GET", "/proxies/container-main")
    now_h = gh.get("now") if isinstance(gh, dict) else None
    now_o = go.get("now") if isinstance(go, dict) else None
    # prove isolation by data plane: each client's default proxy hits its own node
    sh, bh = socks_fetch("127.0.0.1", DEF_PORT, "host-user", "host-pass",
                         E2E_HOST, E2E_PORT)
    so, bo = socks_fetch("127.0.0.1", DEF_PORT, "os-user", "os-pass",
                         E2E_HOST, E2E_PORT)
    record("client_selector_isolation",
           now_h == blanc0 and now_o == xeovo0
           and sh == "200" and f"NODE:{blanc0_c}" in bh
           and so == "200" and f"NODE:{xeovo0_c}" in bo,
           f"host.now={now_h} (body NODE:{blanc0_c if sh=='200' else '?'}), "
           f"os.now={now_o} (body NODE:{xeovo0_c if so=='200' else '?'})")

    # =========================================================================
    # 4. direct route
    # =========================================================================
    mihomo_api("PUT", "/proxies/host-main", {"name": "DIRECT"})
    time.sleep(0.3)
    sd, bd = socks_fetch("127.0.0.1", DEF_PORT, "host-user", "host-pass",
                         E2E_HOST, E2E_PORT, timeout=5)
    # DIRECT to a non-routable IP -> no canned NODE response (None status).
    record("direct_route",
           sd is None and "NODE:" not in (bd or ""),
           f"DIRECT status={sd} reason={(bd or '')[:60]!r} (no node response expected)")
    # restore host default for later checks
    mihomo_api("PUT", "/proxies/host-main", {"name": blanc0})

    # =========================================================================
    # 5. concurrent one-shot
    # =========================================================================
    # slot os_0 -> blanc0, os_1 -> xeovo0, os_2 -> blanc1
    mihomo_api("PUT", "/proxies/os_0", {"name": blanc0})
    mihomo_api("PUT", "/proxies/os_1", {"name": xeovo0})
    mihomo_api("PUT", "/proxies/os_2", {"name": blanc1})
    time.sleep(0.5)

    def one_shot(i, user, clean_node):
        stt, body = socks_fetch("127.0.0.1", OS_PORT, user, OS_SECRETS[i],
                                E2E_HOST, E2E_PORT)
        return i, stt, body, clean_node

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(one_shot, 0, "os_0", blanc0_c),
                ex.submit(one_shot, 1, "os_1", xeovo0_c),
                ex.submit(one_shot, 2, "os_2", blanc1_c)]
        res = [f.result() for f in futs]
    ok5 = all(r[1] == "200" and f"NODE:{r[3]}" in r[2] for r in res)
    record("concurrent_oneshot",
           ok5,
           " | ".join(f"os_{r[0]}->NODE:{r[3]}:{'ok' if r[1]=='200' and f'NODE:{r[3]}' in r[2] else 'FAIL'}"
                      for r in res))

    # =========================================================================
    # 6. mapping scale (300 nodes -> fixed rule count)
    # =========================================================================
    big_proxies = [{"name": f"big-node-{i:03d}", "type": "socks5",
                    "server": "127.0.0.1", "port": 1000 + i, "udp": True}
                   for i in range(300)]
    big_yaml, _ = run_adapter("blanc", yaml.safe_dump({"proxies": big_proxies}, sort_keys=False))
    (home / "big.yaml").write_text(big_yaml)  # inside mihomo home (subpath rule)
    big_cfg = build_cfg([ProviderDef("big", type="file", path=str(home / "big.yaml"),
                                     prefix="blanc-")])
    big_data = big_cfg.generate()
    n_rules_big = len(big_data["rules"])
    # small config for comparison
    small_data = cfg.generate()
    n_rules_small = len(small_data["rules"])
    # rule count must be identical (independent of node count)
    record("mapping_scale",
           n_rules_big == n_rules_small,
           f"rules with 300 nodes={n_rules_big}, rules with {len(all_nodes)} nodes="
           f"{n_rules_small} (must be equal; IN-USER count is fixed)")

    # =========================================================================
    # 7. GEO update
    # The explicit GEO update writes to a SEPARATE dir (not mihomo's home) so
    # it cannot corrupt the running mihomo's datasets; the GeoManager is the
    # same code path egressd uses. We verify: config has geo-auto-update, the
    # update atomically rewrites the file + metadata + lkg, and mihomo stays up.
    # =========================================================================
    cfg_loaded = yaml.safe_load((home / "config.yaml").read_text())
    geo_auto = cfg_loaded.get("geo-auto-update") is True
    geo_interval = cfg_loaded.get("geo-update-interval")
    mihomo_geo_present = all((home / f).is_file()
                             for f in ("GeoIP.dat", "GeoSite.dat", "asn.mmdb"))
    geo_dir = WORK / "geo_update"
    geo_dir.mkdir(exist_ok=True)
    for f in ("GeoIP.dat", "GeoSite.dat", "asn.mmdb"):
        shutil.copy(GEO_SRC / f, geo_dir / f)
    # fetcher returns the real GeoIP payload with a trailing marker so the hash
    # differs from the on-disk copy (a same-hash update would be "unchanged").
    real_geoip = (GEO_SRC / "GeoIP.dat").read_bytes()
    geo_mgr = GeoManager(GeoConfig(geo_dir=geo_dir,
                                   fetcher=lambda url: real_geoip + b"\x00a2marker",
                                   interval_hours=24))
    res7 = geo_mgr.update(names=("geoip",), force=True, now=time.time())
    geo_meta = (geo_dir / "geo-metadata.json").is_file()
    geo_lkg = (geo_dir / "GeoIP.dat.lkg").is_file()
    # mihomo must still be healthy (its own home datasets were not touched)
    sth, verh = mihomo_api("GET", "/version")
    record("geo_update",
           geo_auto and geo_interval == 24 and mihomo_geo_present
           and res7["geoip"]["updated"] and geo_meta and geo_lkg and sth == 200,
           f"geo-auto-update={geo_auto}, interval={geo_interval}, "
           f"mihomo_geo_files={mihomo_geo_present}, updated={res7['geoip']['updated']}, "
           f"metadata={geo_meta}, lkg={geo_lkg}, mihomo_still_up={sth == 200}")

    # =========================================================================
    # 8. Docker bypass precedence
    # =========================================================================
    # (a) rule order: the Docker CIDR rule must come before the IN-USER rules
    _, rules_resp = mihomo_api("GET", "/rules")
    rules_now = rules_resp.get("rules", []) if isinstance(rules_resp, dict) else []
    docker_idx = next((i for i, r in enumerate(rules_now)
                       if isinstance(r, dict) and "172.17.0.0/16" in str(r.get("payload", ""))), None)
    inuser_idx = next((i for i, r in enumerate(rules_now)
                       if isinstance(r, dict) and r.get("type") == "InUser"), None)
    order_ok = docker_idx is not None and inuser_idx is not None and docker_idx < inuser_idx
    # (b) data plane: host default is a proxy, but a Docker-bridge destination
    # must stay DIRECT (no node response for the non-routable 172.17.0.99).
    mihomo_api("PUT", "/proxies/host-main", {"name": blanc0})
    time.sleep(0.3)
    sdock, bdock = socks_fetch("127.0.0.1", DEF_PORT, "host-user", "host-pass",
                               DOCKER_HOST, 80, timeout=5)
    record("docker_bypass_precedence",
           order_ok and sdock is None and "NODE:" not in (bdock or ""),
           f"docker_cidr_rule_idx={docker_idx} < inuser_idx={inuser_idx}: {order_ok}; "
           f"docker dst via proxy -> status={sdock} (DIRECT, no node response)")

    # ---- results ----
    RESULTS["meta"] = {
        "mihomo": ver.get("version") if isinstance(ver, dict) else "?",
        "blanc_nodes": blanc_clean, "xeovo_nodes": xeovo_clean,
        "rules_small": n_rules_small, "rules_big": n_rules_big,
    }
    (WORK / "results.json").write_text(json.dumps(RESULTS, indent=2))
    n_pass = sum(1 for c in RESULTS["checks"].values() if c["ok"])
    n_tot = len(RESULTS["checks"])
    log(f"\nA2 LIVE: {n_pass}/{n_tot} passed")
    return 0 if n_pass == n_tot else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        # stop every process we started (no pkill: it would match this shell).
        for p in PROCS:
            p.stop()
    sys.exit(rc)
