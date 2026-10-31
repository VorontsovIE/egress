# Project status

This file summarizes the current implementation state. Historical acceptance evidence is retained in `POC_REPORT.md`, `reports/`, and the archived handoff documents. Update this summary after each completed work item.

## Current state — 2026-10-31

## Egress lifecycle work — 2026-10-31

Implementation stages 1–6 are complete and committed separately; see the stage table in [HANDOFF-egress-lifecycle.md](HANDOFF-egress-lifecycle.md). The client is built as a wheel and installed into versioned environments, the release workflow publishes attested versioned assets, `egressctl upgrade` supports client/backend scopes with rollback, provider removal is scoped, and uninstall preserves subscriptions unless `--purge` is confirmed. The `curl | bash` bootstrap supports full install and unprivileged `--client-only` install; it explains privileged actions before prompting through the controlling terminal.

Final offline acceptance passed locally: **376 tests** with temporary egressd state paths; security suite **50 passed**; installation suite **7 passed** (including its 76 scenario checks); `docker compose config --quiet`, Python compilation, shell syntax checks, and `git diff --check` passed. A disposable Ubuntu 24.04 QEMU/KVM host was provisioned from an Ubuntu cloud image after verifying its signed checksum. Backend install started both services healthy; live host acceptance passed **20 checks, 0 failed, 2 skipped**; `verify-backend-host.sh` also passed, and `egressctl status` reached the backend. The report is preserved at [reports/host_acceptance_2026-10-31_qemu.json](reports/host_acceptance_2026-10-31_qemu.json). Default uninstall was exercised on the VM and preserved both subscription inputs and persistent backend state. The live run exposed a harness check that assumed the old source-install client layout; it now imports from the versioned wheel venv, and offline scenarios use an isolated client prefix. GitHub Actions exposed one more fixture issue: it used service UID 1000 while the hosted runner is UID 1001. Test mode now uses the current runner UID/GID, and the 76-scenario install suite passes when reproduced as UID 1001. The CI rerun is pending. Real provider-route acceptance remains unverified.

The backend, client CLI, provider management, route catalog, probes, ranking, client-local TUN path, installation, and security gate are implemented. Phases A0–A8 are complete for work that can be exercised in the development environment. Deployment-specific live acceptance still depends on a configured Linux host, Docker backend, valid client profiles, and real provider routes.

Earlier implementation includes:

- Stable host/container client IDs derived from account names, with UID fallback. Historical installation migration code was removed because deployed-system migration is out of scope.
- Personal route shorthand (`@alias:node`) alongside canonical provider route IDs.
- `egress-run --default` reads the client's saved route and execution mode; an explicit `--mode` overrides the saved mode.
- Client-local TUN using a per-run user/network namespace, a pinned static packet engine, and capability drop before workload launch. TUN fails closed when local prerequisites are missing.
- TUN DNS support for container resolver configurations, including DNS-over-HTTPS through the selected route and UDP/TCP fallback.
- Docker test support for personal routes through client-owned normalized snapshots; the test client does not gain access to the source profile.
- Concurrent TUN checks that keep workloads alive while comparing namespace identities.

## Validation state

- Historical offline suite: 351 tests passed in a Python 3.13 environment; installation harness: 77 passed. See dated entries in the repository history and associated reports for earlier counts.
- Help output for `egressctl test`, `egressctl probe-domain`, and `egress-run` was checked during the documentation refresh. `test` without a route checks catalog routes up to the backend limit.
- The full offline suite passed: 353 tests. Development dependencies are declared in `egressd/pyproject.toml`, pinned in `egressd/uv.lock`, and installed with `uv sync --locked --project egressd --extra dev`. The test run used temporary writable paths for egressd state. CLI help, link, standalone-language, and `git diff --check` validations also passed.
- Live routes, provider availability, and host-specific TUN acceptance must be rechecked after deployment. Historical reports describe their own environment and test date; they are not a guarantee of current provider availability.

## Known limits and next checks

- TUN requires Linux user namespaces, `/dev/net/tun`, `unshare`, `iproute2`, and a matching engine binary. Docker clients need the documented device and seccomp setup.
- Container-side personal-source add/refresh requires PyYAML in the client image for local normalization; other client commands have no third-party Python dependencies.
- Provider-dependent checks require valid source subscriptions and working routes.
- Run `scripts/run-live-acceptance.sh` on the deployment host, then independently verify the report with `scripts/verify-live-report.py`.
- Keep the offline suite passing with `uv sync --locked --project egressd --extra dev`. Verify every CLI option and installation example against the live help output and script behavior when interfaces change.

## Documentation refresh — 2026-10-29

README, installation guide, user guide, CLI reference, development guide, agent guidance, technical specification, PoC report, TUN handoff, personal-subscription report, and auxiliary test/acceptance docs have been translated into English and reviewed for standalone use. Added `egress-run --help`, clarified CLI help, and documented supported options. `TECH_SPEC.md` marks the admin UI and MCP extension as not shipped and deployment-dependent checks accordingly. The documentation handoff is in `HANDOFF-documentation.md`.

## Decision log — 2026-10-29

- Treat this repository as a standalone product. Installation and client configuration must not depend on an employer, a fixed checkout path, or a specific application integration.
- Keep the backend in a separate Docker Compose stack. Install `egressctl` and `egress-run` in each host or container where workloads run; commands always execute locally.
- Keep the core Python client standard-library-only. Container-side personal-source normalization is optional and requires PyYAML; it does not make PyYAML a dependency of other client commands.
- Run TUN locally in a per-invocation user/network namespace. Fail closed when local prerequisites are missing; do not restore the privileged host helper or silently fall back to proxy mode.
- Keep personal source data scoped to the client identity. Container clients upload normalized snapshots, not raw source credentials or access to another client's private profile.
- Use stable account-derived host/container client IDs with UID fallback, and keep migrations rollback-safe.
- Declare backend and development dependencies in `egressd/pyproject.toml`, lock them in `egressd/uv.lock`, and document setup through `DEVELOPMENT.md`.
- Keep Admin UI and MCP as unshipped optional extensions; do not present their requirements as current features.
