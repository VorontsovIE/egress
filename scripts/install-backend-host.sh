#!/usr/bin/env bash
# install-backend-host.sh — deploy the vibelab-egress backend on a Docker
# host (spec sections 32-A, 35, 36, 37, 38).
#
# Steps:
#   1. prerequisites (docker, compose v2, python3)
#   2. persistent root: create dirs, check filesystem (findmnt/df)
#   3. backend secrets: /etc/vibelab-egress/secrets/{mihomo_api_secret,
#      devbridge.token, providers/, clients/} (0600, never printed)
#   4. (GEO data is optional and managed by mihomo at runtime — see below)
#   5. Docker CIDR sync (egress-hostctl) + systemd timer
#   6. firewall: restrict published ports to local sources (best effort,
#      ufw; nftables fallback documented)
#   7. compose deploy (pinned mihomo image, egressd built from repo)
#   8. host client provisioning + install (unless --skip-client)
#   9. host acceptance harness (egress-host-test)
#
# Usage:
#   install-backend-host.sh [--root <persistent-root>] [--secrets-dir <dir>] \
#       [--docker-gateway <ip>] [--skip-client] [--skip-firewall] [--yes]
#
# Exit codes: 0 ok, 2 usage, 3 environment error, 4 verification failed.
set -euo pipefail

ROOT="${VIBELAB_EGRESS_ROOT:-/home/vibelab/openscience/egress}"
SECRETS_DIR="${VIBELAB_EGRESS_SECRETS:-/etc/vibelab-egress/secrets}"
DOCKER_GATEWAY="${DOCKER_GATEWAY:-172.17.0.1}"
SKIP_CLIENT=0
SKIP_FIREWALL=0
ASSUME_YES=0
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOSTCTL="$REPO_ROOT/host/egress-hostctl"

die_usage() { echo "install-backend: $*" >&2; exit 2; }
die_env()   { echo "install-backend: $*" >&2; exit 3; }
say()       { echo "[install-backend] $*"; }

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

[ "$(id -u)" = "0" ] || die_env "must run as root (host bootstrap operation)"
command -v docker >/dev/null || die_env "docker not found"
command -v python3 >/dev/null || die_env "python3 not found"
docker compose version >/dev/null 2>&1 || die_env "docker compose v2 not available"
[ -f "$REPO_ROOT/compose.yaml" ] || die_env "compose.yaml not found in $REPO_ROOT"

confirm() {
  [ "$ASSUME_YES" = "1" ] && return 0
  read -r -p "$1 [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

# ---------------------------------------------------------------------------
# 2. persistent root + filesystem check (spec 38)
# ---------------------------------------------------------------------------
say "persistent root: $ROOT"
if [ -e "$ROOT" ]; then
  say "existing filesystem for $ROOT:"
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
chown 1000:1000 "$ROOT/egressd" 2>/dev/null || true
chmod 755 "$ROOT/egressd"
# egressd also WRITES the generated mihomo config to $ROOT/generated (mounted
# at /var/lib/egressd/generated; mihomo reads it :ro). It must be owned by
# uid 1000 too, or a runtime regenerate() fails with PermissionError. Keep it
# 0700 (the host acceptance persistent_dirs_permissions check requires every
# non-egressd dir under $ROOT to be 0700); ownership (not the mode) is what
# lets uid 1000 write.
chown 1000:1000 "$ROOT/generated" 2>/dev/null || true
chmod 700 "$ROOT/generated"
say "persistent dirs ready (0700; egressd+generated -> uid 1000)"

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
    say "  WARNING: GEO seed missing: $GEO_SEED/$f (first boot will fail if the geox-url download is unreachable)"
  fi
done
say "GEO seed: $ROOT/mihomo (GeoIP/GeoSite/ASN present or seeded)"

# ---------------------------------------------------------------------------
# 3. backend secrets (spec 37) — generated once, never printed
# ---------------------------------------------------------------------------
say "backend secrets: $SECRETS_DIR"
mkdir -p "$SECRETS_DIR/providers" "$SECRETS_DIR/clients"
chmod 700 "$SECRETS_DIR" "$SECRETS_DIR/providers" "$SECRETS_DIR/clients"
gen_secret() { # path
  if [ -f "$1" ]; then
    say "  keep existing $(basename "$1")"
  else
    umask 077
    python3 -c "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_urlsafe(32))" "$1"
    chmod 600 "$1"
    say "  generated $(basename "$1")"
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
say "GEO data: optional — mihomo auto-updates at runtime (no pre-download)"

# ---------------------------------------------------------------------------
# 5. Docker CIDR sync + timer (spec 35)
# ---------------------------------------------------------------------------
say "Docker CIDR sync"
export VIBELAB_EGRESS_ROOT="$ROOT"
python3 "$HOSTCTL" sync-docker-cidrs || die_env "CIDR sync failed"
if command -v systemctl >/dev/null; then
  install -m 644 "$REPO_ROOT/host/units/vibelab-egress-cidr-sync.service" \
                 /etc/systemd/system/ 2>/dev/null || true
  install -m 644 "$REPO_ROOT/host/units/vibelab-egress-cidr-sync.timer" \
                 /etc/systemd/system/ 2>/dev/null || true
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now vibelab-egress-cidr-sync.timer 2>/dev/null || \
    say "  note: could not enable cidr-sync timer (run: systemctl enable --now vibelab-egress-cidr-sync.timer)"
else
  say "  note: systemctl not available; run CIDR sync manually after Docker restarts"
fi

# ---------------------------------------------------------------------------
# 6. firewall (spec 36) — best effort with ufw
# ---------------------------------------------------------------------------
if [ "$SKIP_FIREWALL" = "1" ]; then
  say "firewall: skipped (--skip-firewall)"
elif command -v ufw >/dev/null; then
  say "firewall (ufw): allow egress ports only from local sources"
  ufw allow in on lo 2>/dev/null || true
  ufw allow from "$DOCKER_GATEWAY" to any port 9091,1080:1081 proto tcp 2>/dev/null || true
  ufw deny 9090 2>/dev/null || true   # mihomo admin: never reachable outside compose
  say "  (verify with: ufw status verbose; adjust for your host policy)"
else
  say "firewall: ufw not found — manually restrict ports 9091/1080/1081 to"
  say "  127.0.0.1 and $DOCKER_GATEWAY, and block 9090 (spec section 36)."
fi

# ---------------------------------------------------------------------------
# 7. host client provisioning + install (must precede bootstrap config: the
#    generated mihomo config embeds the per-client proxy credentials).
# ---------------------------------------------------------------------------
if [ "$SKIP_CLIENT" = "1" ]; then
  say "host client: skipped (--skip-client)"
else
  say "provisioning host client (host-main)"
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
say "generating initial mihomo config (bootstrap)"
touch "$ROOT/providers/.gitkeep"
PYTHONPATH="$REPO_ROOT/egressd/src" python3 -m egressd.bootstrap_config \
  --root "$ROOT" --secrets-dir "$SECRETS_DIR" \
  --oneshot-slots "${EGRESSD_ONESHOT_SLOTS:-8}" \
  || die_env "bootstrap config generation failed"

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
say "making backend secrets readable by the egressd user (uid 1000)"
chown -R 1000:1000 "$SECRETS_DIR" 2>/dev/null || \
  say "  note: could not chown $SECRETS_DIR to uid 1000 (egressd may fail to read secrets)"

# ---------------------------------------------------------------------------
# 9. compose deploy
# ---------------------------------------------------------------------------
say "deploying compose stack (mihomo pinned, egressd built from repo)"
export VIBELAB_EGRESS_ROOT="$ROOT"
export VIBELAB_EGRESS_SECRETS="$SECRETS_DIR"
export DOCKER_GATEWAY
cd "$REPO_ROOT"
# Service key in compose.yaml is the full name (vibelab-egress-egressd);
# the short name is not a valid compose service reference.
docker compose build vibelab-egress-egressd
docker compose up -d
say "waiting for services to become healthy"
# Require EVERY service to be healthy, not just any one. The old check
# (grep '"healthy"') broke out as soon as mihomo was healthy, so it reported
# success while egressd was crash-looping. We poll each service's health and
# only proceed when all of them are healthy; on timeout we dump the failing
# container's logs so the crash is visible instead of masked.
SERVICES=(vibelab-egress-mihomo vibelab-egress-egressd)
all_healthy() {
  local s
  for s in "${SERVICES[@]}"; do
    local h
    h="$(docker inspect --format '{{.State.Health.Status}}' "$s" 2>/dev/null)"
    [ "$h" = "healthy" ] || return 1
    # also guard against a container that is restarting/crashed (no health)
    local st
    st="$(docker inspect --format '{{.State.Status}}' "$s" 2>/dev/null)"
    [ "$st" = "running" ] || return 1
  done
  return 0
}
healthy=0
for i in $(seq 1 45); do
  if all_healthy; then healthy=1; break; fi
  sleep 2
done
docker compose ps || true
if [ "$healthy" -ne 1 ]; then
  say "ERROR: not all services became healthy within 90s"
  for s in "${SERVICES[@]}"; do
    h="$(docker inspect --format '{{.State.Health.Status}}' "$s" 2>/dev/null || echo unknown)"
    st="$(docker inspect --format '{{.State.Status}}' "$s" 2>/dev/null || echo unknown)"
    say "  $s: state=$st health=$h"
    if [ "$h" != "healthy" ]; then
      say "  --- last 40 lines of $s logs ---"
      docker logs --tail 40 "$s" 2>&1 | sed 's/^/    /' || true
    fi
  done
  die_env "egress stack not healthy (see logs above); re-run after fixing the crash"
fi

# ---------------------------------------------------------------------------
# 10. acceptance harness
# ---------------------------------------------------------------------------
say "running host acceptance harness"
# The harness resolves compose.yaml from VIBELAB_EGRESS_REPO; point it at the
# repo checkout on this host (its built-in default is the sandbox path).
export VIBELAB_EGRESS_REPO="$REPO_ROOT"
if python3 "$REPO_ROOT/host/egress-host-test" --out "$ROOT/acceptance-report.json"; then
  say "acceptance: PASS"
else
  say "acceptance: see $ROOT/acceptance-report.json"
  exit 4
fi

say "backend installation complete"
say "  persistent root : $ROOT"
say "  secrets         : $SECRETS_DIR"
say "  compose project : vibelab-egress (mihomo + egressd)"
say "  next: provision container clients with scripts/provision-client.sh"
