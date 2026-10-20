#!/usr/bin/env python3
"""PoC orchestrator for vibelab-egress (Phase 0).

Brings up a real Mihomo (pinned v1.19.32) with the production config
generator, fake provider nodes, a local echo server, a VLESS server
(second Mihomo) and a userspace WireGuard server, then runs the spec's
Phase-0 checks and records measured outcomes to results.json.

Run: poc/run_poc.py   (from the repo root)
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
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POC = ROOT / "poc"
sys.path.insert(0, str(ROOT / "egressd" / "src"))

import yaml  # noqa: E402
from egressd.configgen import (  # noqa: E402
    EgressConfig, ProviderDef, ClientDef, OneShotSlot,
)

MIHOMO = POC / "mihomo"
WORK = POC / "run"
RESULTS: dict = {"checks": {}, "meta": {}}

# Ports
CTRL = "127.0.0.1:9090"
DEF_PORT = 18080
OS_PORT = 18081
DNS_PORT = 1053
NODE_BASE = 18001
ECHO_PORT = 18999
VLESS_PORT = 10817
WG_PORT = 51820

SECRET = "poc-mihomo-secret"
CLIENTS = {
    "host-main": ("host-user", "host-pass"),
    "container-main": ("os-user", "os-pass"),
}
OS_SLOTS = 8
OS_SECRETS = [f"os-pass-{i}" for i in range(OS_SLOTS)]
ECHO_TOKEN = "poc-echo-token"
VLESS_UUID = "11111111-2222-3333-4444-555555555555"

# E2e destination. It must be non-LAN so the hard-direct / GEOIP,LAN rules
# cannot intercept the connection (a LAN destination would route DIRECT and
# make the proxy-path checks pass without actually going through a node).
#
# We use a non-routable public IP (TEST-NET-3, 203.0.113.0/24): it is NOT in
# any hard-direct CIDR, so the request is handed to the selected proxy. The
# fake node responds with a canned "NODE:<name>" body, so a 200 response is
# unambiguous proof the connection was routed through that node (and a
# DIRECT route to this IP produces no response, proving it was NOT a proxy).
E2E_HOST = "203.0.113.1"
E2E_PORT = 80


def log(msg: str):
    print(f"[poc] {msg}", flush=True)


def http(method: str, url: str, data=None, headers=None, timeout=10):
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


def mihomo_api(method, path, data=None, secret=SECRET, timeout=15):
    h = {"Authorization": f"Bearer {secret}"}
    return http(method, f"http://{CTRL}{path}", data, h, timeout)


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
    """Wait until the endpoint answers (any HTTP status means it is up)."""
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
            # 401/403/404 still mean the HTTP server is up.
            return True
        except Exception:
            time.sleep(0.3)
    return False


def socks_fetch(host, port, user, password, dst_host, dst_port,
                path="/", timeout=20):
    """Minimal SOCKS5 client (no-auth + user/pass) returning (status, body).

    Returns (None, <reason>) on any socket/transport error (e.g. the DIRECT
    case dialing a non-routable IP), so callers can assert a route did NOT
    produce a response.
    """
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
            u = user.encode(); p = password.encode()
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
        req = (f"GET {path} HTTP/1.1\r\nHost: {dst_host}\r\n"
               f"X-Echo-Token: {ECHO_TOKEN}\r\nConnection: close\r\n\r\n")
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
        # e.g. DIRECT route to a non-routable IP: the connection is accepted
        # by the listener but the data transfer stalls and times out.
        return None, f"transport-error: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


class EchoServer:
    def __init__(self, port):
        self.port = port
        self.proc = None

    def start(self):
        code = f'''
import http.server, socketserver, json
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({{"src_ip": self.client_address[0], "path": self.path,
                            "token_ok": self.headers.get("X-Echo-Token","") == {ECHO_TOKEN!r}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", {self.port}), H) as httpd:
    httpd.serve_forever()
'''
        self.proc = subprocess.Popen([sys.executable, "-c", code],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        return wait_port("127.0.0.1", self.port, 15)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.kill()


class Proc:
    def __init__(self, name, cmd, cwd=None, **kw):
        self.name, self.cmd, self.cwd, self.kw = name, cmd, cwd, kw
        self.proc = None

    def start(self):
        log(f"start {self.name}")
        self.logpath = WORK / f"log_{self.name}.txt"
        self._lf = open(self.logpath, "w")
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd,
                                     stdout=self._lf,
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


def write_provider_nodes():
    # Node names carry the provider prefix so they are unique across
    # providers and match the references used by the checks.
    blanc = {"proxies": []}
    for i, name in enumerate(["blanc-FI-Helsinki-1", "blanc-FI-Helsinki-2",
                              "blanc-NL-Ams-1"]):
        blanc["proxies"].append({"name": name, "type": "socks5",
                                 "server": "127.0.0.1", "port": NODE_BASE + i,
                                 "udp": True})
    (WORK / "providers" / "blanc.yaml").write_text(
        yaml.safe_dump(blanc, sort_keys=False))
    xeovo = {"proxies": []}
    for i, name in enumerate(["xeovo-US-NY-1", "xeovo-DE-FRA-1"]):
        xeovo["proxies"].append({"name": name, "type": "socks5",
                                 "server": "127.0.0.1", "port": NODE_BASE + 3 + i,
                                 "udp": True})
    (WORK / "providers" / "xeovo.yaml").write_text(
        yaml.safe_dump(xeovo, sort_keys=False))


def x25519_keypair():
    from cryptography.hazmat.primitives.asymmetric import x25519
    k = x25519.X25519PrivateKey.generate()
    return (base64.b64encode(k.private_bytes_raw()).decode(),
            base64.b64encode(k.public_key().public_bytes_raw()).decode())


def start_vless_server(procs):
    d = WORK / "vless-srv"
    d.mkdir(exist_ok=True)
    crt, key = d / "server.crt", d / "server.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-keyout", str(key), "-out", str(crt), "-days", "1",
                    "-nodes", "-subj", "/CN=127.0.0.1"],
                   check=True, capture_output=True)
    srv_cfg = {
        "mixed-port": 0, "allow-lan": False, "mode": "rule",
        "log-level": "info", "external-controller": "127.0.0.1:9091",
        "secret": "vless-srv-secret",
        "listeners": [{
            "name": "vless-in", "type": "vless", "port": VLESS_PORT,
            "listen": "127.0.0.1",
            "users": [{"username": "u1", "uuid": VLESS_UUID}],
            "certificate": str(crt), "private-key": str(key),
        }],
        # Route all VLESS traffic to a dedicated fake node (vless-upstream on
        # NODE_BASE+5) so the e2e path is: main mihomo -> VLESS tunnel ->
        # vless-upstream fake node -> canned "NODE:vless-upstream" response.
        "proxies": [{
            "name": "vless-upstream", "type": "socks5",
            "server": "127.0.0.1", "port": NODE_BASE + 5, "udp": True,
        }],
        "proxy-groups": [
            {"name": "vless-out", "type": "select",
             "proxies": ["vless-upstream", "DIRECT"]},
        ],
        "rules": ["MATCH,vless-out"],
    }
    (d / "config.yaml").write_text(yaml.safe_dump(srv_cfg, sort_keys=False))
    p = Proc("vless-srv", [str(MIHOMO), "-d", str(d), "-f", str(d / "config.yaml")],
             cwd=str(d)).start()
    procs.append(p)
    return wait_http("http://127.0.0.1:9091/version", 20, secret="vless-srv-secret")


def start_wg_server(procs, client_pub):
    wgbin = POC / "wgserver" / "wgserver"
    p = Proc("wgserver", [str(wgbin), str(WG_PORT), client_pub, str(ECHO_PORT)],
             cwd=str(POC)).start()
    procs.append(p)
    # Poll for the ready marker: gVisor stack + wireguard-go init can take
    # several seconds under load, so a fixed sleep is a race.
    deadline = time.time() + 20
    while time.time() < deadline:
        out = p.output()
        if "WG_SERVER_OK" in out:
            return out.split("public_key=")[1].split()[0]
        if p.proc.poll() is not None:
            break  # exited before ready
        time.sleep(0.3)
    return None


def build_config(static_proxies) -> EgressConfig:
    # No name prefix in the PoC: node names stay clean so the test can
    # reference them directly. (Real deployments may add prefixes; the
    # catalog maps stable route IDs to mihomo names.)
    providers = [
        ProviderDef(name="blanc", type="file",
                    path=str(WORK / "providers" / "blanc.yaml")),
        ProviderDef(name="xeovo", type="file",
                    path=str(WORK / "providers" / "xeovo.yaml")),
    ]
    clients = [ClientDef(client_id=cid, proxy_user=u, proxy_password=p)
               for cid, (u, p) in CLIENTS.items()]
    slots = [OneShotSlot(i, f"os_{i}", OS_SECRETS[i]) for i in range(OS_SLOTS)]
    return EgressConfig(
        mihomo_secret=SECRET, clients=clients, providers=providers,
        oneshot_slots=slots, default_port=DEF_PORT, oneshot_port=OS_PORT,
        controller=CTRL, dns_listen=f"127.0.0.1:{DNS_PORT}",
        hard_direct_cidrs=["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                           "169.254.0.0/16"],
        static_proxies=static_proxies,
    )


def record(name, ok, detail):
    RESULTS["checks"][name] = {"ok": bool(ok), "detail": detail}
    log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if not ok else ""))


def read_node_log(node_log):
    """Return the set of node names that logged a 'selected' event."""
    if not node_log.exists():
        return set()
    names = set()
    for line in node_log.read_text().strip().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("phase") == "selected":
            names.add(e.get("node"))
    return names


def main():
    if not MIHOMO.exists():
        log(f"mihomo binary not found at {MIHOMO}")
        sys.exit(1)
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / "providers").mkdir(parents=True)
    # Mihomo auto-detects GEO files by name in its home dir (WORK).
    shutil.copy(POC / "geo" / "GeoIP.dat", WORK / "GeoIP.dat")
    shutil.copy(POC / "geo" / "GeoSite.dat", WORK / "GeoSite.dat")
    shutil.copy(POC / "geo" / "asn.mmdb", WORK / "ASN.mmdb")
    write_provider_nodes()

    procs = []
    echo = EchoServer(ECHO_PORT)
    if not echo.start():
        log("echo server failed to start")
        sys.exit(1)

    # Upstream servers first (VLESS + WireGuard) so the main mihomo can use
    # them as real nodes.
    vless_ok = start_vless_server(procs)
    wg_client_priv, wg_client_pub = x25519_keypair()
    wg_server_pub = start_wg_server(procs, wg_client_pub)

    static_proxies = []
    if vless_ok:
        static_proxies.append({
            "name": "vless-node", "type": "vless", "server": "127.0.0.1",
            "port": VLESS_PORT, "uuid": VLESS_UUID, "udp": True,
            "tls": True, "skip-cert-verify": True,
        })
    if wg_server_pub:
        static_proxies.append({
            "name": "wg-node", "type": "wireguard", "server": "127.0.0.1",
            "port": WG_PORT, "ip": "10.66.0.2", "private-key": wg_client_priv,
            "public-key": wg_server_pub, "allowed-ips": ["0.0.0.0/0"],
            "udp": True, "mtu": 1420,
        })

    cfg = build_config(static_proxies)
    generated = cfg.generate()
    (WORK / "mihomo.yaml").write_text(yaml.safe_dump(generated, sort_keys=False))
    log(f"generated config: {len(generated['rules'])} rules, "
        f"{len(generated['proxy-groups'])} groups, "
        f"{len(generated['proxies'])} static proxies")

    # Fake provider nodes (5 total: 3 blanc + 2 xeovo)
    node_log = WORK / "nodes.jsonl"
    # vless-upstream (NODE_BASE+5) is the dedicated upstream for the VLESS
    # server; the rest are the fake provider nodes.
    nodes = ["blanc-FI-Helsinki-1", "blanc-FI-Helsinki-2", "blanc-NL-Ams-1",
             "xeovo-US-NY-1", "xeovo-DE-FRA-1", "vless-upstream"]
    node_proc = Proc("fakenodes", [sys.executable, str(POC / "fake_node.py"),
                                   "--base-port", str(NODE_BASE),
                                   "--nodes", ",".join(nodes),
                                   "--log", str(node_log)]).start()
    procs.append(node_proc)
    wait_port("127.0.0.1", NODE_BASE, 15)

    mihomo = Proc("mihomo", [str(MIHOMO), "-d", str(WORK),
                             "-f", str(WORK / "mihomo.yaml")],
                  cwd=str(WORK)).start()
    procs.append(mihomo)
    if not wait_http(f"http://{CTRL}/version", 30, secret=SECRET):
        log("mihomo controller did not come up")
        log(mihomo.output()[-3000:])
        sys.exit(1)
    st, ver = mihomo_api("GET", "/version")
    RESULTS["meta"]["mihomo_version"] = ver.get("version") if isinstance(ver, dict) else ver
    log(f"mihomo up: {ver}")

    try:
        run_checks(cfg, node_log, vless_ok, wg_server_pub)
    finally:
        for p in procs:
            p.stop()
        echo.stop()
        (WORK / "mihomo.log").write_text(mihomo.output())

    (POC / "results.json").write_text(json.dumps(RESULTS, indent=2))
    passed = sum(1 for c in RESULTS["checks"].values() if c["ok"])
    total = len(RESULTS["checks"])
    log(f"\n=== {passed}/{total} checks passed ===")
    for name, c in RESULTS["checks"].items():
        log(f"  {'PASS' if c['ok'] else 'FAIL'}  {name}")


def run_checks(cfg, node_log, vless_ok, wg_server_pub):
    st, ver = mihomo_api("GET", "/version")
    record("P01_controller_version", st == 200 and ver.get("meta") is True,
           f"status={st} ver={ver}")
    st, _ = http("GET", f"http://{CTRL}/version")
    record("P02_controller_requires_auth", st in (401, 403), f"no-auth status={st}")

    st, prox = mihomo_api("GET", "/proxies")
    groups = prox.get("proxies", {}) if isinstance(prox, dict) else {}
    for cid in CLIENTS:
        g = groups.get(cid)
        ok = g and g.get("type") == "Selector" and "DIRECT" in g.get("all", [])
        record(f"P03_selector_{cid}", ok,
               f"type={g.get('type') if g else None} all={g.get('all') if g else None}")

    os_groups = [g for g in groups if g.startswith("os_")]
    record("P04_oneshot_slot_groups", len(os_groups) == OS_SLOTS,
           f"found {len(os_groups)} slot groups")

    host_node = "blanc-FI-Helsinki-1"
    st, _ = mihomo_api("PUT", f"/proxies/host-main", {"name": host_node})
    st2, g = mihomo_api("GET", "/proxies/host-main")
    record("P05_set_default_host", st == 204 and g.get("now") == host_node,
           f"set={st} now={g.get('now')}")

    os_node = "xeovo-US-NY-1"
    st, _ = mihomo_api("PUT", "/proxies/container-main", {"name": os_node})
    st2, g = mihomo_api("GET", "/proxies/container-main")
    st3, gh = mihomo_api("GET", "/proxies/host-main")
    record("P06_client_isolation",
           g.get("now") == os_node and gh.get("now") == host_node,
           f"os.now={g.get('now')} host.now={gh.get('now')}")

    # ---- Default proxy data plane (non-LAN destination) ----
    # E2E_HOST (203.0.113.1, TEST-NET-3) is not in any hard-direct CIDR, so
    # the request is handed to the selected proxy. The fake node answers with
    # a canned "NODE:<name>" body, so a 200 + matching NODE line is proof the
    # connection was routed through that node (a DIRECT route to this IP
    # produces no response).
    mihomo_api("PUT", "/proxies/host-main", {"name": host_node})
    st, body = socks_fetch("127.0.0.1", DEF_PORT, "host-user", "host-pass",
                           E2E_HOST, E2E_PORT)
    record("P07_default_proxy_host",
           st == "200" and f"NODE:{host_node}" in body,
           f"status={st} body={body[:120]!r}")

    mihomo_api("PUT", "/proxies/container-main", {"name": os_node})
    st, body = socks_fetch("127.0.0.1", DEF_PORT, "os-user", "os-pass",
                           E2E_HOST, E2E_PORT)
    record("P08_default_proxy_container",
           st == "200" and f"NODE:{os_node}" in body,
           f"status={st} body={body[:120]!r}")

    st, _ = mihomo_api("PUT", "/proxies/os_0", {"name": host_node})
    st, body = socks_fetch("127.0.0.1", OS_PORT, "os_0", OS_SECRETS[0],
                           E2E_HOST, E2E_PORT)
    record("P09_oneshot_route",
           st == "200" and f"NODE:{host_node}" in body,
           f"status={st} body={body[:120]!r}")

    st, gh = mihomo_api("GET", "/proxies/host-main")
    st, go = mihomo_api("GET", "/proxies/container-main")
    record("P10_oneshot_no_default_change",
           gh.get("now") == host_node and go.get("now") == os_node,
           f"host={gh.get('now')} os={go.get('now')}")

    # Three concurrent one-shots through distinct routes (incl. DIRECT).
    mihomo_api("PUT", "/proxies/os_0", {"name": "blanc-FI-Helsinki-1"})
    mihomo_api("PUT", "/proxies/os_1", {"name": "xeovo-DE-FRA-1"})
    mihomo_api("PUT", "/proxies/os_2", {"name": "DIRECT"})
    node_log.write_text("")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [
            ex.submit(socks_fetch, "127.0.0.1", OS_PORT, "os_0", OS_SECRETS[0],
                      E2E_HOST, E2E_PORT),
            ex.submit(socks_fetch, "127.0.0.1", OS_PORT, "os_1", OS_SECRETS[1],
                      E2E_HOST, E2E_PORT),
            ex.submit(socks_fetch, "127.0.0.1", OS_PORT, "os_2", OS_SECRETS[2],
                      E2E_HOST, E2E_PORT),
        ]
        res = [f.result() for f in futs]
    logged_nodes = read_node_log(node_log)
    # The two proxy one-shots must hit their distinct nodes; the DIRECT one
    # must NOT produce a response (non-routable IP) and must NOT touch a node.
    p0_ok = res[0][0] == "200" and "NODE:blanc-FI-Helsinki-1" in res[0][1]
    p1_ok = res[1][0] == "200" and "NODE:xeovo-DE-FRA-1" in res[1][1]
    p2_no_resp = res[2][0] != "200"
    record("P11_concurrent_oneshot",
           p0_ok and p1_ok and p2_no_resp
           and "blanc-FI-Helsinki-1" in logged_nodes
           and "xeovo-DE-FRA-1" in logged_nodes,
           f"statuses={[r[0] for r in res]} "
           f"direct_body={res[2][1][:60]!r} nodes={sorted(logged_nodes)}")

    rules = cfg.generate()["rules"]
    idx_hd = next(i for i, r in enumerate(rules) if r.startswith("IP-CIDR"))
    idx_user = next(i for i, r in enumerate(rules) if r.startswith("IN-USER"))
    record("P12_harddirect_precedence", idx_hd < idx_user,
           f"hard-direct@{idx_hd} before IN-USER@{idx_user}")

    st, _ = mihomo_api("PUT", "/providers/proxies/blanc")
    record("P13_provider_update_api", st == 204, f"status={st}")

    # Provider nodes are not top-level proxies (direct /delay on them is
    # 404), so node latency is measured through a probe slot pointed at the
    # node — the same pattern egressd will use for health/latency.
    mihomo_api("PUT", "/proxies/os_3", {"name": "blanc-FI-Helsinki-1"})
    # The fake node answers 200 to any destination, so the non-LAN host is a
    # valid delay-test target: mihomo fetches it through the node and the
    # round-trip is measured.
    st, d = mihomo_api("GET",
                       f"/proxies/os_3/delay?url=http://{E2E_HOST}/&timeout=5000")
    record("P14_node_delay_api", st == 200 and isinstance(d, dict)
           and isinstance(d.get("delay"), int),
           f"status={st} d={d}")

    mihomo_api("PUT", "/providers/proxies/blanc")
    st, gh = mihomo_api("GET", "/proxies/host-main")
    record("P15_update_preserves_selection", gh.get("now") == host_node,
           f"after update host.now={gh.get('now')}")

    st, rules = mihomo_api("GET", "/rules")
    rule_list = rules.get("rules", []) if isinstance(rules, dict) else []
    # Mihomo reports the type as "GeoIP" (mixed case); match case-insensitively.
    geoip_rules = [r for r in rule_list
                   if str(r.get("type", "")).upper() == "GEOIP"]
    record("P16_geoip_rules_loaded", len(geoip_rules) >= 1,
           f"geoip rules={len(geoip_rules)} total={len(rule_list)}")

    st, q = mihomo_api("GET", "/dns/query?name=example.com&type=A")
    record("P17_dns_query", st == 200 and isinstance(q, dict), f"status={st}")

    big = EgressConfig(
        mihomo_secret=SECRET,
        clients=[ClientDef("c1", "u1", "p1")],
        providers=[ProviderDef(name="big", type="file",
                               path=str(WORK / "providers" / "big.yaml"))],
        oneshot_slots=[OneShotSlot(i, f"os_{i}", f"p{i}") for i in range(32)],
        default_port=DEF_PORT, oneshot_port=OS_PORT, controller=CTRL,
        dns_listen=f"127.0.0.1:{DNS_PORT}",
    )
    big_prov = {"proxies": [{"name": f"n{i}", "type": "socks5",
                             "server": "127.0.0.1", "port": 18001}
                            for i in range(300)]}
    (WORK / "providers" / "big.yaml").write_text(
        yaml.safe_dump(big_prov, sort_keys=False))
    bigcfg = big.generate()
    n_rules = len(bigcfg["rules"])
    # Rule count must be independent of node count: same clients/slots,
    # 300 nodes -> identical rule count to a 1-node config.
    small = EgressConfig(
        mihomo_secret=SECRET, clients=[ClientDef("c1", "u1", "p1")],
        providers=[ProviderDef(name="small", type="file",
                               path=str(WORK / "providers" / "small.yaml"))],
        oneshot_slots=[OneShotSlot(i, f"os_{i}", f"p{i}") for i in range(32)],
        default_port=DEF_PORT, oneshot_port=OS_PORT, controller=CTRL,
        dns_listen=f"127.0.0.1:{DNS_PORT}",
    )
    (WORK / "providers" / "small.yaml").write_text(
        yaml.safe_dump({"proxies": [{"name": "s1", "type": "socks5",
                                     "server": "127.0.0.1", "port": 18001}]},
                       sort_keys=False))
    n_small = len(small.generate()["rules"])
    expected = 1 + 4 + 1 + 32 + 1  # GEOIP + CIDRs + client + slots + MATCH
    record("P18_mapping_scale", n_rules == n_small == expected,
           f"300 nodes -> {n_rules} rules, 1 node -> {n_small}, expected {expected} "
           f"(independent of node count)")

    st, _ = http("GET", f"http://{CTRL}/proxies",
                 headers={"Authorization": "Bearer host-pass"})
    record("P19_client_token_no_mihomo_api", st in (401, 403),
           f"client-token on mihomo api status={st}")

    # ---- VLESS data-plane e2e ----
    # Path: client -> main mihomo (os_4 -> vless-node) -> VLESS tunnel ->
    # vless-upstream fake node -> canned "NODE:vless-upstream" response. The
    # non-LAN destination ensures the request is not intercepted by
    # GEOIP,LAN and is genuinely carried over the VLESS transport.
    if vless_ok:
        mihomo_api("PUT", "/proxies/os_4", {"name": "vless-node"})
        st, body = socks_fetch("127.0.0.1", OS_PORT, "os_4", OS_SECRETS[4],
                               E2E_HOST, E2E_PORT)
        record("P20_vless_e2e", st == "200" and "NODE:vless-upstream" in body,
               f"status={st} body={body[:160]!r}")
    else:
        record("P20_vless_e2e", False, "vless server did not start")

    # ---- WireGuard data-plane e2e ----
    # Path: client -> main mihomo (os_5 -> wg-node) -> WG tunnel -> wgserver
    # -> local echo server -> JSON response (token_ok) -> WG tunnel -> client.
    # The wgserver forwarder dials 127.0.0.1:<requested-port>, so the client
    # requests the echo port on a non-LAN IP: the connection is routed through
    # the WG tunnel (not intercepted by GEOIP,LAN) and the response travels
    # back through the tunnel.
    if wg_server_pub:
        mihomo_api("PUT", "/proxies/os_5", {"name": "wg-node"})
        st, body = socks_fetch("127.0.0.1", OS_PORT, "os_5", OS_SECRETS[5],
                               E2E_HOST, ECHO_PORT)
        record("P21_wireguard_e2e", st == "200" and '"token_ok": true' in body,
               f"status={st} body={body[:160]!r}")
    else:
        record("P21_wireguard_e2e", False, "wgserver did not start")


if __name__ == "__main__":
    main()
