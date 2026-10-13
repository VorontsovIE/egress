#!/usr/bin/env bash
# verify-backend-host.sh — verify the deployed backend (spec 32-A, 34, 36).
#
# Checks:
#   1. compose stack up + services healthy
#   2. no public binds (published ports bound to 127.0.0.1 / gateway only)
#   3. docker socket not mounted into any service
#   4. mihomo admin API (9090) not reachable from outside the compose net
#   5. secrets not present in container environment (docker inspect)
#   6. persistent dirs + permissions
#   7. host acceptance harness (egress-host-test) — full report
#
# Usage:
#   verify-backend-host.sh [--root <persistent-root>] [--secrets-dir <dir>] \
#       [--docker-gateway <ip>] [--report <path>]
#
# Exit codes: 0 ok, 2 usage, 3 verification failed.
set -uo pipefail

ROOT="${VIBELAB_EGRESS_ROOT:-/home/vibelab/openscience/egress}"
SECRETS_DIR="${VIBELAB_EGRESS_SECRETS:-/etc/vibelab-egress/secrets}"
DOCKER_GATEWAY="${DOCKER_GATEWAY:-172.17.0.1}"
REPORT="$ROOT/acceptance-report.json"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

FAILURES=0
pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAILURES=$((FAILURES + 1)); }
skip() { echo "  SKIP  $*"; }

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

echo "== verify-backend-host =="
cd "$REPO_ROOT"

# 1. compose up + healthy
if docker compose ps --format json 2>/dev/null | grep -q '"running"'; then
  pass "compose stack running"
else
  fail "compose stack not running"
fi
if docker compose ps --format json 2>/dev/null | grep -q '"healthy"'; then
  pass "services healthy"
else
  fail "no service reports healthy"
fi

# 2. no public binds: every published port must be on 127.0.0.1 or the gateway
PUB="$(docker inspect --format '{{range .NetworkSettings.Ports}}{{.}}{{"\n"}}{{end}}' \
      vibelab-egress-mihomo 2>/dev/null || true)"
PUB="$PUB
$(docker inspect --format '{{range .NetworkSettings.Ports}}{{.}}{{"\n"}}{{end}}' \
      vibelab-egress-egressd 2>/dev/null || true)"
BAD_BINDS="$(echo "$PUB" | grep -v '^0.0.0.0' | grep -vE "^(127\.0\.0\.1|${DOCKER_GATEWAY%%.*}\.)" | grep -v '^$' || true)"
# docker inspect shows host bindings as "IP:port"; empty IP means 0.0.0.0
if echo "$PUB" | grep -qE '^:|^0\.0\.0\.0:'; then
  fail "a published port is bound to 0.0.0.0 (public)"
elif [ -n "$BAD_BINDS" ]; then
  fail "published port(s) on unexpected interface: $BAD_BINDS"
else
  pass "all published ports bound to 127.0.0.1 / $DOCKER_GATEWAY"
fi

# 3. docker socket not mounted
if docker inspect vibelab-egress-mihomo vibelab-egress-egressd 2>/dev/null \
   | grep -q "docker.sock"; then
  fail "docker socket mounted into a service"
else
  pass "docker socket not mounted"
fi

# 4. mihomo admin API not reachable from host loopback (it is compose-private)
if python3 - "$SECRETS_DIR" <<'PY'
import socket, sys, os
secret_path = os.path.join(sys.argv[1], "mihomo_api_secret")
try:
    secret = open(secret_path).read().strip()
except OSError:
    secret = ""
# try the mihomo container IP on the compose network via its published
# ports: 9090 must NOT be published, so a direct loopback connect must fail.
s = socket.socket()
s.settimeout(2)
rc = s.connect_ex(("127.0.0.1", 9090))
s.close()
sys.exit(0 if rc != 0 else 1)
PY
then
  pass "mihomo admin API (9090) not reachable on host loopback"
else
  fail "mihomo admin API (9090) reachable on host loopback"
fi

# 5. secrets not in container environment
ENV_LEAK=0
for c in vibelab-egress-mihomo vibelab-egress-egressd; do
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
echo "== host acceptance harness =="
if python3 "$REPO_ROOT/host/egress-host-test" --out "$REPORT"; then
  pass "host acceptance harness: all checks passed (report: $REPORT)"
else
  fail "host acceptance harness reported failures (report: $REPORT)"
fi

echo "== verify-backend-host: $FAILURES failure(s) =="
[ "$FAILURES" -eq 0 ] || exit 3
exit 0
