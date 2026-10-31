# Egress lifecycle implementation handoff

## Goal

Deliver a neutral `egress` product namespace, wheel-based client, one-command installer, release upgrade workflow, scoped provider removal, and safe uninstall. Existing installations do not need migration; remove migration-only implementation instead. Do not delete `poc/` or `reports/` evidence.

## Decisions

- `egressctl update` remains provider refresh. Software release update is `egressctl upgrade`.
- `egressctl provider remove --personal <alias>` removes the authenticated client's profile. `sudo egressctl provider remove --managed <id>` removes a backend-managed source. Do not retain old removal aliases.
- Non-root users may run `egressctl upgrade --client-only`; root-owned installs/backend require explicit sudo. No privileged command may execute a user-owned wheel or venv.
- Full upgrade defaults to client, engine, and backend; support `--version`, `--client-only`, and `--backend-only`.
- Ordinary uninstall removes installed software and runtime auth credentials but retains provider subscriptions and persistent data. `--purge` removes those only after confirmation.
- Host personal raw sources are persisted in backend personal metadata; container raw sources stay in the user's XDG state directory. Backend stores normalized snapshots/provider YAML for both.
- `curl | sh` bootstrap downloads and verifies a signed installer before explaining privileged actions and requesting sudo. Without TTY/authorization it aborts before mutation. `--client-only` works without root.
- No deployed-install migration or compatibility aliases. Rename paths/packages/services to `egress`; remove past migration code while retaining current username/UID fallback and PoC/report evidence.

## Stages and status

Update both this table and `STATUS.md` after each stage. Statuses: Pending, In progress, Review, Complete. Record the phase commit SHA only after acceptance checks and critic review pass.

| Stage | Deliverable | Developer | Test author | Critic | Status | Commit |
|---|---|---|---|---|---|---|
| 1 | Remove identity/legacy migration paths; neutralize names and docs | Implement | Add regression checks | Review deletions and scope | Complete | `a700592` |
| 2 | Client wheel, managed system/user installs, separated paths | Implement | Isolated wheel/install tests | Review privilege boundary | Complete | `d726141` |
| 3 | Signed versioned wheel/engine/deployment assets and CI | Implement | Build/manifest/checksum tests | Review supply-chain flow | Complete | `aa6f81a` |
| 4 | Upgrade command with staging, permissions, health checks and rollback | Implement | Mock release/Docker tests | Review root execution | Complete | `257d044` |
| 5 | Personal/managed provider removal and scoped uninstall/purge | Implement | Data-retention/destructive tests | Review deletion boundaries | Complete | `2f6598e` |
| 6 | `curl | sh` full/client-only installer and sudo explanation | Implement | Bootstrap/no-TTY/signature tests | Review before escalation | Complete | `2c3b333` |
| 7 | End-to-end acceptance and docs/status closure | Integrate | Run offline acceptance; live host pending | Final review | Review | `780abbd` |

## Acceptance gates

1. Each stage passes its focused checks, receives critic review, updates this table and `STATUS.md`, then lands as a scoped commit.
2. Wheel installs without checkout and includes all client runtime imports; TUN engine selection and checksum verification work on amd64/arm64.
3. Ordinary uninstall preserves managed and personal source inputs, normalized provider data, and persistent state. Client-only uninstall never removes shared backend configuration/secrets. Purge removes these only after explicit confirmation.
4. Signed assets are verified before sudo. Managed delete/full upgrade/uninstall use root-owned fixed entry points and fixed argv; never execute user-writable Python code as root.
5. Offline acceptance covers wheel packaging, both provider deletion scopes, ordinary uninstall, purge, installer sudo flow, and updater failure recovery. On 2026-10-31, the local offline suite passed (376 tests), security suite passed (50 tests), and install suite passed (7 pytest checks / 76 scenario checks), including an isolated root-container run. A disposable Ubuntu 24.04 QEMU/KVM host was provisioned from a cloud image whose signed checksum verified. The full backend install completed with both services healthy; live host acceptance passed (20 checks, 0 failed, 2 skipped), `verify-backend-host.sh` passed, and `egressctl status` reached the backend. The report is preserved at `reports/host_acceptance_2026-10-31_qemu.json`. Default uninstall was exercised in the VM and retained both provider input sentinels and persistent backend state. Live testing exposed and verified a fix for the host harness's assumption that the client package lived directly under its install prefix instead of the versioned wheel venv. The latest GitHub Actions run still fails in the offline install harness; failed harness check IDs/details are now printed for the next CI diagnosis. Real provider-route acceptance still requires configured provider credentials.

## Agent coordination

- Developer owns implementation and stage commits.
- Test author owns focused automated tests and reports exact commands/results.
- Critic performs read-only review of each stage's interfaces, data loss risks, signatures, sudo flow, and rollback before the stage is marked Complete.
- Root integrator resolves findings, updates the handoff and `STATUS.md`, and records the commit SHA.

## Resolved implementation issue

Client-only removal now deletes only client config, credentials, binaries, and the client installation root. The shared `/etc/egress` backend configuration and provider secrets remain intact.
