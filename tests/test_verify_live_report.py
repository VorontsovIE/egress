"""Tests for scripts/verify-live-report.py.

The verifier's whole purpose is that a buggy or hand-edited live report cannot
fake a pass. These tests build synthetic reports and prove the verifier:
  * accepts a genuine, self-consistent report (VERIFIED);
  * rejects a report where a stored "pass" is not supported by its own
    evidence (re-derivation), even when the tamper hash still matches;
  * rejects a report whose evidence was edited after the hash was computed
    (tamper hash);
  * rejects a report containing a secret value (secret scan);
  * rejects structurally broken reports (missing fields / bad summary).

Run:  python -m pytest tests/test_verify_live_report.py -q
"""
import copy
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VERIFIER = os.path.join(REPO, "scripts", "verify-live-report.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verifier = _load("verify_live_report", VERIFIER)


def make_check(cid, spec, status, command, evidence):
    return {"id": cid, "spec": spec, "status": status,
            "command": command, "evidence": evidence, "detail": ""}


def make_report(checks):
    report = {
        "report": "vibelab-egress live acceptance",
        "generated_at": "2026-01-01T00:00:00Z",
        "host": "testhost",
        "checks": checks,
        "summary": {
            "passed": sum(1 for c in checks if c["status"] == "pass"),
            "failed": sum(1 for c in checks if c["status"] == "fail"),
            "skipped": sum(1 for c in checks if c["status"] == "skip"),
            "total": len(checks),
        },
    }
    report["checks_hash"] = verifier.canon_hash(checks)
    return report


def write(tmp_path, report):
    p = tmp_path / "report.json"
    p.write_text(json.dumps(report))
    return str(p)


# ---------------------------------------------------------------------------
# a genuine report: every stored status is supported by its evidence
# ---------------------------------------------------------------------------
def genuine_checks():
    return [
        make_check("a8_mihomo_unreachable", "spec49.30", "pass", "conn",
                   {"port": 9090, "reachable_from_host_loopback": False}),
        make_check("a8_docker_socket_absent", "spec49.33", "pass", "inspect",
                   {"mounts": []}),
        make_check("a8_secrets_absent", "spec49.34", "pass", "scan",
                   {"scanned": ["repo"], "leaks": [], "leak_count": 0}),
        make_check("a8_public_ports_closed", "spec49.32", "pass", "ss",
                   {"binds": [{"ip": "127.0.0.1", "port": 1080}],
                    "public_binds": ["0.0.0.0:14096"],
                    "egress_ports_on_public": []}),
        make_check("tun_no_silent_fallback", "spec50.11", "pass", "egress-run",
                   {"exit": 6, "stdout": "", "stderr": "no silent fallback"}),
        make_check("a8_litellm_reachable", "spec49.29", "skip", "GET",
                   {"reason": "litellm not reachable", "url": "http://x",
                    "pre": False, "post": False}),
    ]


def test_genuine_report_verified(tmp_path):
    report = make_report(genuine_checks())
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "VERIFIED", out
    assert out["hash_ok"] is True
    assert out["discrepancies"] == []
    assert out["verified_pass"] == 5  # the five stored passes


# ---------------------------------------------------------------------------
# tamper 1: flip a stored "fail" to "pass"; evidence unchanged so the hash
# still matches, but re-derivation must catch the lie.
# ---------------------------------------------------------------------------
def test_flipped_status_rejected(tmp_path):
    checks = genuine_checks()
    # add a check whose evidence supports FAIL, stored as fail (consistent)
    checks.append(make_check("a8_mihomo_unreachable2", "spec49.30", "fail", "conn",
                             {"port": 9090, "reachable_from_host_loopback": True}))
    report = make_report(checks)
    # tamper: claim it passed, recompute hash over unchanged evidence
    for c in report["checks"]:
        if c["id"] == "a8_mihomo_unreachable2":
            c["status"] = "pass"
    report["checks_hash"] = verifier.canon_hash(report["checks"])
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "REJECTED", out
    assert out["hash_ok"] is True  # hash layer did NOT catch it...
    assert any(d["id"] == "a8_mihomo_unreachable2" for d in out["discrepancies"])
    # ...but re-derivation did.


# ---------------------------------------------------------------------------
# tamper 2: edit a pass check's evidence so it no longer supports pass,
# without updating the hash -> tamper hash mismatch.
# ---------------------------------------------------------------------------
def test_edited_evidence_rejected_by_hash(tmp_path):
    report = make_report(genuine_checks())
    for c in report["checks"]:
        if c["id"] == "a8_mihomo_unreachable":
            c["evidence"]["reachable_from_host_loopback"] = True  # now supports fail
    # hash NOT recomputed -> mismatch
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "REJECTED", out
    assert out["hash_ok"] is False


# ---------------------------------------------------------------------------
# tamper 3: a secret value appears in the report -> secret scan rejects.
# ---------------------------------------------------------------------------
def test_secret_injection_rejected(tmp_path, monkeypatch):
    report = make_report(genuine_checks())
    # plant a fake secret file the verifier will read
    fake = tmp_path / "secrets" / "mihomo_api_secret"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("supersecretvalue123456\n")
    monkeypatch.setenv("VIBELAB_EGRESS_SECRETS", str(tmp_path / "secrets"))
    # leak it into a check's evidence
    for c in report["checks"]:
        if c["id"] == "a8_secrets_absent":
            c["evidence"]["leaks"] = ["repo:x contains supersecretvalue123456"]
    out, raw = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "REJECTED", out
    assert any("SECRET LEAK" in p for p in out["problems"])


# ---------------------------------------------------------------------------
# structure: missing top-level field / bad summary count -> rejected.
# ---------------------------------------------------------------------------
def test_missing_field_rejected(tmp_path):
    report = make_report(genuine_checks())
    del report["checks_hash"]
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "REJECTED", out
    assert any("checks_hash" in p for p in out["problems"])


def test_bad_summary_rejected(tmp_path):
    report = make_report(genuine_checks())
    report["summary"]["passed"] = 99
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "REJECTED", out
    assert any("summary.passed" in p for p in out["problems"])


def test_skip_without_reason_rejected(tmp_path):
    checks = [make_check("a8_litellm_reachable", "spec49.29", "skip", "GET",
                         {"url": "http://x"})]  # no reason
    report = make_report(checks)
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "REJECTED", out
    assert any(d["id"] == "a8_litellm_reachable" for d in out["discrepancies"])


# ---------------------------------------------------------------------------
# a pass whose evidence genuinely supports it is verified (positive control
# for the re-derivation rules across several check types).
# ---------------------------------------------------------------------------
def test_pass_rules_supported(tmp_path):
    checks = [
        make_check("tun_tcp_client", "spec50.1", "pass", "egress-run",
                   {"exit": 0, "exit_ip": "8.8.8.8",
                    "expected_country": "US", "exit_country": "US"}),
        make_check("tun_direct_isolated", "spec50.5", "pass", "egress-run",
                   {"exit": 0, "exit_ip": "1.1.1.1",
                    "host_public_ip": "1.1.1.1", "tun_device_present": False}),
        make_check("openscience_tun_unsupported", "spec50.9", "pass", "egress-run",
                   {"exit": 6, "stdout": "", "stderr": "no silent fallback",
                    "runtime": "runsc"}),
        make_check("a8_probe_domain_all", "spec49.20", "pass", "probe-domain",
                   {"exit": 0, "n_results": 3, "has_direct": True,
                    "has_stages": True, "errors": [None, None, "timeout"]}),
    ]
    report = make_report(checks)
    out, _ = verifier.verify(write(tmp_path, report))
    assert out["verdict"] == "VERIFIED", out
    assert out["verified_pass"] == 4


def test_hard_direct_requires_successful_connect_ex(tmp_path):
    check = make_check("a8_internal_hard_direct", "spec49.27", "pass",
                       "egress-run direct", {"localhost_ip": "127.0.0.1",
                       "docker_gateway_reachable": True,
                       "docker_gateway_connect_errno": "111", "dg_exit": 0})
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "REJECTED"
    check["evidence"]["docker_gateway_connect_errno"] = "0"
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "VERIFIED"


def test_restart_cannot_prove_recreate_persistence(tmp_path):
    check = make_check("a8_openscience_recreate_persists", "spec49.39", "pass",
                       "docker restart", {"before_client_id": "openscience-main",
                       "after_client_id": "openscience-main", "restart_rc": 0})
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "REJECTED"


def test_runc_does_not_verify_gvisor_unsupported(tmp_path):
    check = make_check("openscience_tun_unsupported", "spec50.9", "pass",
                       "egress-run", {"exit": 6, "stdout": "",
                       "stderr": "no silent fallback", "runtime": "runc"})
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "REJECTED"


def test_tun_capabilities_must_match_helper(tmp_path):
    check = make_check("tun_capabilities_accuracy", "spec50.12", "pass",
                       "egressctl capabilities", {
                           "host": {"exit": 0, "tun": "supported"},
                           "helper": {"status": "unsupported_in_environment",
                                      "reason": "missing CAP_SYS_ADMIN"},
                           "openscience": {"exit": 0,
                                           "tun": "unsupported_in_environment"}})
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "REJECTED"
    check["evidence"]["host"]["tun"] = "unsupported_in_environment"
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "VERIFIED"


def test_tun_concurrent_requires_three_distinct_routes(tmp_path):
    check = make_check("tun_concurrent", "spec50.4", "pass", "egress-run", {
        "host_public_ip": "8.8.8.8",
        "jobs": [{"route": "direct", "exit": 0, "exit_ip": "8.8.8.8"}]})
    out, _ = verifier.verify(write(tmp_path, make_report([check])))
    assert out["verdict"] == "REJECTED"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
