# Live acceptance — host-side run + re-verification

This folder contains the tooling to test the part of the project that **requires
host access** (Docker daemon, the live egress stack, the TUN helper, the
OpenScience/gVisor container) and to hand the results back for **independent
re-verification**.

Two files do the work:

| File | Runs on | Role |
|------|---------|------|
| `host/live-acceptance.py` | the **host** | the *producer*: runs every live check and writes **one** JSON report |
| `scripts/verify-live-report.py` | anywhere (no host access needed) | the *verifier*: re-derives every pass/fail from the report's raw evidence and checks a tamper hash |
| `scripts/run-live-acceptance.sh` | the host | convenience wrapper: detects the OpenScience container, runs the producer, writes the report |

The design goal: the report is **self-contained and tamper-evident**. The
verifier does **not** trust the stored status of a check — it re-derives it from
the evidence, and a `checks_hash` over each check's `{id, spec, command,
evidence}` detects any post-hoc edit. So a buggy or hand-edited report cannot
fake a pass.

## What is covered

- **TECH_SPEC.md section 50 (Phase 2 TUN)** — all 12 items:
  `tun_tcp_client`, `tun_local_workload`, `tun_udp_quic`, `tun_concurrent`,
  `tun_direct_isolated`, `tun_no_root_escalation`, `tun_stale_cleanup`,
  `tun_hard_direct`, `openscience_tun_unsupported`, `tun_no_silent_fallback`,
  `tun_capabilities_accuracy`, `tun_no_gvisor_weakening`.
- **TECH_SPEC.md section 49 (v1 proxy) — host-only live items**:
  `a8_public_ports_closed` (49.32), `a8_openscience_recreate_persists` (49.39),
  `a8_litellm_reachable` (49.29), `a8_parallel_oneshot` (49.23),
  `a8_egress_run_local` (49.25), `a8_internal_hard_direct` (49.27),
  `a8_secrets_absent` (49.34), `a8_host_test_report` (49.41, runs the existing
  `host/egress-host-test`), `a8_docker_socket_absent` (49.33),
  `a8_mihomo_unreachable` (49.30), `a8_client_same_release` (49.7),
  `a8_version_negotiation` (49.42), `a8_probe_domain_all` (49.20),
  `a8_benchmark_bounded` (49.35), `a8_best_domain` (49.36).

Checks that cannot run in the given environment are reported as `skip` with a
reason (never silently dropped, never faked). A check is `pass` only if its raw
evidence supports it.

## Prerequisites (on the host)

1. Docker daemon running; the egress stack is up:
   - `docker compose -f compose.yaml up -d` (always)
   - `docker compose -f compose.yaml -f compose.tun.yaml up -d` (for the TUN checks)
2. The **host** client is installed: `egressctl` and `egress-run` on `PATH`,
   with `/etc/vibelab-egress/client.yaml`, `client.token`, `default-proxy.auth`.
3. The **OpenScience** container is running and has the client installed
   (`/usr/local/bin/egressctl` inside it). Its name is auto-detected; override
   with `VIBELAB_EGRESS_OS_CONTAINER` if detection fails.
4. For the TUN checks: the TUN helper socket is present at the path written
   into the host client's `client.yaml` (`tun_helper_socket:` —
   `install-backend-host.sh` writes `<VIBELAB_EGRESS_ROOT>/tun-helper/tun-helper.sock`;
   `compose.tun.yaml` binds that dir to the helper's
   `/run/vibelab-egress/`). The harness resolves it from the host client
   config automatically; override with `VIBELAB_EGRESS_TUN_HELPER_SOCKET`
   or `--tun-helper-sock`.

## Run it (one command)

```bash
cd /srv/workspace/vibelab-egress
bash scripts/run-live-acceptance.sh
```

This writes the single report to `reports/host_acceptance_report.json`.

To run the **restart** observation (it restarts the OpenScience container — do
this only if you can afford the brief interruption):

```bash
bash scripts/run-live-acceptance.sh --recreate
```

This check reports `skip` for spec 49.39 even when the restart succeeds:
`docker restart` keeps the writable layer and cannot prove that a client
installation survives container removal and recreation.

Optional environment overrides:

```bash
VIBELAB_EGRESS_OS_CONTAINER=openscience \
VIBELAB_EGRESS_LITELLM_URL=http://127.0.0.1:4000/health \
bash scripts/run-live-acceptance.sh
```

### Run the producer directly (advanced)

```bash
python3 host/live-acceptance.py \
  --out reports/host_acceptance_report.json \
  --config /etc/vibelab-egress/client.yaml \
  --os-container openscience \
  --litellm-url http://127.0.0.1:4000/health
  # add --recreate to enable the destructive recreate check
  # add --no-tun to skip the TUN checks
  # add --check a8_secrets_absent,tun_tcp_client to run a subset
```

## Send back

Send **one file**: `reports/host_acceptance_report.json`. That is the entire
deliverable for re-verification. It contains every check's command, exit code,
and (truncated) output, plus a summary and the tamper hash. **No secrets are
written to it** — token values, default-proxy credentials, the mihomo secret and
provider subscription values are read from the known secret locations and
redacted out of every captured output (the redaction count is recorded in the
report).

## Re-verification (what the lead runs on the returned file)

```bash
python3 scripts/verify-live-report.py reports/host_acceptance_report.json
```

Output is a verdict:

- `VERIFIED` — structure is valid, the tamper hash matches, **every stored pass
  is independently supported by its own evidence**, and no secret value is
  present. Exit code 0.
- `REJECTED` — any of the above failed. The output lists the exact
  discrepancies (e.g. `stored=pass rederived=fail (exit_ip not public)`) and
  problems (e.g. `checks_hash mismatch`, `SECRET LEAK`). Exit code 1.

The verifier re-derivation rules are covered by
`tests/test_verify_live_report.py`, which proves it catches a flipped status,
edited evidence, an injected secret, and structural breaks.

## Notes / limits

- The producer uses only the Python 3 standard library and a fixed command
  allowlist (`egressctl`, `egress-run`, `docker`, `ip`, `ss`, `python3`, and the
  sibling `egress-host-test`). There is no free-form shell input.
- The OpenScience checks run **inside** the container via `docker exec`, because
  the bundle's token paths (`/run/secrets/*`) and the gVisor environment only
  exist there.
- `a8_public_ports_closed` checks that the egress client-facing ports
  (1080/1081/9091) are not reachable on a public NIC; unrelated host services
  (e.g. an IDE) are out of scope and reported but not failed.
- `a8_host_test_report` runs the existing `host/egress-host-test` harness and
  folds its result in, so the static host checks (49.15, 49.31, …) are covered
  in the same single report.
