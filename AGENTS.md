# AGENTS.md — egress

Repository guidance for coding agents and contributors.

## Project overview

egress is a Mihomo-based egress service. One Docker Compose backend (`mihomo` + `egressd`) serves multiple Linux hosts and containers. Install the `egressctl` and `egress-run` client in each execution environment.

- Requirements and architecture: `TECH_SPEC.md`.
- Current progress and decision log: `STATUS.md`; update it after each completed work item.
- Installation: `INSTALL.md`; routine usage: `USER_GUIDE.md`; CLI: `CLI_REFERENCE.md`.
- Development: `DEVELOPMENT.md`; PoC results: `POC_REPORT.md`.

## Repository map

- `egressd/`: FastAPI control plane and backend state.
- `client/`: standard-library Python client package and tests.
- `adapters/`: provider detection, normalization, redaction, and atomic writes.
- `host/`: fixed-operation host tools, acceptance harnesses, and systemd units.
- `scripts/`: backend/client installation, provisioning, verification, and maintenance.
- `mihomo/`: config template and generated/provider mount points.
- `tests/`: offline integration, installation, and security tests.
- `poc/`: PoC sources and run artifacts; do not delete.
- `reports/`: acceptance evidence; do not delete.

The repository is self-contained. Resolve paths relative to the repository root; do not assume a fixed checkout directory or an external project environment.

## Vendor and generated data

PoC binaries and vendor source archives are not tracked. Fetch pinned PoC sources with `sh scripts/fetch-poc-vendor.sh`; the script applies the tracked one-line WireGuard patch. Build instructions are in `DEVELOPMENT.md`.

GEO datasets are not tracked. Fetch them on demand with `scripts/fetch-geo-data.sh`, or let Mihomo download them through its configured GEO URLs.

## Required conventions

1. **Secrets:** Commit only fake credentials and TEST-NET addresses. Real tokens, keys, subscriptions, and backend secrets belong under the protected secrets directory (default `/etc/egress/secrets`, mode 0600); never place them in environment variables, logs, or reports.
2. **Client dependencies:** `client/src/` must remain Python standard-library-only. The pinned static TUN engine is built separately and included in client bundles.
3. **Host command execution:** Host tools must use fixed argument vectors and allowlisted operations. Do not add `shell=True`, `os.system`, arbitrary shell input, or free-form command execution. Security tests enforce these constraints.
4. **Generated config:** Never hand-edit generated Mihomo configuration. Change its source inputs and regenerate through `egressd/configgen.py`.
5. **Compose security:** No public binds, Docker socket mounts, or published Mihomo admin API. Keep the image pinned by tag and digest. Verify with `scripts/verify-backend-host.sh` and `tests/security/`.
6. **Tests:** Install the declared and locked test environment with `uv sync --locked --project egressd --extra dev`, then run `uv run --locked --project egressd python -m pytest tests/ client/tests/ -q`. Real provider-route checks require access to a configured backend.
7. **Progress:** Update `STATUS.md` after each completed step and record decisions or bugs in its log.
8. **Preserve evidence:** Do not remove `poc/` or `reports/`. GEO data stays out of Git.
9. **Language:** Documentation, help, comments, and user-facing text are in English.

## Quick navigation

| Topic | Location |
|---|---|
| Architecture and requirements | `TECH_SPEC.md` |
| Current status | `STATUS.md` |
| Public API | `egressd/src/egressd/main.py` |
| Client CLI | `client/src/egress_client/` |
| Provider adapters | `adapters/` |
| Backend installation | `INSTALL.md`, `scripts/install-backend-host.sh` |
| Acceptance | `host/LIVE_ACCEPTANCE.md`, `host/live-acceptance.py` |
| Security | `tests/security/test_security_gate.py` |
