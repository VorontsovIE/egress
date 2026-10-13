#!/usr/bin/env bash
# bootstrap-dev-access.sh — set up the narrow dev/test bridge so the
# OpenScience agent can request fixed host operations (spec section 33.2).
#
# This is the ONE-TIME administrator step. After it runs, the agent can
# POST to the devbridge (fixed ops only: deploy/redeploy/status/logs/
# run-acceptance/sync-docker-cidrs/install-client). There is no arbitrary
# host command path.
#
# Steps:
#   1. generate the devbridge bearer token (0600, never printed)
#   2. install the devbridge systemd unit (if systemctl available)
#   3. start it; verify /healthz
#   4. write the agent-facing connection info (token path only, no value)
#
# Usage:
#   bootstrap-dev-access.sh [--bind <ip>] [--port <p>] [--secrets-dir <dir>] \
#       [--repo <repo-root>] [--yes]
#
# Exit codes: 0 ok, 2 usage, 3 environment error.
set -euo pipefail

BIND="${DEVBRIDGE_BIND:-172.17.0.1}"
PORT="${DEVBRIDGE_PORT:-9099}"
SECRETS_DIR="${VIBELAB_EGRESS_SECRETS:-/etc/vibelab-egress/secrets}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ASSUME_YES=0

die_usage() { echo "bootstrap-dev-access: $*" >&2; exit 2; }
die_env()   { echo "bootstrap-dev-access: $*" >&2; exit 3; }
say()       { echo "[bootstrap-dev-access] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --bind)        BIND="${2:?}"; shift 2 ;;
    --port)        PORT="${2:?}"; shift 2 ;;
    --secrets-dir) SECRETS_DIR="${2:?}"; shift 2 ;;
    --repo)        REPO_ROOT="${2:?}"; shift 2 ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" = "0" ] || die_env "must run as root"
command -v python3 >/dev/null || die_env "python3 not found"
[ -f "$REPO_ROOT/host/egress-devbridge" ] || die_env "egress-devbridge not found in $REPO_ROOT/host"

confirm() {
  [ "$ASSUME_YES" = "1" ] && return 0
  read -r -p "$1 [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

# 1. token
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"
TOKEN_FILE="$SECRETS_DIR/devbridge.token"
if [ -f "$TOKEN_FILE" ]; then
  say "keep existing devbridge token"
else
  umask 077
  python3 -c "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_urlsafe(32))" "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  say "generated devbridge token"
fi

# 2. systemd unit (if available)
if command -v systemctl >/dev/null; then
  say "installing devbridge systemd unit"
  sed -e "s|--bind 172.17.0.1|--bind $BIND|" \
      -e "s|--port 9099|--port $PORT|" \
      -e "s|/srv/workspace/vibelab-egress|$REPO_ROOT|g" \
      "$REPO_ROOT/host/units/vibelab-egress-devbridge.service" \
      > /etc/systemd/system/vibelab-egress-devbridge.service
  systemctl daemon-reload
  systemctl enable --now vibelab-egress-devbridge.service
  say "devbridge service started"
else
  say "systemctl not available; start the bridge manually with:"
  say "  python3 $REPO_ROOT/host/egress-devbridge --bind $BIND --port $PORT --token-file $TOKEN_FILE"
fi

# 3. verify healthz
sleep 1
if curl -fsS "http://$BIND:$PORT/healthz" >/dev/null 2>&1; then
  say "devbridge /healthz OK at http://$BIND:$PORT"
else
  say "warning: /healthz not reachable yet (service may still be starting)"
fi

# 4. agent-facing info (no secret value)
INFO="$SECRETS_DIR/devbridge.agent-info.json"
python3 - "$BIND" "$PORT" "$TOKEN_FILE" > "$INFO" <<'PY'
import json, sys, hashlib
bind, port, token_file = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    h = hashlib.sha256(open(token_file, "rb").read().strip()).hexdigest()[:8]
except OSError:
    h = "missing"
print(json.dumps({
    "endpoint": f"http://{bind}:{port}",
    "auth": "Bearer <contents of token file>",
    "token_file": token_file,
    "token_sha256_8": h,
    "allowed_ops": ["deploy", "redeploy", "status", "logs",
                    "run-acceptance", "sync-docker-cidrs", "install-client"],
    "note": "fixed operations only; no arbitrary host command",
}, indent=2))
PY
chmod 600 "$INFO"
say "agent connection info written to $INFO"
say "bootstrap-dev-access complete"
