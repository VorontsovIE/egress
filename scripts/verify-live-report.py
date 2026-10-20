#!/usr/bin/env python3
"""verify-live-report.py -- strict re-verification of a live acceptance report.

Takes the single JSON file produced by host/live-acceptance.py and INDEPENDENTLY
re-derives every pass/fail/skip from the raw evidence embedded in the file. It
does NOT trust the stored status: a check stored as "pass" is only VERIFIED if
the raw evidence actually supports it. This makes the report tamper-evident and
self-contained, so it can be re-verified on a machine without host access.

Layers of verification:
  1. Structure      -- required fields, valid statuses, summary counts match.
  2. Tamper hash    -- report.checks_hash must equal the canonical hash of each
                       check's {id, spec, command, evidence}. Editing the
                       evidence or command after the fact breaks this.
  3. Re-derivation  -- every stored "pass" is re-derived from its evidence by an
                       independent implementation of the verdict logic. A stored
                       pass whose evidence does not support it is flagged.
  4. Secret scan    -- the report text is scanned for any secret values readable
                       on the verifier host (real scan when run on the host;
                       "skipped_no_secrets" when run elsewhere).

Usage:
    python3 scripts/verify-live-report.py host_acceptance_report.json
    python3 scripts/verify-live-report.py --json host_acceptance_report.json

Exit code: 0 if VERIFIED (structure ok, hash matches, every stored pass is
supported by its evidence, no secret leaks), 1 otherwise.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

def _secrets_dir():
    return os.environ.get("VIBELAB_EGRESS_SECRETS", "/etc/vibelab-egress/secrets")

REQUIRED_TOP = ("report", "generated_at", "host", "checks", "summary", "checks_hash")
VALID_STATUS = ("pass", "fail", "skip")
UNSUPPORTED_TUN = ("unsupported_in_environment", "unsupported", "false", "none")


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


def canon_hash(checks):
    parts = []
    for c in checks:
        parts.append(json.dumps(
            {"id": c.get("id"), "spec": c.get("spec"),
             "command": c.get("command"), "evidence": c.get("evidence")},
            sort_keys=True, separators=(",", ":")))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# independent re-derivation: does the EVIDENCE support a "pass"?
# returns (supported: bool, note: str)
# ---------------------------------------------------------------------------
def _ev(c, key, default=None):
    return (c.get("evidence") or {}).get(key, default)


def rederive_pass(cid, c):
    ev: dict = c.get("evidence") or {}

    def get(k, d=None) -> Any:
        return ev.get(k, d)

    if cid == "preflight":
        if not (get("docker", {}).get("rc") == 0 and get("egressctl", {}).get("rc") == 0
                and get("routes")):
            return False, "docker/egressctl not ok or no routes"
        return True, "host ready"

    if cid == "tun_tcp_client":
        if get("exit") != 0 or not is_public_ip(get("exit_ip", "")):
            return False, "exit!=0 or exit_ip not public"
        if get("expected_country") and get("exit_country"):
            if str(get("exit_country")).upper() != str(get("expected_country")).upper():
                return False, "exit country mismatch"
        return True, "tun tcp -> public ip"

    if cid == "tun_local_workload":
        if get("exit") != 0:
            return False, "exit!=0"
        if get("uid") != str(get("expected_uid", "")) or get("gid") != str(get("expected_gid", "")):
            return False, "uid/gid mismatch"
        if get("hostname") != get("expected_hostname"):
            return False, "hostname mismatch"
        if not get("host_marker_written") or len(get("lines", [])) < 4 or get("lines")[3] != get("expected_cwd"):
            return False, "host filesystem/cwd not confirmed"
        return True, "ran as caller on host"

    if cid == "tun_udp_quic":
        if get("exit") != 0 or not get("udp_reply_hex"):
            return False, "no udp reply"
        return True, "udp reply"

    if cid in ("tun_concurrent", "a8_parallel_oneshot"):
        jobs = get("jobs", [])
        routes = [j.get("route") for j in jobs]
        if len(jobs) < 3 or routes.count("direct") != 1 or len(set(routes)) != len(routes):
            return False, "requires direct plus two distinct proxy routes"
        if cid == "a8_parallel_oneshot":
            for j in jobs:
                if j.get("exit") != 0 or j.get("http_status") != "204":
                    return False, f"job {j.get('route')} failed"
            return True, f"{len(jobs)} jobs returned HTTP 204"
        for j in jobs:
            if j.get("exit") != 0 or not is_public_ip(j.get("exit_ip", "")):
                return False, f"job {j.get('route')} failed"
        return True, f"{len(jobs)} jobs ok"

    if cid == "tun_direct_isolated":
        if get("exit") != 0:
            return False, "exit!=0"
        lines = get("lines", [])
        if len(lines) < 2 or lines[0] != get("exit_ip") or lines[1] != "False":
            return False, "direct netns interface evidence missing"
        if not is_public_ip(get("exit_ip", "")):
            return False, "direct ip is not public"
        if get("tun_device_present"):
            return False, "tun device present for direct"
        return True, "isolated direct"

    if cid == "tun_no_root_escalation":
        if get("exit") != 0:
            return False, "exit!=0"
        if get("uid") != str(get("expected_uid", "")) or get("gid") != str(get("expected_gid", "")):
            return False, "not caller uid/gid"
        try:
            if int(get("effective_capabilities", ""), 16) != 0:
                return False, "effective capabilities retained"
        except (TypeError, ValueError):
            return False, "effective capabilities absent"
        return True, "no escalation"

    if cid == "tun_stale_cleanup":
        if get("exit") != 0:
            return False, "exit!=0"
        if get("new_netns") or get("new_tun_devs") or get("new_host_veth"):
            return False, "stale resources"
        return True, "clean"

    if cid in ("tun_hard_direct", "a8_internal_hard_direct"):
        lo = get("localhost_ip", "")
        if not is_local_bind(lo):
            return False, "localhost not direct"
        if (not get("docker_gateway_reachable") or
                get("docker_gateway_connect_errno") != "0" or
                get("dg_exit") != 0):
            return False, "docker gateway not direct"
        return True, "hard direct ok"

    if cid == "container_tun_packet_path":
        if get("exit") != 0:
            return False, f"exit={get('exit')}"
        if not get("workload_inside_client_container"):
            return False, "workload did not stay inside client container"
        if not get("engine_received_tun_fd_only"):
            return False, "packet engine input boundary missing"
        if get("workload_argv_sent_to_engine"):
            return False, "workload argv reached packet engine"
        return True, "generic container TUN path passed"

    if cid == "tun_no_silent_fallback":
        if get("exit") != 6:
            return False, f"exit={get('exit')} (expected 6)"
        if "PROXY_PATH_RAN" in get("stdout", ""):
            return False, "proxy path ran (fallback)"
        return True, "explicit failure, no fallback"

    if cid == "tun_capabilities_accuracy":
        if get("host", {}).get("exit") != 0:
            return False, "host capabilities did not exit successfully"
        host_tun = get("host", {}).get("tun", {})
        if get("container", {}).get("exit") != 0:
            return False, "container capabilities did not exit successfully"
        container_tun = get("container", {}).get("tun", {})
        for label, value in (("host", host_tun), ("container", container_tun)):
            if not isinstance(value, dict) or value.get("backend") not in ("local-userns", "none"):
                return False, f"{label} TUN backend is invalid"
        return True, "capabilities accurate"

    if cid == "tun_local_namespace_boundary":
        if get("issues"):
            return False, f"issues: {get('issues')}"
        if get("container_tun_backend") not in (None, "local-userns"):
            return False, f"container tun_backend={get('container_tun_backend')!r}"
        return True, "client-local namespace boundary"

    if cid == "a8_public_ports_closed":
        # spec 49.32: only the egress client-facing ports matter; other host
        # services (e.g. an IDE on 0.0.0.0) are out of scope.
        if get("egress_ports_on_public"):
            return False, f"egress ports on public NIC: {get('egress_ports_on_public')}"
        return True, "no egress port on a public NIC"

    if cid == "a8_container_recreate_persists":
        return False, "docker restart does not prove persistence across recreation"

    if cid == "a8_litellm_reachable":
        if not (get("pre") and get("post")):
            return False, "litellm not reachable"
        return True, "reachable"

    if cid == "a8_egress_run_local":
        if get("exit") != 0:
            return False, "exit!=0"
        if get("hostname") != get("expected_hostname") or get("uid") != str(get("expected_uid", "")):
            return False, "not local host env"
        return True, "local exec"

    if cid == "a8_secrets_absent":
        if get("leak_count", -1) != 0 or not get("scanned"):
            return False, f"leaks={get('leak_count')}"
        return True, "no leaks"

    if cid == "a8_host_test_report":
        if get("exit") != 0:
            return False, f"exit={get('exit')}"
        if get("failed_checks"):
            return False, f"failed: {get('failed_checks')}"
        if not (get("summary") or {}).get("total"):
            return False, "no checks in host harness"
        return True, "host harness pass"

    if cid == "a8_docker_socket_absent":
        if get("mounts"):
            return False, f"mounts: {get('mounts')}"
        return True, "no docker.sock"

    if cid == "a8_mihomo_unreachable":
        if get("reachable_from_host_loopback"):
            return False, "mihomo admin reachable"
        return True, "not published"

    if cid == "a8_client_same_release":
        if not get("host_version") or get("host_version") != get("os_version"):
            return False, "release mismatch"
        return True, "same release"

    if cid == "a8_version_negotiation":
        if not get("backend_version"):
            return False, "no backend version"
        return True, "version present"

    if cid == "a8_probe_domain_all":
        if get("exit") != 0 or get("n_results", 0) < 2:
            return False, "multiple probe results required"
        if not get("has_direct") or not get("has_stages"):
            return False, "missing direct or stages"
        errors = get("errors", [])
        if len(errors) != get("n_results") or all(e == "proxy_failure" for e in errors):
            return False, "missing errors or proxy infrastructure failure"
        return True, "probe ok"

    if cid == "a8_benchmark_bounded":
        if get("exit") != 0:
            return False, "exit!=0"
        if get("duration_s", 999) > 120:
            return False, "not bounded"
        results = get("results", [])
        if (len(results) != 1 or not results[0].get("ok")
                or not 0 < results[0].get("bytes", 0) <= 1024 * 1024):
            return False, "download did not complete within 1M"
        return True, "bounded"

    if cid == "a8_best_domain":
        if get("exit") != 0 or not get("n_routes"):
            return False, "no routes"
        return True, "best ok"

    return False, f"no re-derivation rule for {cid}"


def rederive(cid, stored, c):
    """Return (expected_status, note). expected is what the evidence supports."""
    if stored == "skip":
        if (c.get("evidence") or {}).get("reason"):
            return "skip", "skip with reason"
        return "fail", "skip without reason"
    if stored == "fail":
        return "fail", "fail accepted as-is"
    # stored == "pass": must be supported by evidence
    ok, note = rederive_pass(cid, c)
    return ("pass" if ok else "fail"), note


def collect_secret_values():
    vals = []

    def add(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                vals.append(v)
        except OSError:
            pass

    for p in (os.path.join("/etc/vibelab-egress", "client.token"),
              os.path.join("/etc/vibelab-egress", "default-proxy.auth")):
        add(p)
    sdir = _secrets_dir()
    if os.path.isdir(sdir):
        for root_, _, files in os.walk(sdir):
            for fn in files:
                add(os.path.join(root_, fn))
    seen, out = set(), []
    for v in vals:
        if len(v) >= 8 and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def verify(path):
    problems = []
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        report = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return {"verdict": "INVALID", "problems": [f"not valid JSON: {e}"]}, raw

    # 1. structure
    for k in REQUIRED_TOP:
        if k not in report:
            problems.append(f"missing top-level field: {k}")
    checks = report.get("checks", [])
    if not isinstance(checks, list) or not checks:
        problems.append("checks missing or empty")
        checks = []
    ids = []
    for c in checks:
        for k in ("id", "spec", "status", "command", "evidence"):
            if k not in c:
                problems.append(f"check missing field {k}: {c.get('id', '?')}")
        if c.get("status") not in VALID_STATUS:
            problems.append(f"invalid status {c.get('status')!r}: {c.get('id')}")
        ids.append(c.get("id"))
    if len(ids) != len(set(ids)):
        problems.append("duplicate check ids")
    summary = report.get("summary", {})
    for key, val in (("passed", "pass"), ("failed", "fail"), ("skipped", "skip")):
        actual = sum(1 for c in checks if c.get("status") == val)
        if summary.get(key) != actual:
            problems.append(f"summary.{key}={summary.get(key)} != actual {actual}")
    if summary.get("total") != len(checks):
        problems.append(f"summary.total={summary.get('total')} != {len(checks)}")

    # 2. tamper hash
    stored_hash = report.get("checks_hash", "")
    computed = canon_hash(checks)
    hash_ok = bool(stored_hash) and stored_hash == computed
    if not hash_ok:
        problems.append(f"checks_hash mismatch (stored={stored_hash[:16]}.. "
                        f"computed={computed[:16]}..): evidence/command was edited")

    # 3. re-derivation
    discrepancies = []
    verified_pass = 0
    for c in checks:
        cid = c.get("id")
        stored = c.get("status")
        expected, note = rederive(cid, stored, c)
        if stored == "pass" and expected == "pass":
            verified_pass += 1
        if expected != stored:
            discrepancies.append({"id": cid, "stored": stored,
                                  "rederived": expected, "note": note})

    # 4. secret scan (real when secrets are readable on this host)
    secrets = collect_secret_values()
    secret_scan = "skipped_no_secrets_on_verifier_host"
    leaks = []
    if secrets:
        secret_scan = "scanned"
        for s in secrets:
            if s in raw:
                leaks.append("secret value present in report")
                break
    if leaks:
        problems.append("SECRET LEAK: report contains a secret value")

    passed = sum(1 for c in checks if c.get("status") == "pass")
    failed = sum(1 for c in checks if c.get("status") == "fail")
    skipped = sum(1 for c in checks if c.get("status") == "skip")

    verdict = "VERIFIED"
    if problems:
        verdict = "REJECTED"
    elif discrepancies:
        verdict = "REJECTED"
    elif not hash_ok:
        verdict = "REJECTED"

    out = {
        "verdict": verdict,
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file": path,
        "hash_ok": hash_ok,
        "checks_total": len(checks),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "verified_pass": verified_pass,
        "discrepancies": discrepancies,
        "secret_scan": secret_scan,
        "problems": problems,
        "report_generated_at": report.get("generated_at"),
        "report_host": report.get("host"),
    }
    return out, raw


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verify-live-report.py")
    ap.add_argument("report", help="path to host_acceptance_report.json")
    ap.add_argument("--json", action="store_true", help="print machine-readable result")
    args = ap.parse_args(argv)

    out, _ = verify(args.report)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"verdict: {out['verdict']}")
        print(f"  file: {out['file']}")
        print(f"  tamper hash: {'OK' if out['hash_ok'] else 'MISMATCH'}")
        print(f"  checks: total={out['checks_total']} pass={out['passed']} "
              f"fail={out['failed']} skip={out['skipped']} "
              f"(verified pass={out['verified_pass']})")
        print(f"  secret scan: {out['secret_scan']}")
        if out["discrepancies"]:
            print("  discrepancies (stored status not supported by evidence):")
            for d in out["discrepancies"]:
                print(f"    - {d['id']}: stored={d['stored']} rederived={d['rederived']} "
                      f"({d['note']})")
        if out["problems"]:
            print("  problems:")
            for p in out["problems"]:
                print(f"    - {p}")
    return 0 if out["verdict"] == "VERIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
