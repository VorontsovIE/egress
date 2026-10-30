#!/usr/bin/env bash
# install-backend-host.sh — deploy the egress backend on a Docker
# host (spec sections 32-A, 35, 36, 37, 38).
#
# Steps (numbered as logged, step N/10):
#   1. prerequisites (docker, compose v2, python3)
#   2. persistent root: create dirs, check filesystem (findmnt/df)
#   3. backend secrets: /etc/egress/secrets/{mihomo_api_secret,
#      devbridge.token, providers/, clients/} (0600, never printed)
#   4. GEO data (optional — mihomo auto-updates at runtime)
#   5. Docker CIDR sync (egress-hostctl) + systemd timer
#   6. firewall: restrict published ports to local sources (best effort,
#      ufw; nftables fallback documented)
#   7. host client provisioning + install (unless --skip-client)
#   8. bootstrap config generation (incl. 8b: secrets readable by uid 1000)
#   9. compose deploy (pinned mihomo image, versioned egressd image from GHCR)
#   10. host acceptance harness (egress-host-test)
#
# Structured logging:
#   Every action is logged as "[<UTC timestamp>] [step N/10] <message>" to
#   stdout AND appended to <root>/install-backend-host.log, which starts
#   with a run header (hostname, kernel, user, docker/compose/python
#   versions, git rev, full argv). Subprocess output that used to be lost
#   (compose pull/up/ps, CIDR sync, bootstrap config, acceptance harness)
#   is teed into the same file. On any unhandled failure an ERR trap logs
#   the failing step, the failing command and the exit code, and points to
#   "full log: <log file>".
#
# Usage:
#   install-backend-host.sh [--root <persistent-root>] [--secrets-dir <dir>] \
#       [--docker-gateway <ip>] [--skip-client] [--skip-firewall] [--yes]
#
# Exit codes: 0 ok, 2 usage, 3 environment error, 4 verification failed.
#
# Test mode:
#   EGRESS_TEST_MODE=1 — only for stubbed tests (tests/install); it
#   bypasses the root check and must not be used for production installs.
set -Eeuo pipefail

ROOT="${EGRESS_ROOT:-/var/lib/egress}"
SECRETS_DIR="${EGRESS_SECRETS:-/etc/egress/secrets}"
DOCKER_GATEWAY="${DOCKER_GATEWAY:-172.17.0.1}"
SKIP_CLIENT=0
SKIP_FIREWALL=0
ASSUME_YES=0
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOSTCTL="$REPO_ROOT/host/egress-hostctl"
# pkexec and service launchers may start us outside the checkout. Compose
# commands below need the project file from this repository.
cd "$REPO_ROOT"
# pkexec gives root a restricted PATH that omits /usr/local/bin, where step 7
# installs egressctl and egress-run. The acceptance harness needs both.
export PATH="$PATH:/usr/local/bin:/usr/local/sbin"

ARGS="$*"

die_usage() { echo "install-backend: $*" >&2; exit 2; }
die_env()   { echo "install-backend: $*" >&2; exit 3; }

# ---------------------------------------------------------------------------
# structured logging
# ---------------------------------------------------------------------------
STEP=0
STEP_NAME=""
LOG_FILE=""

now_utc() { date -u +%FT%TZ; }

log() {
  local line
  line="[$(now_utc)] [step $STEP/10] $*"
  echo "$line" || true
  if [ -n "$LOG_FILE" ]; then
    printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
  fi
  return 0
}

# run_logged <label> <cmd...> — run a required command, tee its combined
# output into the install log, and return the command's exit code
# (PIPESTATUS[0], so a tee failure cannot mask it).
run_logged() {
  local label="$1"
  shift
  log "run: $label"
  local rc=0
  "$@" 2>&1 | tee -a "$LOG_FILE" || rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    log "run: $label FAILED (exit $rc) — full log: $LOG_FILE"
  fi
  return "$rc"
}

on_error() {
  local rc=$?
  local msg
  msg="[$(now_utc)] [step $STEP/10] ERROR: step=$STEP ($STEP_NAME) command='${BASH_COMMAND:-unknown}' exit=$rc"
  echo "$msg" >&2 || true
  echo "install-backend: full log: ${LOG_FILE:-<not created yet>}" >&2 || true
  if [ -n "$LOG_FILE" ]; then
    {
      printf '%s\n' "$msg"
      printf '%s\n' "[$(now_utc)] [step $STEP/10] full log: $LOG_FILE"
    } >> "$LOG_FILE" 2>/dev/null || true
  fi
  # Documented exit code: unexpected failure = environment error (3).
  # The underlying command rc is recorded in $msg above.
  exit 3
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --root)           ROOT="${2:?}"; shift 2 ;;
    --secrets-dir)    SECRETS_DIR="${2:?}"; shift 2 ;;
    --docker-gateway) DOCKER_GATEWAY="${2:?}"; shift 2 ;;
    --skip-client)    SKIP_CLIENT=1; shift ;;
    --skip-firewall)  SKIP_FIREWALL=1; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    -h|--help)        grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

# Host bootstrap operation: root is required. EGRESS_TEST_MODE=1
# bypasses ONLY this check — it is consumed by the offline stubbed test
# suite (tests/install, see tests/install/README.md) and is not a
# production flag.
[ "${EGRESS_TEST_MODE:-0}" = "1" ] || [ "$(id -u)" = "0" ] || \
  die_env "must run as root (host bootstrap operation)"

# Log file is created before step 2 (persistent root) so steps 2+ are
# captured; until then log() prints to stdout only (graceful).
LOG_FILE="$ROOT/install-backend-host.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
: >> "$LOG_FILE" 2>/dev/null || LOG_FILE=""

trap on_error ERR

# ---------------------------------------------------------------------------
# run header
# ---------------------------------------------------------------------------
log "=== egress backend install ==="
log "run header: hostname=$(hostname) kernel=$(uname -r) user=$(id -un)"
log "run header: docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unavailable) compose=$(docker compose version 2>/dev/null || echo unavailable)"
log "run header: python3=$(python3 --version 2>&1) git_rev=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
log "run header: root=$ROOT secrets_dir=$SECRETS_DIR docker_gateway=$DOCKER_GATEWAY skip_client=$SKIP_CLIENT skip_firewall=$SKIP_FIREWALL assume_yes=$ASSUME_YES args='$ARGS'"

confirm() {
  [ "$ASSUME_YES" = "1" ] && return 0
  read -r -p "$1 [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

# ---------------------------------------------------------------------------
# 1. prerequisites (docker, compose v2, python3)
# ---------------------------------------------------------------------------
STEP=1; STEP_NAME="prerequisites"
log "prerequisites"
command -v docker >/dev/null 2>&1 || die_env "docker not found"
command -v python3 >/dev/null 2>&1 || die_env "python3 not found"
docker compose version >/dev/null 2>&1 || die_env "docker compose v2 not available"
[ -f "$REPO_ROOT/compose.yaml" ] || die_env "compose.yaml not found in $REPO_ROOT"
log "prerequisites ok (docker, compose v2, python3, compose.yaml)"
if [ "${EGRESS_TEST_MODE:-0}" = 1 ]; then
  EGRESSD_UID=1000
  EGRESSD_GID=1000
else
  getent group egressd >/dev/null || groupadd --system egressd
  id egressd >/dev/null 2>&1 || \
    useradd --system --gid egressd --no-create-home \
            --shell /usr/sbin/nologin egressd
  EGRESSD_UID="$(id -u egressd)"
  EGRESSD_GID="$(id -g egressd)"
fi
export EGRESSD_UID EGRESSD_GID

# ---------------------------------------------------------------------------
# 2. persistent root + filesystem check (spec 38)
# ---------------------------------------------------------------------------
STEP=2; STEP_NAME="persistent root"
log "persistent root: $ROOT"
if [ -e "$ROOT" ]; then
  log "existing filesystem for $ROOT:"
  findmnt -T "$ROOT" || true
  df -h "$ROOT" || true
else
  if ! confirm "create $ROOT on the current filesystem?"; then
    die_env "aborted: persistent root not created"
  fi
fi
# NOTE: no $ROOT/geo dir — GEO files are mihomo-managed inside $ROOT/mihomo
# (see step 4) and egressd's enrichment store lives in $ROOT/egressd/geo.
for d in mihomo providers generated egressd; do
  mkdir -p "$ROOT/$d"
  chmod 700 "$ROOT/$d"
done
chmod 700 "$ROOT"
# egressd runs as uid 1000 (non-root, egressd/Dockerfile) and writes its
# state/metrics/geo under $ROOT/egressd (mounted at /var/lib/egressd). The
# root:0700 default would make its startup mkdir() fail with PermissionError
# and crash-loop the container, so hand that one dir to the container user.
chown -R "$EGRESSD_UID:$EGRESSD_GID" "$ROOT/egressd" 2>/dev/null || true
chmod 755 "$ROOT/egressd"
# egressd also WRITES the generated mihomo config to $ROOT/generated (mounted
# at /var/lib/egressd/generated; mihomo reads it :ro). It must be owned by
# uid 1000 too, or a runtime regenerate() fails with PermissionError. Keep it
# 0700 (the host acceptance persistent_dirs_permissions check requires every
# non-egressd dir under $ROOT to be 0700); ownership (not the mode) is what
# lets uid 1000 write.
chown -R "$EGRESSD_UID:$EGRESSD_GID" "$ROOT/generated" 2>/dev/null || true
chmod 700 "$ROOT/generated"
chown -R "$EGRESSD_UID:$EGRESSD_GID" "$ROOT/providers" 2>/dev/null || true
chmod 700 "$ROOT/providers"
log "persistent dirs ready (0700; egressd+generated -> service uid $EGRESSD_UID)"
if [ "${EGRESS_TEST_MODE:-0}" != 1 ]; then
  install -d -o root -g root -m 0755 /etc/egress
  EGRESSD_VERSION="$(git -C "$REPO_ROOT" describe --tags --exact-match HEAD 2>/dev/null || true)"
  EGRESSD_VERSION="${EGRESSD_VERSION:-latest}"
  printf 'EGRESSD_UID=%s\nEGRESSD_GID=%s\nEGRESS_ROOT=%s\nEGRESS_SECRETS=%s\nDOCKER_GATEWAY=%s\nEGRESSD_IMAGE=ghcr.io/vorontsovie/egress:%s\n' \
    "$EGRESSD_UID" "$EGRESSD_GID" "$ROOT" "$SECRETS_DIR" "$DOCKER_GATEWAY" "$EGRESSD_VERSION" \
    > /etc/egress/compose.env
  chmod 0644 /etc/egress/compose.env
fi
if [ "${EGRESS_TEST_MODE:-0}" != 1 ]; then
  install -m 0755 "$REPO_ROOT/host/egress-providerctl" /usr/local/sbin/egress-providerctl
  install -m 0755 "$REPO_ROOT/host/egress-upgrade" /usr/sbin/egress-upgrade
fi

# Seed the GEO datasets into the mihomo home dir. Mihomo v1.19.32 treats a
# missing GeoIP.dat that it also cannot download as a FATAL config-parse
# error ("load GeoIP dns fallback filter error"), so on hosts where the
# geox-url download is unreachable (e.g. GitHub blocked) the container
# crash-loops. Seeding the files makes first boot deterministic; mihomo's
# geo-auto-update then refreshes them at runtime when the network allows.
GEO_SEED="$REPO_ROOT/host/geo"
for f in GeoIP.dat GeoSite.dat asn.mmdb; do
  if [ -f "$GEO_SEED/$f" ]; then
    [ -f "$ROOT/mihomo/$f" ] || cp "$GEO_SEED/$f" "$ROOT/mihomo/$f"
  else
    log "  WARNING: GEO seed missing: $GEO_SEED/$f (first boot will fail if the geox-url download is unreachable)"
  fi
done
log "GEO seed: $ROOT/mihomo (GeoIP/GeoSite/ASN present or seeded)"

# ---------------------------------------------------------------------------
# 3. backend secrets (spec 37) — generated once, never printed
# ---------------------------------------------------------------------------
STEP=3; STEP_NAME="backend secrets"
log "backend secrets: $SECRETS_DIR"
mkdir -p "$SECRETS_DIR/providers" "$SECRETS_DIR/clients"
chmod 700 "$SECRETS_DIR" "$SECRETS_DIR/providers" "$SECRETS_DIR/clients"
gen_secret() { # path
  if [ -f "$1" ]; then
    log "  keep existing $(basename "$1")"
  else
    umask 077
    python3 -c "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_urlsafe(32))" "$1"
    chmod 600 "$1"
    log "  generated $(basename "$1")"
  fi
}
gen_secret "$SECRETS_DIR/mihomo_api_secret"
gen_secret "$SECRETS_DIR/devbridge.token"

# ---------------------------------------------------------------------------
# 4. GEO data (spec 14/41) — OPTIONAL, mihomo-managed.
#    We do NOT pre-download GEO here. Mihomo fetches/refreshes GeoIP/GeoSite/
#    ASN into its own persistent home dir at startup via `geox-url` +
#    `geo-auto-update` (set in the generated config, spec 14.1). GEO is routing
#    context, not a hard dependency (spec 14.3): the backend installs and runs
#    even when the GEO download is unavailable; mihomo simply has no GEO data
#    until its auto-update succeeds. (egressd's separate enrichment store at
#    ${ROOT}/egressd/geo is likewise best-effort and tolerates fetch failures.)
# ---------------------------------------------------------------------------
STEP=4; STEP_NAME="GEO data (optional)"
log "GEO data: optional — mihomo auto-updates at runtime (no pre-download)"

# ---------------------------------------------------------------------------
# 5. Docker CIDR sync + timer (spec 35)
# ---------------------------------------------------------------------------
STEP=5; STEP_NAME="Docker CIDR sync"
log "Docker CIDR sync"
export EGRESS_ROOT="$ROOT"
run_logged "egress-hostctl sync-docker-cidrs" \
  python3 "$HOSTCTL" sync-docker-cidrs \
  || die_env "CIDR sync failed (full log: $LOG_FILE)"
if command -v systemctl >/dev/null; then
  install -m 644 "$REPO_ROOT/host/units/egress-cidr-sync.service" \
                 /etc/systemd/system/ 2>/dev/null || true
  install -m 644 "$REPO_ROOT/host/units/egress-cidr-sync.timer" \
                 /etc/systemd/system/ 2>/dev/null || true
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now egress-cidr-sync.timer 2>/dev/null || \
    log "  note: could not enable cidr-sync timer (run: systemctl enable --now egress-cidr-sync.timer)"
else
  log "  note: systemctl not available; run CIDR sync manually after Docker restarts"
fi

# ---------------------------------------------------------------------------
# 6. firewall (spec 36) — best effort with ufw
# ---------------------------------------------------------------------------
STEP=6; STEP_NAME="firewall"
if [ "$SKIP_FIREWALL" = "1" ]; then
  log "firewall: skipped (--skip-firewall)"
elif command -v ufw >/dev/null; then
  log "firewall (ufw): allow egress ports only from local sources"
  ufw allow in on lo 2>/dev/null || true
  ufw allow from "$DOCKER_GATEWAY" to any port 9091,1080:1081 proto tcp 2>/dev/null || true
  # Container clients reach the published ports from their own container
  # IPs, i.e. from the docker subnets — not from the gateway address
  # (spec 36: allow exactly the needed local subnets). The CIDR artifact
  # is written by step 5; networks created after install need a manual
  # `ufw allow from <cidr> to any port 9091,1080:1081 proto tcp`.
  CIDR_ARTIFACT="$ROOT/generated/docker-cidrs.yaml"
  if [ -f "$CIDR_ARTIFACT" ]; then
    for cidr in $(sed -n 's/^[[:space:]]*subnet:[[:space:]]*//p' "$CIDR_ARTIFACT" \
                 | tr -d "'\"" | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' || true); do
      if ufw allow from "$cidr" to any port 9091,1080:1081 proto tcp >/dev/null 2>&1; then
        log "  allow $cidr -> 9091,1080:1081 (container subnet)"
      fi
    done
  else
    log "  WARNING: $CIDR_ARTIFACT missing — container subnets not allowed; container clients will be blocked by ufw"
  fi
  ufw deny 9090 2>/dev/null || true   # mihomo admin: never reachable outside compose
  log "  (verify with: ufw status verbose; adjust for your host policy)"
else
  log "firewall: ufw not found — manually restrict ports 9091/1080/1081 to"
  log "  127.0.0.1 and $DOCKER_GATEWAY, and block 9090 (spec section 36)."
fi

# ---------------------------------------------------------------------------
# 7. host client provisioning + install (must precede bootstrap config: the
#    generated mihomo config embeds the per-client proxy credentials).
# ---------------------------------------------------------------------------
STEP=7; STEP_NAME="host client provisioning"
if [ "$SKIP_CLIENT" = "1" ]; then
  log "host client: skipped (--skip-client)"
else
  log "provisioning host client (host-main)"
  # Invoke helpers via `bash` (read, not exec): direct shebang exec of a
  # script that is still open for write by another process fails with
  # ETXTBSY ("Text file busy") on some filesystems (observed on the 9p
  # workspace mount); `bash <script>` is immune.
  bash "$REPO_ROOT/scripts/provision-client.sh" \
    --client host-main --environment host \
    --secrets-dir "$SECRETS_DIR" \
    --out "$ROOT/client-bundles"
  bash "$REPO_ROOT/scripts/install-client.sh" \
    --client host-main --target host \
    --bundle "$ROOT/client-bundles/host-main"
fi

# ---------------------------------------------------------------------------
# 8. bootstrap config: generate the initial mihomo config from secrets so
#    the stack can start (egressd takes over generation at runtime in A3).
# ---------------------------------------------------------------------------
STEP=8; STEP_NAME="bootstrap config"
log "generating initial mihomo config (bootstrap)"
# A running egressd writes the same shared config. Stop it before bootstrap
# so it cannot replace the startup config while Mihomo is being recreated.
run_logged "docker compose stop egress-egressd egress-mihomo" \
  docker compose stop egress-egressd egress-mihomo \
  || die_env "docker compose stop failed (full log: $LOG_FILE)"
touch "$ROOT/providers/.gitkeep"
run_logged "python3 -m egressd.bootstrap_config" \
  env PYTHONPATH="$REPO_ROOT/egressd/src" python3 -m egressd.bootstrap_config \
  --root "$ROOT" --secrets-dir "$SECRETS_DIR" \
  --oneshot-slots "${EGRESSD_ONESHOT_SLOTS:-8}" \
  || die_env "bootstrap config generation failed (full log: $LOG_FILE)"

# ---------------------------------------------------------------------------
# 8b. make the backend secrets readable by the egressd container user.
#     egressd runs as a NON-root user (uid 1000, see egressd/Dockerfile) and
#     reads the backend client tokens / mihomo secret / provider subscriptions
#     from this dir (mounted read-only into the container). The secrets are
#     generated above as root:0600, which uid 1000 cannot read — that made
#     egressd crash-loop at startup (PermissionError in ClientRegistry.reload).
#     Hand the whole secrets tree to uid 1000 (keeping 0600/0700 perms) so
#     egressd can read it. mihomo runs as root in its container, so it can
#     still read the files regardless of host ownership.
# ---------------------------------------------------------------------------
log "making backend secrets readable by the egressd user (uid $EGRESSD_UID)"
chown -R "$EGRESSD_UID:$EGRESSD_GID" "$SECRETS_DIR" 2>/dev/null || \
  log "  note: could not chown $SECRETS_DIR to service uid (egressd may fail to read secrets)"
if [ "${EGRESS_TEST_MODE:-0}" != 1 ]; then
  chmod 0755 /etc/egress
fi

# ---------------------------------------------------------------------------
# 9. compose deploy
# ---------------------------------------------------------------------------
STEP=9; STEP_NAME="compose deploy"
log "deploying compose stack (mihomo pinned, egressd from GHCR)"
export EGRESS_SECRETS="$SECRETS_DIR"
export DOCKER_GATEWAY
EGRESSD_VERSION="$(git -C "$REPO_ROOT" describe --tags --exact-match HEAD 2>/dev/null || true)"
export EGRESSD_IMAGE="ghcr.io/vorontsovie/egress:${EGRESSD_VERSION:-latest}"
cd "$REPO_ROOT"
# Service key in compose.yaml is the full name (egress-egressd);
# the short name is not a valid compose service reference.
run_logged "docker compose pull egress-egressd" \
  docker compose pull egress-egressd \
  || die_env "docker compose pull failed (full log: $LOG_FILE)"
# The generated config and client credentials are bind-mounted files. Compose
# does not recreate a container when their contents change, so an existing
# mihomo could keep its old controller bind and SOCKS credentials indefinitely.
# Restart both services on install to load the freshly generated inputs.
run_logged "docker compose up -d --force-recreate" docker compose up -d --force-recreate \
  || die_env "docker compose up -d --force-recreate failed (full log: $LOG_FILE)"
if [ "${EGRESS_TEST_MODE:-0}" != 1 ] && command -v systemctl >/dev/null; then
  install -d -o root -g root -m 0755 /usr/local/libexec/egress
  install -o root -g root -m 0755 "$REPO_ROOT/host/egress-enroll" \
    /usr/local/libexec/egress/egress-enroll
  install -o root -g root -m 0644 "$REPO_ROOT/host/client_identity.py" \
    /usr/local/libexec/egress/client_identity.py
  install -o root -g root -m 0644 \
    "$REPO_ROOT/host/units/egress-enroll.service" \
    /etc/systemd/system/egress-enroll.service
  systemctl daemon-reload
  systemctl enable --now egress-enroll.service
  systemctl restart egress-enroll.service
fi
log "waiting for services to become healthy (3 consecutive polls, 2s interval, 90s timeout)"
# Require EVERY service to be healthy, not just any one. The old check
# (grep '"healthy"') broke out as soon as mihomo was healthy, so it reported
# success while egressd was crash-looping. A single healthy/running reading
# of a restarting container can also be STALE (real failure on 2026-10-12:
# compose ps showed "Restarting (1)" yet one inspect read still said
# healthy/running and the install passed the gate). We therefore require
# 3 CONSECUTIVE all-healthy polls before proceeding; the counter resets on
# any failure. On timeout we dump the failing containers' logs (redacted)
# and inspect fields so the crash is visible instead of masked.
SERVICES=(egress-mihomo egress-egressd)

# svc_field <service> <format> — one docker inspect field; empty on error.
svc_field() {
  docker inspect --format "$2" "$1" 2>/dev/null || true
}

all_healthy() {
  local s h st rcnt
  for s in "${SERVICES[@]}"; do
    h="$(svc_field "$s" '{{.State.Health.Status}}')"
    st="$(svc_field "$s" '{{.State.Status}}')"
    rcnt="$(svc_field "$s" '{{.RestartCount}}')"
    [ "$h" = "healthy" ] || return 1
    [ "$st" = "running" ] || return 1
    [ "$rcnt" = "0" ] || return 1
  done
  return 0
}

# redact_stream — replace the VALUES of the known backend secrets on stdin
# with [REDACTED]. Values are urlsafe tokens (no sed metacharacters); empty
# values are skipped. We run as root, so the files are readable.
redact_stream() {
  local f val exprs=""
  local files=()
  if [ -f "$SECRETS_DIR/mihomo_api_secret" ]; then
    files+=("$SECRETS_DIR/mihomo_api_secret")
  fi
  for f in "$SECRETS_DIR/clients/"*.token "$SECRETS_DIR/clients/"*.proxy-creds; do
    if [ -f "$f" ]; then
      files+=("$f")
    fi
  done
  for f in ${files[@]+"${files[@]}"}; do
    val="$(cat "$f" 2>/dev/null)" || continue
    if [ -n "$val" ]; then
      exprs="${exprs}-e s/${val}/[REDACTED]/g "
    fi
  done
  if [ -n "$exprs" ]; then
    # shellcheck disable=SC2086  # exprs is built from urlsafe tokens only
    sed $exprs
  else
    cat
  fi
}

# log_stack_dump — per-service inspect fields + redacted `docker logs --tail
# 100`, written to the install log (indented) and stdout.
log_stack_dump() {
  local s st h rcnt xcode cerr oom out
  for s in "${SERVICES[@]}"; do
    st="$(svc_field "$s" '{{.State.Status}}')"
    h="$(svc_field "$s" '{{.State.Health.Status}}')"
    rcnt="$(svc_field "$s" '{{.RestartCount}}')"
    xcode="$(svc_field "$s" '{{.State.ExitCode}}')"
    cerr="$(svc_field "$s" '{{.State.Error}}')"
    oom="$(svc_field "$s" '{{.State.OOMKilled}}')"
    [ -n "$st" ] || st="unknown"
    [ -n "$h" ] || h="unknown"
    log "  $s: state=$st health=$h RestartCount=$rcnt ExitCode=$xcode OOMKilled=$oom"
    if [ -n "$cerr" ]; then
      log "  $s: Error=$cerr"
    fi
    log "  --- last 100 lines of $s logs (redacted) ---"
    out="$(docker logs --tail 100 "$s" 2>&1 || true)"
    out="$(printf '%s\n' "$out" | redact_stream)"
    if [ -n "$out" ]; then
      printf '%s\n' "$out" | sed 's/^/    /' | tee -a "$LOG_FILE" || true
    else
      log "  (no logs available for $s)"
    fi
  done
}

consecutive=0
healthy=0
for i in $(seq 1 45); do
  if all_healthy; then
    consecutive=$((consecutive + 1))
    if [ "$consecutive" -ge 3 ]; then
      healthy=1
      break
    fi
  else
    consecutive=0
  fi
  sleep 2
done
run_logged "docker compose ps" docker compose ps || true
if [ "$healthy" -ne 1 ]; then
  log "ERROR: not all services became healthy within 90s (3 consecutive polls required)"
  log_stack_dump
  log "ERROR: egress stack not healthy (full log: $LOG_FILE); re-run after fixing the crash"
  die_env "egress stack not healthy (full log: $LOG_FILE); re-run after fixing the crash"
fi

# ---------------------------------------------------------------------------
# 10. acceptance harness
# ---------------------------------------------------------------------------
STEP=10; STEP_NAME="acceptance harness"
log "running host acceptance harness"
# The harness resolves compose.yaml from EGRESS_REPO; point it at the
# repo checkout on this host (its built-in default is the sandbox path).
export EGRESS_REPO="$REPO_ROOT"
if run_logged "egress-host-test (host acceptance harness)" \
     python3 "$REPO_ROOT/host/egress-host-test" --out "$ROOT/acceptance-report.json"; then
  log "acceptance: PASS"
else
  log "acceptance: FAIL — see $ROOT/acceptance-report.json (full harness JSON above in this log)"
  exit 4
fi

log "backend installation complete"
log "  persistent root : $ROOT"
log "  secrets         : $SECRETS_DIR"
log "  compose project : egress (mihomo + egressd)"
log "  next: provision container clients with scripts/provision-client.sh"
