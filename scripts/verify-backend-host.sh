#!/usr/bin/env bash
# verify-backend-host.sh — verify the deployed backend (spec 32-A, 34, 36).
#
# Checks (7 sections, semantics unchanged):
#   1. compose stack up + services healthy (per service: running + healthy
#      + RestartCount == 0)
#   2. no public binds (published ports bound to 127.0.0.1 / gateway only)
#   3. docker socket not mounted into any service
#   4. mihomo admin API (9090) not reachable from outside the compose net
#   5. secrets not present in container environment (docker inspect)
#   6. persistent dirs + permissions
#   7. host acceptance harness (egress-host-test) — full report
#
# Log: the entire output (run header, all pass/fail/skip lines, the
# harness JSON) is duplicated to <root>/verify-backend-host.log (append),
# mirroring scripts/install-backend-host.sh.
#
# Usage:
#   verify-backend-host.sh [--root <persistent-root>] [--secrets-dir <dir>] \
#       [--docker-gateway <ip>] [--report <path>]
#
# Exit codes: 0 ok, 2 usage, 3 verification failed.
set -uo pipefail

ROOT="${EGRESS_ROOT:-/var/lib/egress}"
SECRETS_DIR="${EGRESS_SECRETS:-/etc/egress/secrets}"
DOCKER_GATEWAY="${DOCKER_GATEWAY:-172.17.0.1}"
REPORT="$ROOT/acceptance-report.json"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARGV="$*"

FAILURES=0
LOG_FILE=""

now_utc() { date -u +%FT%TZ; }

# vlog — print a line to stdout AND append it to the verify log (graceful
# stdout-only until the log file is available).
vlog() {
  local line="[$(now_utc)] $*"
  echo "$line" || true
  if [ -n "$LOG_FILE" ]; then
    printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
  fi
  return 0
}

pass() { vlog "PASS  $*"; }
fail() { vlog "FAIL  $*"; FAILURES=$((FAILURES + 1)); }
skip() { vlog "SKIP  $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --root)           ROOT="${2:?}"; shift 2 ;;
    --secrets-dir)    SECRETS_DIR="${2:?}"; shift 2 ;;
    --docker-gateway) DOCKER_GATEWAY="${2:?}"; shift 2 ;;
    --report)         REPORT="${2:?}"; shift 2 ;;
    -h|--help)        grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify-backend: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Log file (append): created as soon as the root dir exists; if it cannot
# be created the script still runs, stdout-only.
LOG_FILE="$ROOT/verify-backend-host.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
: >> "$LOG_FILE" 2>/dev/null || LOG_FILE=""

# run header (same fields as the install log)
vlog "== verify-backend-host =="
vlog "run header: hostname=$(hostname) kernel=$(uname -r) user=$(id -un)"
vlog "run header: docker=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unavailable) compose=$(docker compose version 2>/dev/null || echo unavailable)"
vlog "run header: python3=$(python3 --version 2>&1) git_rev=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
vlog "run header: root=$ROOT secrets_dir=$SECRETS_DIR docker_gateway=$DOCKER_GATEWAY report=$REPORT args='$ARGV'"

cd "$REPO_ROOT"

# 1. compose up + healthy (per service)
if docker compose ps --format json 2>/dev/null | grep -q '"running"'; then
  pass "compose stack running"
else
  fail "compose stack not running"
fi
# Per-service health (a single grep '"healthy"' over compose ps used to pass
# as soon as ANY service was healthy, masking a crash-looping sibling).
for c in egress-mihomo egressd; do
  st="$(docker inspect --format '{{.State.Status}}' "$c" 2>/dev/null)"
  h="$(docker inspect --format '{{.State.Health.Status}}' "$c" 2>/dev/null)"
  rcnt="$(docker inspect --format '{{.RestartCount}}' "$c" 2>/dev/null)"
  if [ "$st" = "running" ] && [ "$h" = "healthy" ] && [ "$rcnt" = "0" ]; then
    pass "$c: running + healthy + RestartCount=0"
  else
    fail "$c: state=${st:-unknown} health=${h:-unknown} RestartCount=${rcnt:-unknown} (expected running/healthy/0)"
  fi
done

# 2. no public binds: every published port must be on 127.0.0.1 or the
#    docker bridge gateway. `NetworkSettings.Ports` renders one line per
#    published container port (the map VALUE — no port key):
#      [{127.0.0.1 1080} {172.17.0.1 1080}]
# The host bindings are the dotted quads inside the brackets; a line with
# no host address (0.0.0.0 or :: publish) means "all interfaces" -> fail.
PUB="$(docker inspect --format '{{range .NetworkSettings.Ports}}{{.}}{{"\n"}}{{end}}' \
      egress-mihomo 2>/dev/null || true)"
PUB="$PUB
$(docker inspect --format '{{range .NetworkSettings.Ports}}{{.}}{{"\n"}}{{end}}' \
      egressd 2>/dev/null || true)"
BAD_BINDS=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  ips="$(printf '%s\n' "$line" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' || true)"
  if [ -z "$ips" ]; then
    BAD_BINDS="$BAD_BINDS$line
"
    continue
  fi
  while IFS= read -r ip; do
    if [ "$ip" != "127.0.0.1" ] && [ "$ip" != "$DOCKER_GATEWAY" ]; then
      BAD_BINDS="$BAD_BINDS$line
"
      break
    fi
  done <<< "$ips"
done <<< "$PUB"
if [ -n "$BAD_BINDS" ]; then
  fail "published port(s) on unexpected interface (expected 127.0.0.1 or $DOCKER_GATEWAY): $(printf '%s' "$BAD_BINDS" | grep -v '^$' | paste -sd';' -)"
else
  pass "all published ports bound to 127.0.0.1 / $DOCKER_GATEWAY"
fi

# 3. docker socket not mounted
if docker inspect egress-mihomo egressd 2>/dev/null \
   | grep -q "docker.sock"; then
  fail "docker socket mounted into a service"
else
  pass "docker socket not mounted"
fi

# 4. mihomo admin API not reachable from host loopback (it is compose-private)
#    9090 is not published, so a direct loopback connect must fail.
#    Exit 0: not reachable (pass); 1: reachable (fail); 2: probe impossible
#    (sandbox forbids socket() -> skip; only restricted test environments).
if python3 - <<'PY'
import socket, sys
try:
    s = socket.socket()
    s.settimeout(2)
    rc = s.connect_ex(("127.0.0.1", 9090))
    s.close()
except OSError:
    sys.exit(2)
sys.exit(0 if rc != 0 else 1)
PY
then
  pass "mihomo admin API (9090) not reachable on host loopback"
else
  probe_rc=$?
  if [ "$probe_rc" -eq 2 ]; then
    skip "mihomo admin API probe (9090): socket() not permitted in this environment"
  else
    fail "mihomo admin API (9090) reachable on host loopback"
  fi
fi

# 5. secrets not in container environment
ENV_LEAK=0
for c in egress-mihomo egressd; do
  ENVV="$(docker inspect --format '{{range .Config.Env}}{{.}}{{"\n"}}{{end}}' "$c" 2>/dev/null || true)"
  if [ -f "$SECRETS_DIR/mihomo_api_secret" ]; then
    S="$(cat "$SECRETS_DIR/mihomo_api_secret")"
    if echo "$ENVV" | grep -qF "$S"; then
      fail "mihomo secret present in $c environment"
      ENV_LEAK=1
    fi
  fi
done
[ "$ENV_LEAK" = "0" ] && pass "backend secrets not present in container environments"

# 6. persistent dirs
if [ -d "$ROOT" ]; then
  PERMS="$(stat -c '%a' "$ROOT")"
  if [ "$PERMS" = "700" ]; then
    pass "persistent root $ROOT is 0700"
  else
    fail "persistent root $ROOT perms=$PERMS (expected 700)"
  fi
  # NOTE: no $ROOT/geo dir — GEO is mihomo-managed inside $ROOT/mihomo and
  # egressd's enrichment store lives in $ROOT/egressd/geo (GEO is optional).
  for d in mihomo providers generated egressd; do
    [ -d "$ROOT/$d" ] && pass "dir $ROOT/$d" || fail "dir $ROOT/$d missing"
  done
else
  fail "persistent root $ROOT missing"
fi

# 7. host acceptance harness
vlog "== host acceptance harness =="
if [ -n "$LOG_FILE" ]; then
  python3 "$REPO_ROOT/host/egress-host-test" --out "$REPORT" 2>&1 | tee -a "$LOG_FILE"
  harness_rc=${PIPESTATUS[0]}
else
  python3 "$REPO_ROOT/host/egress-host-test" --out "$REPORT"
  harness_rc=$?
fi
if [ "$harness_rc" -eq 0 ]; then
  pass "host acceptance harness: all checks passed (report: $REPORT)"
else
  fail "host acceptance harness reported failures (report: $REPORT)"
fi

vlog "== verify-backend-host: $FAILURES failure(s) =="
[ "$FAILURES" -eq 0 ] || exit 3
exit 0
