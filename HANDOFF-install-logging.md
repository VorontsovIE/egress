# Archived handoff: installer logging and offline checks

This file records the completed work order for structured installation logs, reliable health checks, a self-explaining acceptance harness, and offline tests. It is retained as project history; for current usage, follow `INSTALL.md`, `DEVELOPMENT.md`, and `tests/install/README.md`.

## Problem addressed

The installer previously lost useful subprocess output, and its health gate could accept a restarting container if one inspection returned stale `healthy` data. The offline installer scenarios were also not committed as a reusable, documented test suite.

## Delivered changes

1. **Structured installation log:** `scripts/install-backend-host.sh` writes UTC timestamped `[step N/10]` messages to stdout and `<root>/install-backend-host.log`. The log begins with a run header containing machine/runtime versions, revision, and arguments. Compose, CIDR sync, config generation, and acceptance output are captured. Failure logs include step, command, exit code, and log path.
2. **Robust health gate:** backend services must remain healthy across consecutive polls with `RestartCount == 0`. On timeout or failure, the installer captures filtered service status and logs while redacting secrets.
3. **Self-explaining host harness:** `host/egress-host-test` reports named checks with pass/fail/skip status, evidence, and next steps. Static checks run without a Docker daemon.
4. **Offline installer suite:** `tests/install/` uses fixed command stubs to cover successful deployment, Compose/build failures, health timeout, stale-health/restarting behavior, root enforcement, static security conditions, and bind verification. See the current test README for commands and stub controls.
5. **Verifier parity:** `scripts/verify-backend-host.sh` checks the same restricted bind policy as installation and reports offending mappings.
6. **Documentation:** installation logging, exit codes, recovery guidance, and current state were documented. The current user instructions are maintained in `INSTALL.md` and `STATUS.md`.

## Security constraints retained

- Keep secrets out of command output, logs, and reports; log paths and short hashes only.
- Do not broaden host tools into arbitrary command execution; use fixed argument vectors and allowlists.
- Do not mount the Docker socket into the egress stack or publish Mihomo's admin API.
- Preserve evidence under `reports/` and PoC artifacts under `poc/`.

## Historical acceptance

The original work order required the full offline suite, install harness, security gate, `bash -n`, and Python compilation checks. Test totals recorded at the time are historical and may differ from the current checkout. Use the commands in `DEVELOPMENT.md` for present-day validation.
