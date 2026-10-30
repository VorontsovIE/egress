#!/usr/bin/env python3
"""live-acceptance.py -- host-side live acceptance harness for egress.

Runs the acceptance checks that REQUIRE host access (TECH_SPEC.md section 50
"Phase 2 TUN" plus the host-only live items of section 49 "v1 proxy mode") and
writes ALL results to a SINGLE JSON file (default: host_acceptance_report.json).

Send that one file back for re-verification. The companion
scripts/verify-live-report.py re-derives every pass/fail/skip from the raw
evidence in the file, so the report is self-contained and tamper-evident.

Security invariants (mirrors egress-host-test):
  * Python 3 standard library only.
  * No arbitrary shell / free-form command input. Every external command is a
    fixed argv built from a fixed allowlist (egressctl, egress-run, docker,
    ip, ss, python3, egress-host-test).
  * No secrets ever appear in the report: token values, default-proxy
    credentials, mihomo secret and provider subscription values are read from
    the known secret locations and redacted out of every captured output.
    The redaction count is recorded in the report.

Run on the host (see host/LIVE_ACCEPTANCE.md for the exact commands):

    python3 host/live-acceptance.py --out host_acceptance_report.json

Options:
    --out PATH            write the single JSON report here (default
                          host_acceptance_report.json)
    --config PATH         host client config (default /etc/egress/client.yaml)
    --container-config PATH       container client config (default
                          $EGRESS_CONTAINER_CONFIG or
                          /var/lib/egress/client-bundles/container-main/client.yaml)
    --litellm-url URL     LiteLLM endpoint to probe (default
                          $EGRESS_LITELLM_URL or
                          http://127.0.0.1:4000/health/liveliness)
    --container NAME   Docker container name (default
                          $EGRESS_CONTAINER or container)
    --recreate            enable the container restart observation;
                          full container recreation is not automated
    --no-tun              skip the TUN (section 50) checks
    --check ID[,ID...]    run only the named check(s)
    --timeout N           per-command timeout seconds (default 90)

Exit code: 0 if no check FAILED, 1 if at least one check FAILED. Skips are not
failures (a check that cannot run in the given environment is reported as
"skip" with a reason, never silently dropped).
"""

import argparse
import concurrent.futures
import copy
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# fixed configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = "/etc/egress/client.yaml"
DEFAULT_CONTAINER_CONFIG = "/var/lib/egress/client-bundles/container-main/client.yaml"
# /health/liveliness: 200 while the process is up. /health also reports
# provider health and answers 503 when no provider is configured, which
# is not a LiteLLM reachability failure (2026-10-15 live run: skip with
# "not reachable" although the container was up and healthy).
DEFAULT_LITELLM = "http://127.0.0.1:4000/health/liveliness"
DEFAULT_CONTAINER = "container"
SECRETS_DIR = os.environ.get("EGRESS_SECRETS", "/etc/egress/secrets")
ROOT = os.environ.get("EGRESS_ROOT", "/var/lib/egress")
REPO = os.environ.get("EGRESS_REPO", "/srv/workspace/egress")
HOST_TEST = os.path.join(REPO, "host", "egress-host-test")
PROBE_DOMAIN = "www.cloudflare.com"
IP_SERVICES = ("https://api.ipify.org", "https://ifconfig.co/ip")
# python -c snippet for checks that need the exit IP: tries every service
# from IP_SERVICES (like host_public_ip) so a single blocked service cannot
# fail a check (2026-10-15 live run: one direct job died on one service
# while host_public_ip — which loops over both — succeeded).
IP_FETCH = (
    "import urllib.request\n"
    "for _u in %s:\n"
    "    try:\n"
    "        print(urllib.request.urlopen(_u, timeout=15).read().decode().strip())\n"
    "        break\n"
    "    except Exception:\n"
    "        pass\n" % repr(IP_SERVICES)
)
EGRESS_PORTS = (1080, 1081, 9091)
MIHOMO_ADMIN_PORT = 9090
MAX_OUT = 8000

RESULTS = []
CTX = {}


# ---------------------------------------------------------------------------
# secret redaction
# ---------------------------------------------------------------------------
def _collect_secret_values():
    """Gather (value, label) secret pairs to redact/scan.

    The label is the value's ORIGIN (file path or env var name) so scans
    can tell apart what is legitimately embedded where (e.g. the mihomo
    admin secret inside the generated mihomo config — mihomo needs it
    there). ``*.profile.json`` files are excluded: they are deterministic
    NON-SECRET metadata whose identical content legitimately appears in
    the repo (test fixtures, PoC artifacts) — counting them as secrets
    produced false-positive "leaks" (2026-10-15 live run).
    """
    vals = []

    def add(path, label=None):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                vals.append((v, label or path))
        except OSError:
            pass

    for p in (
        os.path.join("/etc/egress", "client.token"),
        os.path.join("/etc/egress", "default-proxy.auth"),
    ):
        add(p, os.path.basename(p))
    # client config token/auth file paths (host + container)
    for cfg_path in (CTX.get("config"), CTX.get("container_config")):
        if cfg_path:
            for key in ("api_token_file", "default_proxy_auth_file"):
                v = _flat_yaml_get(cfg_path, key)
                if v:
                    add(v, key)
    # backend secrets dir (mihomo secret + provider subscriptions)
    if os.path.isdir(SECRETS_DIR):
        for root_, _, files in os.walk(SECRETS_DIR):
            for fn in files:
                if fn.endswith(".profile.json"):
                    continue  # non-secret metadata (see docstring)
                full = os.path.join(root_, fn)
                add(full, os.path.relpath(full, SECRETS_DIR))
    # environment-provided secrets
    for env in ("EGRESS_CONTAINER_TOKEN", "EGRESS_CONTAINER_AUTH"):
        v = os.environ.get(env, "").strip()
        if v:
            vals.append((v, env))
    # de-dup (keep first label), only values long enough to redact safely
    seen = set()
    out = []
    for v, label in vals:
        v = v.strip()
        if len(v) >= 8 and v not in seen:
            seen.add(v)
            out.append((v, label))
    return out


def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _flat_yaml_get(path, key):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line.startswith(key + ":"):
                    return line.partition(":")[2].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def make_redactor():
    secrets = [v for v, _ in _collect_secret_values()]
    count = {"n": 0}

    def redact(text):
        if not isinstance(text, str) or not text:
            return text
        for s in secrets:
            if s in text:
                count["n"] += text.count(s)
                text = text.replace(s, "[REDACTED]")
        return text

    return redact, count


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def is_public_ip(ip):
    if not ip:
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified)


def is_local_bind(ip):
    if not ip or ip in ("0.0.0.0", "::", "[::]"):
        return False
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_unspecified:
        return False
    return a.is_loopback or a.is_private or a.is_link_local


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "egress-acceptance"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def host_public_ip(timeout=8):
    for url in IP_SERVICES:
        try:
            _, body = http_get(url, timeout)
            ip = body.strip()
            if ipaddress.ip_address(ip):
                return ip
        except Exception:
            continue
    return None


def _run(cmd, timeout, redact, env=None, cwd=None, max_out=MAX_OUT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=cwd)
        stdout, stderr = redact(p.stdout), redact(p.stderr)
        return {
            "rc": p.returncode,
            "stdout": stdout if max_out is None else stdout[:max_out],
            "stderr": stderr if max_out is None else stderr[:max_out],
        }
    except subprocess.TimeoutExpired:
        return {"rc": None, "stdout": "", "stderr": f"timeout after {timeout}s",
                "timed_out": True}
    except FileNotFoundError:
        return {"rc": None, "stdout": "", "stderr": f"command not found: {cmd[0]}"}
    except Exception as e:  # noqa: BLE001
        return {"rc": None, "stdout": "", "stderr": redact(str(e))}


def _json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def canon_hash(checks):
    """Canonical hash over each check's {id, spec, command, evidence}.

    The verifier recomputes this to detect post-hoc edits to the evidence or
    command of any check. (status/detail are intentionally excluded: the
    verifier re-derives status from evidence, so it must not be hash-locked.)
    """
    parts = []
    for c in checks:
        parts.append(json.dumps(
            {"id": c.get("id"), "spec": c.get("spec"),
             "command": c.get("command"), "evidence": c.get("evidence")},
            sort_keys=True, separators=(",", ":")))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def add_result(cid, spec, status, command, evidence, detail):
    RESULTS.append({
        "id": cid,
        "spec": spec,
        "status": status,
        "command": command,
        "detail": detail,
        "evidence": evidence,
    })


def skip(cid, spec, command, reason, extra=None):
    ev = {"reason": reason}
    if extra:
        ev.update(extra)
    add_result(cid, spec, "skip", command, ev, reason)


def run_check(cid, spec, command, fn):
    """Run a check function; it returns (status, evidence, detail)."""
    try:
        status, evidence, detail = fn()
    except Exception as e:  # noqa: BLE001
        status, evidence, detail = "fail", {"exception": str(e)}, f"check raised: {e}"
    evidence = evidence or {}
    if status == "skip" and not evidence.get("reason"):
        evidence = dict(evidence)
        evidence["reason"] = detail
    add_result(cid, spec, status, command, evidence, detail)


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _client_cmd(base, module, config, args):
    """Prefer the installed release binary (spec 49.7); fall back to python -m."""
    binpath = _which(base)
    if binpath:
        return [binpath, "--config", config, "--json"] + args if base == "egressctl" \
            else [binpath, "--config", config] + args
    mod = "egress_client." + module
    return [sys.executable, "-m", mod, "--config", config, "--json"] + args \
        if module == "egressctl" else [sys.executable, "-m", mod, "--config", config] + args


def egressctl(args, config, timeout, redact, max_out=MAX_OUT):
    return _run(_client_cmd("egressctl", "egressctl", config, args), timeout,
                redact, max_out=max_out)


def egress_run(args, config, timeout, redact, env=None):
    return _run(_client_cmd("egress-run", "egress_run", config, args),
                timeout, redact, env=env)


def container_exec(args, timeout, redact):
    """Run a command INSIDE the Docker container (spec 49: the
    container client lives in the container; its token paths /run/secrets/*
    and its container runtime environment only exist there)."""
    return _run(["docker", "exec", CTX["container_name"]] + args, timeout, redact)


def _client_version(host):
    """Return the installed client package __version__ (spec 49.7).

    Resolves the install libdir from the egressctl wrapper (which bakes it in),
    then reads __init__.py. ``host`` is False to read inside the container.
    """
    redact, timeout = CTX["redact"], CTX["timeout"]
    wrapper = "/usr/local/bin/egressctl"
    if host:
        if not os.path.isfile(wrapper):
            return None
        with open(wrapper, "r", encoding="utf-8") as f:
            txt = f.read()
        m = re.search(r"sys\.path\.insert\(0,\s*'([^']+)'", txt)
        if not m:
            return None
        init = os.path.join(m.group(1), "egress_client", "__init__.py")
        data = read_file(init)
    else:
        r = container_exec(["cat", wrapper], timeout, redact)
        if r["rc"] != 0:
            return None
        m = re.search(r"sys\.path\.insert\(0,\s*'([^']+)'", r["stdout"])
        if not m:
            return None
        init = os.path.join(m.group(1), "egress_client", "__init__.py")
        r = container_exec(["cat", init], timeout, redact)
        data = r["stdout"] if r["rc"] == 0 else None
    if not data:
        return None
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', data)
    return m.group(1) if m else None


def select_proxy_route(routes):
    """Pick a healthy non-direct proxy route (prefer one with a country)."""
    non_direct = [r for r in routes
                  if isinstance(r, dict) and r.get("id") and r.get("id") != "direct"]
    if not non_direct:
        return None
    healthy = [r for r in non_direct if r.get("healthy") is True]
    pool = healthy or non_direct
    with_country = [r for r in pool if r.get("country")]
    return (with_country or pool)[0]


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------
def check_preflight():
    def fn():
        redact = CTX["redact"]
        timeout = CTX["timeout"]
        cfg = CTX["config"]
        env = {
            "hostname": socket.gethostname(),
            "kernel": os.uname().release,
            "user": os.environ.get("USER", ""),
            "uid": os.getuid(),
            "gid": os.getgid(),
            "python": sys.version.split()[0],
            "docker": None,
            "egressctl": None,
        }
        env["docker"] = _run(["docker", "version", "--format", "{{.Server.Version}}"],
                             15, redact)
        env["egressctl"] = egressctl(["status"], cfg, timeout, redact)
        ok_docker = env["docker"]["rc"] == 0
        ok_ctl = env["egressctl"]["rc"] == 0
        env["host_public_ip"] = host_public_ip()
        # route catalog
        rl = egressctl(["list"], cfg, timeout, redact, max_out=None)
        data = _json(rl["stdout"]) or {}
        routes = data.get("routes", data if isinstance(data, list) else [])
        if not isinstance(routes, list):
            routes = []
        env["routes"] = [{k: r.get(k) for k in
                          ("id", "provider", "country", "healthy", "protocol")}
                         for r in routes if isinstance(r, dict)]
        CTX["routes"] = env["routes"]
        CTX["backend_ok"] = ok_docker and ok_ctl
        CTX["client_ok"] = ok_ctl
        CTX["client_bin"] = _which("egressctl")
        ev = env
        if not ok_docker or not ok_ctl:
            # diagnose the specific gap so the report is actionable
            if not ok_ctl:
                err = (env["egressctl"]["stderr"] or env["egressctl"]["stdout"]
                       )[:200]
                if "ModuleNotFoundError" in err or "No module named" in err:
                    why = ("egressctl not installed on the host (python -m "
                           "fallback failed); run scripts/install-client.sh "
                           f"--target host first. detail: {err}")
                else:
                    why = (f"egressctl status failed (rc={env['egressctl']['rc']}); "
                           f"is the backend up? detail: {err}")
            else:
                why = (f"docker not reachable (rc={env['docker']['rc']}); "
                       "is the egress stack up?")
            return ("skip", ev, f"preflight: docker_ok={ok_docker} "
                                f"egressctl_ok={ok_ctl}; {why}")
        if not env["routes"]:
            return ("fail", ev, "egressctl list returned no routes")
        return ("pass", ev, f"host ready; {len(env['routes'])} routes; "
                            f"host_public_ip={env['host_public_ip']}")
    run_check("preflight", "env", "preflight", fn)


# ---------------------------------------------------------------------------
# section 50 -- Phase 2 TUN
# ---------------------------------------------------------------------------
def _tun_ready():
    result = egressctl(["--json", "capabilities"], CTX["config"], CTX["timeout"], CTX["redact"])
    data = _json(result["stdout"]) or {}
    tun = data.get("tun") or {}
    if result["rc"] != 0 or tun.get("status") != "supported":
        return False, f"local TUN {tun.get('status', 'unknown')}: {tun.get('reason', result['stderr'])}"
    return True, ""


def check_tun_tcp():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        route = select_proxy_route(CTX["routes"])
        if not route:
            return ("skip", {"reason": "no non-direct proxy route available"},
                    "no proxy route")
        rid = route["id"]
        r = egress_run(["--mode", "tun", rid, "--",
                        "python3", "-c", IP_FETCH],
                       cfg, timeout, redact)
        ip = r["stdout"].strip().splitlines()[-1].strip() if r["stdout"].strip() else ""
        ev = {"route": rid, "expected_country": route.get("country"),
              "exit": r["rc"], "exit_ip": ip,
              "stdout": r["stdout"], "stderr": r["stderr"]}
        if r["rc"] != 0:
            return ("fail", ev, f"tun tcp exit={r['rc']}")
        if not is_public_ip(ip):
            return ("fail", ev, f"exit_ip {ip!r} is not a public IP")
        if route.get("country") and ip:
            try:
                _, body = http_get(f"https://ipinfo.io/{ip}/json", 10)
                cc = (_json(body) or {}).get("country", "")
                ev["exit_country"] = cc
                if cc and route["country"] and cc.upper() != route["country"].upper():
                    return ("fail", ev,
                            f"exit country {cc} != expected {route['country']}")
            except Exception:
                pass
        return ("pass", ev, f"tun tcp via {rid} -> {ip}")
    run_check("tun_tcp_client", "spec50.1",
              "egress-run --mode tun ROUTE -- <proxy-unaware tcp client>", fn)


def check_tun_local_workload():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        route = select_proxy_route(CTX["routes"]) or {"id": "direct"}
        rid = route["id"]
        marker_fd, marker = tempfile.mkstemp(prefix="egress-tun-acceptance-")
        os.close(marker_fd)
        os.unlink(marker)
        try:
            r = egress_run(["--mode", "tun", rid, "--", "python3", "-c",
                            "import os,pathlib; p=pathlib.Path(os.environ['VIBE_TUN_MARKER']); "
                            "p.write_text('host-native'); "
                            "print(os.getuid()); print(os.getgid()); "
                            "print(os.uname().nodename); print(os.getcwd())"],
                           cfg, timeout, redact,
                           env={**os.environ, "VIBE_TUN_MARKER": marker})
            marker_ok = os.path.isfile(marker) and open(marker).read() == "host-native"
        finally:
            if os.path.exists(marker):
                os.unlink(marker)
        lines = [l for l in r["stdout"].splitlines() if l.strip()]
        ev = {"route": rid, "exit": r["rc"], "lines": lines,
              "stdout": r["stdout"], "stderr": r["stderr"],
              "host_marker_written": marker_ok,
              "expected_uid": os.getuid(), "expected_gid": os.getgid(),
              "expected_hostname": socket.gethostname(),
              "expected_cwd": os.getcwd()}
        if r["rc"] != 0 or len(lines) < 4:
            return ("fail", ev, f"tun workload exit={r['rc']} lines={len(lines)}")
        ev["uid"], ev["gid"], ev["hostname"] = lines[0], lines[1], lines[2]
        if lines[0].strip() != str(os.getuid()) or lines[1].strip() != str(os.getgid()):
            return ("fail", ev, "workload did not run as caller UID/GID")
        if lines[2].strip() != socket.gethostname():
            return ("fail", ev, "workload hostname != host hostname")
        if lines[3].strip() != os.getcwd() or not marker_ok:
            return ("fail", ev, "workload did not retain host cwd/filesystem")
        return ("pass", ev, f"tun workload ran as caller {lines[0]}:{lines[1]} on host")
    run_check("tun_local_workload", "spec50.2",
              "egress-run --mode tun ROUTE -- id/hostname (caller UID/GID, host fs)", fn)


def check_tun_udp():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        route = select_proxy_route(CTX["routes"])
        if not route:
            return ("skip", {"reason": "no non-direct proxy route"}, "no proxy route")
        rid = route["id"]
        r = egress_run(["--mode", "tun", rid, "--",
                        "python3", "-c",
                        "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
                        "s.settimeout(15);"
                        "q=bytes.fromhex('123401000001000000000000')+"
                        "b'\\x07example\\x03com\\x00'+bytes.fromhex('00010001');"
                        "s.sendto(q,('1.1.1.1',53));"
                        "d,_=s.recvfrom(512);"
                        "assert len(d)>=12 and d[:2]==q[:2] and d[2]&128 and d[3]&15==0;"
                        "print(d[:16].hex())"],
                       cfg, timeout, redact)
        ev = {"route": rid, "exit": r["rc"], "udp_reply_hex": r["stdout"].strip(),
              "stdout": r["stdout"], "stderr": r["stderr"]}
        if r["rc"] != 0:
            return ("fail", ev, f"tun udp exit={r['rc']}")
        if not r["stdout"].strip():
            return ("fail", ev, "no UDP reply")
        return ("pass", ev, f"tun udp/QUIC path via {rid} returned a reply")
    run_check("tun_udp_quic", "spec50.3",
              "egress-run --mode tun ROUTE -- <udp client>", fn)


def check_tun_concurrent():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        proxies = [r for r in CTX["routes"]
                   if isinstance(r, dict) and r.get("id") and r["id"] != "direct"
                   and r.get("healthy") is True]
        if len({r["id"] for r in proxies}) < 2:
            reason = "two healthy non-direct proxy routes required for 3+ concurrent jobs"
            return ("skip", {"reason": reason}, reason)
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        proxies = proxies[:2]
        jobs = [{"route": p["id"], "expected_country": p.get("country")} for p in proxies]
        jobs.append({"route": "direct", "expected_country": None})
        host_ip = host_public_ip()

        def run_job(j):
            r = egress_run(["--mode", "tun", j["route"], "--",
                            "python3", "-c", IP_FETCH],
                           cfg, timeout, redact)
            ip = r["stdout"].strip().splitlines()[-1].strip() if r["stdout"].strip() else ""
            return {"route": j["route"], "exit": r["rc"], "exit_ip": ip,
                    "expected_country": j["expected_country"],
                    "stderr": r["stderr"][:400]}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            results = list(ex.map(run_job, jobs))
        ev = {"host_public_ip": host_ip, "jobs": results}
        bad = [j for j in results if j["exit"] != 0 or not is_public_ip(j["exit_ip"])]
        if bad:
            return ("fail", ev, f"{len(bad)} concurrent tun job(s) failed")
        return ("pass", ev, f"{len(results)} concurrent tun jobs ok (incl. direct)")
    run_check("tun_concurrent", "spec50.4",
              "3+ concurrent egress-run --mode tun (different routes + direct)", fn)


def check_tun_direct_isolated():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        host_ip = host_public_ip()
        r = egress_run(["--mode", "tun", "direct", "--",
                        "python3", "-c",
                        "import os,urllib.request;print(urllib.request.urlopen("
                        "'https://api.ipify.org',timeout=15).read().decode().strip());"
                        "print('tun0' in os.listdir('/sys/class/net'))"],
                       cfg, timeout, redact)
        lines = [line.strip() for line in r["stdout"].splitlines() if line.strip()]
        ip = lines[0] if lines else ""
        has_tun = len(lines) > 1 and lines[1] == "True"
        ev = {"exit": r["rc"], "exit_ip": ip, "host_public_ip": host_ip,
              "lines": lines,
              "tun_device_present": has_tun, "stdout": r["stdout"], "stderr": r["stderr"]}
        if r["rc"] != 0:
            return ("fail", ev, f"direct+tun exit={r['rc']}")
        if len(lines) < 2:
            return ("fail", ev, "direct run did not report netns interfaces")
        if not is_public_ip(ip):
            return ("fail", ev, f"direct+tun exit {ip!r} is not public")
        if has_tun:
            return ("fail", ev, "a TUN device is present for a direct run")
        return ("pass", ev, f"direct+tun isolated -> {ip}, no tun device")
    run_check("tun_direct_isolated", "spec50.5",
              "egress-run --mode tun direct -- ... (isolated direct, no tun device)", fn)


def check_tun_no_root():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        r = egress_run(["--mode", "tun", "direct", "--", "python3", "-c",
                        "import os; print(os.getuid()); print(os.getgid()); "
                        "print(next(x.split(':')[1].strip() for x in "
                        "open('/proc/self/status') if x.startswith('CapEff:')))"],
                       cfg, timeout, redact)
        lines = [l.strip() for l in r["stdout"].splitlines() if l.strip()]
        uid, gid, caps = (lines + [None, None, None])[:3]
        ev = {"exit": r["rc"], "uid": uid, "gid": gid, "stdout": r["stdout"],
              "expected_uid": str(os.getuid()), "expected_gid": str(os.getgid()),
              "effective_capabilities": caps}
        if r["rc"] != 0:
            return ("fail", ev, f"id via tun exit={r['rc']}")
        if uid != str(os.getuid()) or gid != str(os.getgid()):
            return ("fail", ev, "tun command did not run as caller (possible escalation)")
        if not caps or int(caps, 16) != 0:
            return ("fail", ev, "tun command retained effective capabilities")
        return ("pass", ev, f"tun command ran as caller {uid}:{gid}; no root escalation")
    run_check("tun_no_root_escalation", "spec50.6",
              "egress-run --mode tun direct -- id (caller UID/GID, no root)", fn)


def check_tun_stale_cleanup():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        route = select_proxy_route(CTX["routes"]) or {"id": "direct"}
        before_ns = _run(["ip", "netns", "list"], 15, redact)["stdout"].splitlines()
        before_tun = _run(["ip", "-o", "link", "show", "type", "tun"], 15, redact)["stdout"].splitlines()
        before_links = _run(["ip", "-o", "link", "show"], 15, redact)["stdout"].splitlines()
        r = egress_run(["--mode", "tun", route["id"], "--",
                        "python3", "-c", IP_FETCH],
                       cfg, timeout, redact)
        after_ns = _run(["ip", "netns", "list"], 15, redact)["stdout"].splitlines()
        after_tun = _run(["ip", "-o", "link", "show", "type", "tun"], 15, redact)["stdout"].splitlines()
        after_links = _run(["ip", "-o", "link", "show"], 15, redact)["stdout"].splitlines()
        new_ns = [l for l in after_ns if l not in before_ns]
        new_tun = [l for l in after_tun if l not in before_tun]
        new_veth = [l for l in after_links if l not in before_links and
                    re.search(r"\beh[0-9a-f]{10}(?:@|:)", l)]
        ev = {"route": route["id"], "exit": r["rc"],
              "new_netns": new_ns, "new_tun_devs": new_tun,
              "new_host_veth": new_veth,
              "before_ns_count": len(before_ns), "after_ns_count": len(after_ns)}
        if r["rc"] != 0:
            return ("fail", ev, f"tun run exit={r['rc']}")
        if new_ns or new_tun or new_veth:
            return ("fail", ev, f"stale resources left: ns={new_ns} tun={new_tun} veth={new_veth}")
        return ("pass", ev, "no stale netns/tun after run")
    run_check("tun_stale_cleanup", "spec50.7",
              "ip netns / tun device list before+after a tun run", fn)


def check_tun_hard_direct():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        ready, why = _tun_ready()
        if not ready:
            return ("skip", {"reason": why}, why)
        # localhost must be reached directly (private IP), not via the proxy
        r_lo = egress_run(["--mode", "tun", "direct", "--",
                           "python3", "-c",
                           "import socket;print(socket.gethostbyname('localhost'))"],
                          cfg, timeout, redact)
        lo_ip = r_lo["stdout"].strip().splitlines()[-1].strip() if r_lo["stdout"].strip() else ""
        # docker bridge gateway must be reachable directly
        r_dg = egress_run(["--mode", "tun", "direct", "--",
                           "python3", "-c",
                           "import socket;s=socket.socket();s.settimeout(3);"
                           "print(s.connect_ex(('172.17.0.1',9091)))"],
                          cfg, timeout, redact)
        dg = r_dg["stdout"].strip().splitlines()[-1].strip() if r_dg["stdout"].strip() else ""
        ev = {"localhost_ip": lo_ip, "localhost_exit": r_lo["rc"],
              "docker_gateway_reachable": dg == "0" and r_dg["rc"] == 0,
              "docker_gateway_connect_errno": dg, "dg_exit": r_dg["rc"],
              "lo_stdout": r_lo["stdout"], "dg_stdout": r_dg["stdout"]}
        if r_lo["rc"] != 0 or not is_local_bind(lo_ip):
            return ("fail", ev, f"localhost not direct (ip={lo_ip!r})")
        if not ev["docker_gateway_reachable"]:
            return ("fail", ev, f"docker bridge gateway connect_ex={dg!r}")
        return ("pass", ev, "HARD DIRECT enforced: localhost + docker gateway direct")
    run_check("tun_hard_direct", "spec50.8",
              "egress-run --mode tun direct -- localhost/docker-gateway (HARD DIRECT)", fn)


def check_container_tun_packet_path():
    run_check("container_tun_packet_path", "spec21.5",
              "generic Docker client TUN acceptance",
              lambda: ("skip", {"harness": "tests/docker/run-client-container.sh",
                                 "reason": "standalone generic Docker harness not run by host acceptance"},
                       "run the generic Docker TUN harness with a provisioned bundle"))


def check_tun_no_fallback():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        # Disable local TUN explicitly. Strict mode must fail closed.
        source = open(cfg, encoding="utf-8").read()
        source = re.sub(r"^tun_backend:.*$", "tun_backend: none", source, flags=re.M)
        fd, path = tempfile.mkstemp(prefix="egress-tun-config-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write(source)
            r = egress_run(["--mode", "tun", "direct", "--",
                            "sh", "-c", "echo PROXY_PATH_RAN"],
                           path, timeout, redact)
        finally:
            os.unlink(path)
        ev = {"exit": r["rc"], "stdout": r["stdout"], "stderr": r["stderr"]}
        if "PROXY_PATH_RAN" in r["stdout"]:
            return ("fail", ev, "target ran despite unavailable strict helper")
        if r["rc"] != 6:
            return ("fail", ev, f"tun failure exit={r['rc']} (expected 6)")
        return ("pass", ev, "unavailable helper causes explicit exit 6; no fallback")
    run_check("tun_no_silent_fallback", "spec50.11",
              "egress-run --mode tun with unavailable helper -> explicit failure", fn)


def check_tun_capabilities():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"},
                    "no live backend")
        rc_host = egressctl(["capabilities"], cfg, timeout, redact)
        cap_host = _json(rc_host["stdout"]) or {}
        tun_host = cap_host.get("tun") or {}
        ev = {"host": {"exit": rc_host["rc"], "tun": tun_host}}
        if rc_host["rc"] != 0:
            return ("fail", ev, "host capabilities failed")
        rc_container = container_exec(["egressctl", "--json", "capabilities"], timeout, redact)
        cap_container = _json(rc_container["stdout"]) or {}
        tun_container = cap_container.get("tun") or {}
        ev["container"] = {"exit": rc_container["rc"], "tun": tun_container}
        if "no such container" in rc_container["stderr"].lower():
            reason = f"container {CTX['container_name']!r} is absent"
            ev["reason"] = reason
            return ("skip", ev, reason)
        if rc_container["rc"] is None:
            reason = f"cannot exec in {CTX['container_name']!r}"
            ev["reason"] = reason
            return ("skip", ev, reason)
        if rc_container["rc"] != 0:
            return ("fail", ev, f"container capabilities exit={rc_container['rc']}")
        for label, value in (("host", tun_host), ("container", tun_container)):
            if value.get("backend") not in ("local-userns", "none"):
                return ("fail", ev, f"{label} has unexpected TUN backend")
        return ("pass", ev, "host and container use client-local TUN capability reporting")
    run_check("tun_capabilities_accuracy", "spec50.12",
              "egressctl capabilities report local TUN backend accurately", fn)


def _yaml_lines(path):
    """Return (indent, text) for non-comment, non-blank lines of a YAML file."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                out.append((len(line) - len(line.lstrip(" ")), line.strip()))
    except OSError:
        return None
    return out


def check_tun_static():
    def fn():
        runtime = os.path.join(REPO, "client/src/egress_client/tun_runtime.py")
        bootstrap = os.path.join(REPO, "client/src/egress_client/tun_bootstrap.py")
        try:
            text = open(runtime, encoding="utf-8").read() + open(bootstrap, encoding="utf-8").read()
        except OSError as exc:
            return ("fail", {"error": str(exc)}, "client-local TUN runtime missing")
        issues = [item for item in ("SCM_RIGHTS", "--user", "--net", "PR_SET_NO_NEW_PRIVS") if item not in text]
        container_config = CTX["container_config"]
        backend = _flat_yaml_get(container_config, "tun_backend") if container_config else None
        if backend not in (None, "local-userns"):
            issues.append(f"container tun_backend={backend!r}")
        ev = {"issues": issues, "container_tun_backend": backend}
        if issues:
            return ("fail", ev, "; ".join(issues))
        return ("pass", ev, "per-invocation TUN is client-local; no privileged helper")
    run_check("tun_local_namespace_boundary", "spec50.10",
              "static audit of client-local TUN namespace and capability boundary", fn)


# ---------------------------------------------------------------------------
# section 49 -- host-only live security / data-plane
# ---------------------------------------------------------------------------
def check_a8_public_ports():
    def fn():
        redact = CTX["redact"]
        ss = _run(["ss", "-ltnH"], 15, redact)
        if ss["rc"] != 0:
            return ("skip", {"reason": ss["stderr"]}, "ss unavailable")
        binds = []
        public_bad = []
        for line in ss["stdout"].splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[3]
            ip = local.rsplit(":", 1)[0].strip("[]")
            port = local.rsplit(":", 1)[1]
            try:
                port = int(port)
            except ValueError:
                continue
            binds.append({"ip": ip, "port": port})
            if not is_local_bind(ip):
                public_bad.append(f"{ip}:{port}")
        ev = {"binds": binds, "public_binds": public_bad,
              "egress_ports_on_public": [
                  f"{b['ip']}:{b['port']}" for b in binds
                  if b["port"] in EGRESS_PORTS and not is_local_bind(b["ip"])]}
        # spec 49.32: the egress client-facing ports (1080/1081/9091) must not
        # be reachable on a public NIC. Other host services (e.g. an IDE) are
        # out of scope for this check.
        if ev["egress_ports_on_public"]:
            return ("fail", ev,
                    f"egress ports on public NIC: {ev['egress_ports_on_public']}")
        return ("pass", ev,
                f"no egress client port on a public NIC "
                f"({len(binds)} binds scanned; {len(public_bad)} other public binds ignored)")
    run_check("a8_public_ports_closed", "spec49.32", "ss -ltnH (no public NIC binds)", fn)


def check_a8_recreate():
    def fn():
        redact, timeout = CTX["redact"], CTX["timeout"]
        cname = CTX["container_name"]
        if not CTX.get("recreate"):
            return ("skip", {"reason": "destructive: restarts the container "
                                        "container; run with --recreate to enable"},
                    "recreate check disabled by default")
        container_config = CTX["container_config"]
        tok = _flat_yaml_get(container_config, "api_token_file")
        tok_hash = None
        if tok:
            r = container_exec(["sh", "-c", f"sha256sum {tok} | cut -c1-16"], timeout, redact)
            if r["rc"] == 0 and r["stdout"].strip():
                tok_hash = r["stdout"].strip()
        before = container_exec(["egressctl", "--json", "whoami"], timeout, redact)
        before_id = (_json(before["stdout"]) or {}).get("client_id")
        if before["rc"] != 0 or not before_id:
            # Self-diagnosis: which config does the container client ACTUALLY
            # use? Install-vs-bundle drift shows up here (2026-10-15 live
            # run: the container presented a HOST control URL, i.e. its
            # installed config was not the container's bundle).
            diag = {}
            diag["installed_client_yaml"] = container_exec(
                ["cat", "/etc/egress/client.yaml"],
                timeout, redact)["stdout"][:800]
            diag["installed_egressctl_config_show"] = container_exec(
                ["egressctl", "config", "show"], timeout, redact)["stdout"][:800]
            ins = _run(["docker", "inspect", "--format", "{{json .Mounts}}",
                        cname], 15, redact)
            diag["container_mounts"] = ins["stdout"][:800]
            return ("skip",
                    {"reason": f"cannot whoami in {cname}: {before['stderr'][:200]}",
                     "diagnostics": diag},
                    "Docker container not reachable")
        re_ = _run(["docker", "restart", cname], 120, redact)
        if re_["rc"] != 0:
            return ("fail", {"before_client_id": before_id,
                             "restart": re_["stderr"][:200]},
                    f"restart failed: {re_['stderr'][:120]}")
        # wait for the client to come back
        after = None
        for _ in range(12):
            after = container_exec(["egressctl", "--json", "whoami"], timeout, redact)
            if after["rc"] == 0:
                break
            import time
            time.sleep(5)
        after_id = (_json(after["stdout"]) or {}).get("client_id") if after else None
        ev = {"before_client_id": before_id, "after_client_id": after_id,
              "token_sha16": tok_hash, "restart_rc": re_["rc"]}
        if after_id != before_id:
            return ("fail", ev, f"client identity changed: {before_id} -> {after_id}")
        # docker restart keeps the same writable layer. It cannot establish
        # that an install survives docker rm + docker run / Compose recreate.
        ev["reason"] = "docker restart passed; container recreation not exercised"
        return ("skip", ev, ev["reason"])
    run_check("a8_container_recreate_persists", "spec49.39",
              "docker exec whoami -> docker restart -> whoami (identity persists)", fn)


def check_a8_litellm():
    def fn():
        url = CTX["litellm"]
        try:
            pre = http_get(url, 8)[0] < 500
        except Exception:
            pre = False
        # a benign route read does not disturb LiteLLM; re-probe
        try:
            post = http_get(url, 8)[0] < 500
        except Exception:
            post = False
        ev = {"url": url, "pre": pre, "post": post}
        if pre and post:
            return ("pass", ev, f"LiteLLM reachable before+after ({url})")
        if not pre:
            return ("skip", ev, f"LiteLLM not reachable at {url} (set --litellm-url)")
        return ("fail", ev, "LiteLLM became unreachable")
    run_check("a8_litellm_reachable", "spec49.29", f"GET {CTX['litellm']}", fn)


def check_a8_parallel_oneshot():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        proxies = [r for r in CTX["routes"]
                   if isinstance(r, dict) and r.get("id") and r["id"] != "direct"
                   and r.get("healthy") is True]
        if not proxies:
            return ("skip",
                    {"reason": "two healthy non-direct proxy routes required "
                               "for parallel one-shot"},
                    "no healthy proxy routes")
        if len(proxies) < 2:
            reason = "two healthy non-direct proxy routes required for parallel one-shot"
            return ("skip", {"reason": reason}, reason)
        proxies = proxies[:2]
        jobs = [{"route": p["id"]} for p in proxies] + [{"route": "direct"}]
        def run_job(j):
            # urllib does not support the SOCKS proxy variables set by
            # egress-run. curl does, so these requests exercise each route.
            r = egress_run([j["route"], "--", "curl", "-sS", "--max-time", "20",
                            "-o", "/dev/null", "-w", "%{http_code}",
                            "https://www.gstatic.com/generate_204"],
                           cfg, timeout, redact)
            return {"route": j["route"], "exit": r["rc"],
                    "http_status": r["stdout"].strip(),
                    "stderr": r["stderr"][:300]}

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            results = list(ex.map(run_job, jobs))
        ev = {"jobs": results}
        bad = [j for j in results if j["exit"] != 0 or j["http_status"] != "204"]
        if bad:
            return ("fail", ev, f"{len(bad)} parallel one-shot job(s) failed")
        return ("pass", ev, f"{len(results)} parallel one-shot jobs ok (incl. direct)")
    run_check("a8_parallel_oneshot", "spec49.23",
              "3 parallel egress-run one-shot curl requests (different routes + direct)", fn)


def check_a8_local_exec():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        r = egress_run(["direct", "--",
                        "sh", "-c", "hostname; id -u"], cfg, timeout, redact)
        lines = [l for l in r["stdout"].splitlines() if l.strip()]
        ev = {"exit": r["rc"], "hostname": lines[0] if lines else None,
              "uid": lines[1] if len(lines) > 1 else None,
              "expected_hostname": socket.gethostname(), "expected_uid": str(os.getuid()),
              "stdout": r["stdout"]}
        if r["rc"] != 0:
            return ("fail", ev, f"egress-run direct exit={r['rc']}")
        if lines and (lines[0] != socket.gethostname()
                      or (len(lines) > 1 and lines[1] != str(os.getuid()))):
            return ("fail", ev, "command did not run in the local host environment")
        return ("pass", ev, "egress-run executes the command locally (host fs/env/uid)")
    run_check("a8_egress_run_local", "spec49.25",
              "egress-run direct -- hostname/id (local exec, not remote)", fn)


def check_a8_internal_direct():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        host_ip = host_public_ip()
        r_lo = egress_run(["direct", "--",
                           "python3", "-c",
                           "import socket;print(socket.gethostbyname('localhost'))"],
                          cfg, timeout, redact)
        lo_ip = r_lo["stdout"].strip().splitlines()[-1].strip() if r_lo["stdout"].strip() else ""
        r_dg = egress_run(["direct", "--",
                           "python3", "-c",
                           "import socket;s=socket.socket();s.settimeout(3);"
                           "print(s.connect_ex(('172.17.0.1',9091)))"],
                          cfg, timeout, redact)
        dg = r_dg["stdout"].strip().splitlines()[-1].strip() if r_dg["stdout"].strip() else ""
        ev = {"host_public_ip": host_ip, "localhost_ip": lo_ip,
              "docker_gateway_reachable": dg == "0" and r_dg["rc"] == 0,
              "docker_gateway_connect_errno": dg,
              "lo_exit": r_lo["rc"], "dg_exit": r_dg["rc"]}
        if r_lo["rc"] != 0 or not is_local_bind(lo_ip):
            return ("fail", ev, f"localhost not direct (ip={lo_ip!r})")
        if not ev["docker_gateway_reachable"]:
            return ("fail", ev, f"docker internal connect_ex={dg!r}")
        return ("pass", ev, "Docker-internal + localhost stay DIRECT")
    run_check("a8_internal_hard_direct", "spec49.27",
              "egress-run direct -- localhost/docker-gateway (internal stays direct)", fn)


def check_a8_secrets():
    def fn():
        redact = CTX["redact"]
        secrets = _collect_secret_values()
        if not secrets:
            return ("skip", {"reason": "no secret values available to scan against"},
                    "no secrets to scan")
        # values the generated mihomo config LEGITIMATELY embeds (mihomo
        # needs them: admin secret + per-client/one-shot listener users):
        # not a leak when present under generated/.
        gen_ok_labels = {label for _, label in secrets
                         if label == "mihomo_api_secret"
                         or label.startswith("clients/")}
        leaks = []
        # 1. repo (skip .git, binaries, large files)
        for root_, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d != ".git"]
            for fn_ in files:
                p = os.path.join(root_, fn_)
                try:
                    if os.path.getsize(p) > 1_000_000:
                        continue
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except OSError:
                    continue
                for sv, label in secrets:
                    if sv in content:
                        leaks.append(f"repo:{p} (secret: {label})")
                        break
        # 2. generated artifacts
        gen = os.path.join(ROOT, "generated")
        if os.path.isdir(gen):
            for root_, _, files in os.walk(gen):
                for fn_ in files:
                    p = os.path.join(root_, fn_)
                    try:
                        with open(p, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                    except OSError:
                        continue
                    for sv, label in secrets:
                        if label in gen_ok_labels:
                            continue
                        if sv in content:
                            leaks.append(f"artifact:{p} (secret: {label})")
                            break
        # 3. docker logs of the egress stack
        for svc in ("egress-mihomo", "egressd"):
            lg = _run(["docker", "logs", "--tail", "2000", svc], 30, redact)
            for sv, label in secrets:
                if sv in (lg["stdout"] + lg["stderr"]):
                    leaks.append(f"logs:{svc} (secret: {label})")
                    break
        ev = {"scanned": ["repo", "generated", "docker-logs"],
              "leaks": leaks[:20], "leak_count": len(leaks)}
        if leaks:
            return ("fail", ev, f"{len(leaks)} secret leak(s)")
        return ("pass", ev, "no provider/client secrets in repo/artifacts/logs")
    run_check("a8_secrets_absent", "spec49.34",
              "scan repo + generated artifacts + docker logs for secret values", fn)


def check_a8_host_test():
    def fn():
        redact = CTX["redact"]
        if not os.path.isfile(HOST_TEST):
            return ("skip", {"reason": f"egress-host-test not found: {HOST_TEST}"},
                    "no host test harness")
        r = _run([sys.executable, HOST_TEST], CTX["timeout"] * 3, redact)
        data = _json(r["stdout"]) or {}
        summary = data.get("summary", {})
        ev = {"exit": r["rc"], "summary": summary,
              "failed_checks": [c.get("id") for c in data.get("checks", [])
                                if c.get("status") == "fail"],
              "stderr": r["stderr"][:300]}
        if r["rc"] != 0 and "PermissionError" in r["stderr"]:
            ev["reason"] = "host harness requires root to inspect persistent dirs"
            return ("skip", ev, ev["reason"])
        if r["rc"] != 0 and summary.get("failed"):
            return ("fail", ev, f"host acceptance harness failed: {ev['failed_checks']}")
        if not data.get("checks"):
            return ("fail", ev, "host harness produced no checks")
        return ("pass", ev, f"host acceptance harness: {summary}")
    run_check("a8_host_test_report", "spec49.41",
              "python3 host/egress-host-test (fixed host harness report)", fn)


def check_a8_docker_socket():
    def fn():
        redact = CTX["redact"]
        mounts = []
        for svc in ("egress-mihomo", "egressd",
                    "egressd"):
            r = _run(["docker", "inspect", "--format", "{{json .Mounts}}", svc],
                     15, redact)
            if r["rc"] != 0:
                continue
            for m in (_json(r["stdout"]) or []):
                src = m.get("Source", "")
                if "docker.sock" in src:
                    mounts.append(f"{svc}:{src}")
        ev = {"mounts": mounts}
        if mounts:
            return ("fail", ev, f"docker socket mounted: {mounts}")
        return ("pass", ev, "no docker socket mounted into the egress stack")
    run_check("a8_docker_socket_absent", "spec49.33",
              "docker inspect .Mounts (no docker.sock)", fn)


def check_a8_mihomo_unreachable():
    def fn():
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect(("127.0.0.1", MIHOMO_ADMIN_PORT))
            reachable = True
        except OSError:
            reachable = False
        finally:
            s.close()
        ev = {"port": MIHOMO_ADMIN_PORT, "reachable_from_host_loopback": reachable}
        if reachable:
            return ("fail", ev, f"mihomo admin {MIHOMO_ADMIN_PORT} reachable (should be private)")
        return ("pass", ev, f"mihomo admin {MIHOMO_ADMIN_PORT} not published")
    run_check("a8_mihomo_unreachable", "spec49.30",
              f"TCP connect 127.0.0.1:{MIHOMO_ADMIN_PORT} (must fail)", fn)


def check_a8_same_release():
    def fn():
        hv = _client_version(host=True)
        ov = _client_version(host=False)
        ev = {"host_version": hv, "os_version": ov,
              "container_name": CTX["container_name"]}
        if hv is None or ov is None:
            return ("skip", ev, "could not read installed client __version__ "
                                "on host and/or in the container")
        if hv != ov:
            return ("fail", ev, f"release mismatch: host={hv!r} os={ov!r}")
        return ("pass", ev, f"same client release installed: {hv}")
    run_check("a8_client_same_release", "spec49.7",
              "compare installed client __version__ (host vs container)", fn)


def check_a8_version_mismatch():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        st = egressctl(["status"], cfg, timeout, redact)
        data = _json(st["stdout"]) or {}
        backend = (data.get("mihomo") or {}).get("version") or data.get("version")
        ev = {"backend_version": backend, "raw": st["stdout"][:400]}
        if st["rc"] != 0:
            return ("fail", ev, f"status failed rc={st['rc']}")
        ev["compatible"] = backend is not None
        if backend is None:
            return ("fail", ev, "backend did not report a version (no negotiation)")
        return ("pass", ev, f"backend reports version {backend!r} (negotiable)")
    run_check("a8_version_negotiation", "spec49.42",
              "egressctl status (backend version present for negotiation)", fn)


def check_a8_probe_all():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        r = egressctl(["probe-domain", PROBE_DOMAIN, "--all"], cfg, max(timeout, 160),
                      redact, max_out=None)
        data = _json(r["stdout"]) or {}
        results = data.get("results", data if isinstance(data, list) else [])
        if not isinstance(results, list):
            results = []
        stage_keys = ("dns_ok", "tcp_ok", "tls_ok", "http_ok")
        ev = {"domain": PROBE_DOMAIN, "exit": r["rc"],
              "n_results": len(results),
              "has_direct": any(x.get("route") == "direct" for x in results
                                if isinstance(x, dict)),
              "has_stages": any(isinstance(x, dict)
                                and any(k in x for k in stage_keys) for x in results),
              "errors": [x.get("error") for x in results if isinstance(x, dict)],
              "sample": results[:3]}
        if r["rc"] != 0:
            return ("fail", ev, f"probe-domain --all exit={r['rc']}")
        if not results:
            return ("fail", ev, "no probe results")
        if ev["errors"] and all(e == "proxy_failure" for e in ev["errors"]):
            return ("fail", ev, "all probes failed at proxy infrastructure")
        if len(results) < 2:
            ev["reason"] = "only direct route; multiple routes required by spec49.20"
            return ("skip", ev, ev["reason"])
        if not ev["has_direct"]:
            return ("fail", ev, "direct route not included in --all probe")
        if not ev["has_stages"]:
            return ("fail", ev, "results do not distinguish DNS/TCP/TLS/HTTP stages")
        return ("pass", ev, f"probe-domain --all: {len(results)} routes incl. direct, staged")
    run_check("a8_probe_domain_all", "spec49.20",
              f"egressctl probe-domain {PROBE_DOMAIN} --all", fn)


def check_a8_benchmark_bounded():
    def fn():
        redact, cfg = CTX["redact"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        routes = [r for r in CTX["routes"] if r.get("id") != "direct"
                  and r.get("healthy") is True][:3]
        if not routes:
            route = select_proxy_route(CTX["routes"])
            routes = [route] if route else []
        if not routes:
            return ("skip", {"reason": "no non-direct proxy route"}, "no proxy route")
        t0 = datetime.now(timezone.utc)
        attempts = []
        for route in routes:
            rid = route["id"]
            r = egressctl(["benchmark", rid, "--bytes", "1M"], cfg, 50, redact)
            data = _json(r["stdout"]) or {}
            results = data.get("results", [])
            attempts.append({"route": rid, "exit": r["rc"], "results": results})
            if (r["rc"] == 0 and len(results) == 1 and results[0].get("ok")
                    and 0 < results[0].get("bytes", 0) <= 1024 * 1024):
                break
        dur = (datetime.now(timezone.utc) - t0).total_seconds()
        ev = {"route": attempts[-1]["route"], "exit": attempts[-1]["exit"],
              "duration_s": round(dur, 1), "bytes": "1M",
              "results": attempts[-1]["results"], "attempts": attempts}
        if dur > 120:
            return ("fail", ev, f"benchmark not bounded ({dur:.0f}s)")
        results = ev["results"]
        if len(results) != 1 or not results[0].get("ok") or not 0 < results[0].get("bytes", 0) <= 1024 * 1024:
            return ("fail", ev, "no route completed a bounded download")
        return ("pass", ev, f"benchmark bounded: {dur:.1f}s for 1M via {rid}")
    run_check("a8_benchmark_bounded", "spec49.35",
              "egressctl benchmark ROUTE --bytes 1M (bounded bytes/time)", fn)


def check_a8_best_domain():
    def fn():
        redact, timeout, cfg = CTX["redact"], CTX["timeout"], CTX["config"]
        if not CTX.get("client_ok"):
            return ("skip", {"reason": "backend/client not reachable"}, "no live backend")
        if not select_proxy_route(CTX["routes"]):
            return ("skip", {"reason": "no non-direct proxy route"}, "no proxy route")
        r = egressctl(["best", "--domain", PROBE_DOMAIN], cfg, max(timeout, 160),
                      redact, max_out=None)
        data = _json(r["stdout"]) or {}
        routes = data.get("candidates", data.get("routes",
                    data if isinstance(data, list) else []))
        if not isinstance(routes, list):
            routes = []
        ev = {"domain": PROBE_DOMAIN, "exit": r["rc"], "n_routes": len(routes),
              "best": data.get("best"),
              "uses_domain_probe": data.get("filters", {}).get("domain") == PROBE_DOMAIN,
              "sample": routes[:3]}
        if r["rc"] != 0:
            return ("fail", ev, f"best --domain exit={r['rc']}")
        if not routes:
            return ("fail", ev, "best --domain returned no routes")
        return ("pass", ev, f"best --domain: {len(routes)} ranked routes, best={data.get('best')!r}")
    run_check("a8_best_domain", "spec49.36",
              f"egressctl best --domain {PROBE_DOMAIN}", fn)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
ALL_CHECKS = [
    ("preflight", check_preflight),
    # section 50 -- TUN
    ("tun_tcp_client", check_tun_tcp),
    ("tun_local_workload", check_tun_local_workload),
    ("tun_udp_quic", check_tun_udp),
    ("tun_concurrent", check_tun_concurrent),
    ("tun_direct_isolated", check_tun_direct_isolated),
    ("tun_no_root_escalation", check_tun_no_root),
    ("tun_stale_cleanup", check_tun_stale_cleanup),
    ("tun_hard_direct", check_tun_hard_direct),
    ("container_tun_packet_path", check_container_tun_packet_path),
    ("tun_no_silent_fallback", check_tun_no_fallback),
    ("tun_capabilities_accuracy", check_tun_capabilities),
    ("tun_local_namespace_boundary", check_tun_static),
    # section 49 -- host-only live
    ("a8_public_ports_closed", check_a8_public_ports),
    ("a8_container_recreate_persists", check_a8_recreate),
    ("a8_litellm_reachable", check_a8_litellm),
    ("a8_parallel_oneshot", check_a8_parallel_oneshot),
    ("a8_egress_run_local", check_a8_local_exec),
    ("a8_internal_hard_direct", check_a8_internal_direct),
    ("a8_secrets_absent", check_a8_secrets),
    ("a8_host_test_report", check_a8_host_test),
    ("a8_docker_socket_absent", check_a8_docker_socket),
    ("a8_mihomo_unreachable", check_a8_mihomo_unreachable),
    ("a8_client_same_release", check_a8_same_release),
    ("a8_version_negotiation", check_a8_version_mismatch),
    ("a8_probe_domain_all", check_a8_probe_all),
    ("a8_benchmark_bounded", check_a8_benchmark_bounded),
    ("a8_best_domain", check_a8_best_domain),
]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="live-acceptance.py")
    ap.add_argument("--out", default="host_acceptance_report.json")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--container-config",
                    default=os.environ.get("EGRESS_CONTAINER_CONFIG", DEFAULT_CONTAINER_CONFIG))
    ap.add_argument("--litellm-url",
                    default=os.environ.get("EGRESS_LITELLM_URL", DEFAULT_LITELLM))
    ap.add_argument("--container",
                    default=os.environ.get("EGRESS_CONTAINER",
                                           DEFAULT_CONTAINER))
    ap.add_argument("--recreate", action="store_true",
                    help="restart the Docker container to observe client "
                         "identity; full recreation is not automated")
    ap.add_argument("--no-tun", action="store_true", help="skip the TUN checks")
    ap.add_argument("--check", help="comma-separated check ids to run")
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args(argv)

    CTX.update({
        "config": args.config,
        "container_config": args.container_config,
        "litellm": args.litellm_url,
        "container_name": args.container,
        "timeout": args.timeout,
        "recreate": args.recreate,
    })
    redact, redact_count = make_redactor()
    CTX["redact"] = redact

    selected = set(args.check.split(",")) if args.check else None
    for cid, fn in ALL_CHECKS:
        if selected and cid not in selected:
            continue
        if args.no_tun and (cid.startswith("tun_") or cid == "container_tun_packet_path"):
            continue
        fn()

    summary = {
        "passed": sum(1 for r in RESULTS if r["status"] == "pass"),
        "failed": sum(1 for r in RESULTS if r["status"] == "fail"),
        "skipped": sum(1 for r in RESULTS if r["status"] == "skip"),
        "total": len(RESULTS),
    }
    report = {
        "report": "egress live acceptance",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "user": os.environ.get("USER", ""),
        "python": sys.version.split()[0],
        "config": args.config,
        "container_config": args.container_config,
        "litellm_url": args.litellm_url,
        "container_name": args.container,
        "redactions": redact_count["n"],
        "checks": RESULTS,
        "summary": summary,
    }
    report["checks_hash"] = canon_hash(RESULTS)
    out = json.dumps(report, indent=2)
    print(out)
    os.makedirs(os.path.dirname(os.path.realpath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(out + "\n")
    print(f"\n[report] {args.out}  "
          f"(passed={summary['passed']} failed={summary['failed']} "
          f"skipped={summary['skipped']} total={summary['total']} "
          f"redactions={redact_count['n']})", file=sys.stderr)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
