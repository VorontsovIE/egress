# Documentation and CLI Handoff

This handoff tracks the documentation audit requested for standalone, international use. Status is maintained as work progresses.

## Scope and acceptance criteria

- User-facing setup and usage instructions match current code and script arguments.
- README is a short entry point; installation, daily usage, and command reference are separate documents.
- CLI help explains implemented behavior, including a successful `egress-run --help`.
- Tracked Russian documentation and code comments are translated into English.
- Documentation does not assume a particular employer, checkout path, or application project. Product names and required Linux/Docker paths remain.
- `TECH_SPEC.md` retains requirements but distinguishes implemented, partial, and planned/deferred behavior.
- Run appropriate help/configuration checks and the existing offline suite; record outcomes here.

## Progress

| Work item | Status | Notes |
|---|---|---|
| README simplification and standalone framing | Complete | Short English overview, quick start, requirements, links. |
| User guide | Complete | Added `USER_GUIDE.md` with setup checks, routes, execution modes, defaults, and personal sources. |
| CLI reference | Complete | Added command summaries and option syntax; corrected behavior for `test` without a route against `egressd` implementation. |
| CLI help parity | Complete | `egress-run --help`, `egressctl test --help`, and `egressctl probe-domain --help` pass and describe actual behavior. |
| Installation guide | Complete | Host, container, provider, proxy-only, and TUN flows reconciled with current scripts. Build-before-provision requirement documented. |
| Development and auxiliary docs | Complete | Development, handoff, acceptance, fixture, install-test, PoC, and report docs are English. |
| Technical specification and status | Complete | Spec has an implementation status map; optional UI/MCP are marked not shipped, deployment checks environment-dependent. `STATUS.md` updated for current documentation work. |
| Other tracked documentation | Complete | No Cyrillic remains in tracked Markdown and code/config text scanned by the audit. |
| Russian code comments | Complete | No Cyrillic remains in tracked Markdown, Python, shell, YAML, TOML, or Go files. |
| Standalone-reference audit | Complete | No employer, fixed checkout directory, or application integration is required. Generic references to checking out this repository describe the normal way to run its scripts. |
| Verification | Complete | CLI help smoke checks, local Markdown link checks, standalone/Cyrillic scans, and `git diff --check` pass. Installed the locked dev dependency set and ran the complete offline suite: 353 passed. |

## Final verification record — 2026-10-29

- Passed: `egress-run --help`, `egressctl test --help`, `egressctl probe-domain --help`, setup/provision/install script help, and `egress-providerctl` help.
- Passed: local Markdown links in the seven primary user/technical docs resolve.
- Passed: `git diff --check`.
- Passed: no Cyrillic remains in the scanned Markdown, Python, shell, YAML, TOML, and Go files; no scanned Markdown contains employer names, fixed checkout paths, or application-specific requirements.
- Passed: `EGRESSD_STATE_DIR=/tmp/vibelab-egress-test-state EGRESSD_METRICS_DB=/tmp/vibelab-egress-test-state/metrics.sqlite EGRESSD_GEO_DIR=/tmp/vibelab-egress-test-state/geo UV_CACHE_DIR=/tmp/uv-cache uv run --locked --project egressd python -m pytest tests/ client/tests/ -q` — 353 passed in 31.42 seconds.
- Dependency setup: `egressd/pyproject.toml` declares runtime dependencies and the `dev` extra (`pytest`, `pytest-asyncio`, `httpx2`); `egressd/uv.lock` pins the resolved environment. `DEVELOPMENT.md` documents install and run commands.
- The first attempts exposed two environment requirements: writable temporary paths for egressd state and `httpx2` for the current Starlette test client. Both are now documented or declared, and the final suite passed.

## Re-audit follow-up — 2026-10-29

- Verified container-side personal-source normalization imports the bundled adapter and requires PyYAML. This optional dependency is now disclosed in README, installation/user docs, and `egressctl source` help; the base client remains standard-library-only.
- Added an explicit proxy-only setup using `--tun-backend none`; it provisions `host-main` before backend installation so the installer's acceptance harness can run with `--skip-client`.
- Corrected the provider file-update example to apply mode `0600` before every file import.
- The earlier standalone scan recorded generic repository checkout references as if they were project-specific. The acceptance criterion is now precise: no fixed checkout path or app integration requirement remains.
- The ignored `egressd/build` copies created during dependency installation contained stale Russian comments; those generated copies were translated to match their English source files.
- Final help and regression recheck after these changes: full suite 353 passed; key CLI help checked; all local Markdown links resolve; final Unicode scan covered 1,200 tracked/generated text files; `git diff --check` passes.
- Added a concise English decision log to `STATUS.md` so it matches the repository guidance in `AGENTS.md` without restoring obsolete historical implementation notes.
- Re-audit caught `egress-run --default` ignoring the per-client stored mode despite the API/spec and CLI claiming it was used. The wrapper now reads the saved mode, explicit `--mode` overrides it, and an integration test covers both paths.
- Final regression run after saved-mode integration: 353 passed in 31.42 seconds. The new focused live-API integration case also passed independently.
