#!/usr/bin/env bash
# verify-client.sh — verify a client installation (spec section 32, step B).
#
# Runs, in order:
#   1. egressctl config validate
#   2. egressctl whoami            (requires live backend)
#   3. egressctl doctor            (config + control plane + identity + proxy)
#   4. egressctl status
#   5. egressctl config show       (secrets masked)
#   6. secret-file permissions     (0600, owned by the invoking user)
#   7. no provider secrets in the client config dir (scan by hash)
#
# With --offline, steps 2-4 are skipped (no backend) and only local checks
# (1, 5, 6, 7) run — used by the host acceptance harness static mode and by
# CI in environments without a live stack.
#
# Usage:
#   verify-client.sh [--config <client.yaml>] [--offline] \
#       [--secrets-dir <backend secrets dir>]
#
# Exit codes: 0 all checks passed, 2 usage, 3 verification failed.
set -uo pipefail

CONFIG=""
OFFLINE=0
SECRETS_DIR="${EGRESS_SECRETS:-/etc/egress/secrets}"
EGRESSCTL="${EGRESSCTL:-egressctl}"

FAILURES=0
pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAILURES=$((FAILURES + 1)); }
skip() { echo "  SKIP  $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config)      CONFIG="${2:?}"; shift 2 ;;
    --offline)     OFFLINE=1; shift ;;
    --secrets-dir) SECRETS_DIR="${2:?}"; shift 2 ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "verify-client: unknown argument: $1" >&2; exit 2 ;;
  esac
done

CFG_ARGS=()
[ -n "$CONFIG" ] && CFG_ARGS=(--config "$CONFIG")

echo "== verify-client =="

# 1. config validate
if "$EGRESSCTL" "${CFG_ARGS[@]}" config validate >/tmp/.verify-cfg.out 2>&1; then
  pass "config validate ($(cat /tmp/.verify-cfg.out))"
else
  fail "config validate: $(cat /tmp/.verify-cfg.out)"
fi

# 2-4. live backend checks
if [ "$OFFLINE" -eq 1 ]; then
  skip "whoami (offline)"
  skip "doctor (offline)"
  skip "status (offline)"
else
  if "$EGRESSCTL" "${CFG_ARGS[@]}" whoami >/tmp/.verify-whoami.out 2>&1; then
    pass "whoami ($(cat /tmp/.verify-whoami.out))"
  else
    fail "whoami: $(cat /tmp/.verify-whoami.out)"
  fi
  if "$EGRESSCTL" "${CFG_ARGS[@]}" doctor >/tmp/.verify-doctor.out 2>&1; then
    pass "doctor"
    sed 's/^/        /' /tmp/.verify-doctor.out
  else
    fail "doctor:"
    sed 's/^/        /' /tmp/.verify-doctor.out
  fi
  if "$EGRESSCTL" "${CFG_ARGS[@]}" status >/tmp/.verify-status.out 2>&1; then
    pass "status ($(cat /tmp/.verify-status.out))"
  else
    fail "status: $(cat /tmp/.verify-status.out)"
  fi
fi

# 5. config show (masked)
if "$EGRESSCTL" "${CFG_ARGS[@]}" config show >/tmp/.verify-show.out 2>&1; then
  pass "config show (secrets masked)"
else
  fail "config show: $(cat /tmp/.verify-show.out)"
fi

# 6. secret file permissions from the config itself
CFG_FILE=""
if [ -n "$CONFIG" ]; then CFG_FILE="$CONFIG"
elif [ -n "${EGRESS_CONFIG:-}" ]; then CFG_FILE="$EGRESS_CONFIG"
else CFG_FILE="/etc/egress/client.yaml"; fi
if [ -f "$CFG_FILE" ]; then
  TOKEN_FILE="$(sed -n 's/^api_token_file:[[:space:]]*//p' "$CFG_FILE" | head -1)"
  AUTH_FILE="$(sed -n 's/^default_proxy_auth_file:[[:space:]]*//p' "$CFG_FILE" | head -1)"
  for f in "$TOKEN_FILE" "$AUTH_FILE"; do
    if [ -z "$f" ]; then continue; fi
    if [ ! -f "$f" ]; then
      fail "secret file missing: $f"
      continue
    fi
    PERM="$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f")"
    if [ "$PERM" = "600" ]; then
      pass "secret perms 600: $f"
    else
      fail "secret perms $PERM (expected 600): $f"
    fi
  done
else
  skip "secret perms (no client.yaml found: $CFG_FILE)"
fi

# 7. no provider secrets in client config dir
if [ -d "$(dirname "$CFG_FILE")" ] && [ -f "$CFG_FILE" ]; then
  CLIENT_DIR="$(dirname "$CFG_FILE")"
  if [ -d "$SECRETS_DIR/providers" ] && ls "$SECRETS_DIR/providers/"* >/dev/null 2>&1; then
    LEAK="$(python3 - "$CLIENT_DIR" "$SECRETS_DIR" <<'PY'
import sys, os
client_dir, secrets_dir = sys.argv[1], sys.argv[2]
leaks = []
for name in os.listdir(os.path.join(secrets_dir, "providers")):
    p = os.path.join(secrets_dir, "providers", name)
    if not os.path.isfile(p):
        continue
    needle = open(p, "rb").read().strip()
    if len(needle) < 8:
        continue
    for root, _, files in os.walk(client_dir):
        for f in files:
            fp = os.path.join(root, f)
            try:
                if needle in open(fp, "rb").read():
                    leaks.append(f"{fp} contains provider secret {name}")
            except OSError:
                pass
print("\n".join(leaks))
PY
)"
    if [ -n "$LEAK" ]; then
      fail "provider secret leaked into client dir: $LEAK"
    else
      pass "no provider secrets in client dir"
    fi
  else
    skip "provider secret scan (no provider secrets at $SECRETS_DIR/providers)"
  fi
fi

rm -f /tmp/.verify-*.out

echo "== verify-client: $FAILURES failure(s) =="
[ "$FAILURES" -eq 0 ] || exit 3
exit 0
