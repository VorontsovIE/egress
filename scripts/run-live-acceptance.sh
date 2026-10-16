#!/usr/bin/env bash
# run-live-acceptance.sh — run the host-side live acceptance checks and produce
# the SINGLE report file to send back for re-verification.
#
# This is the one command to run on the host. It calls host/live-acceptance.py
# with sensible defaults and writes the report to reports/host_acceptance_report.json.
#
# Prerequisites (all must be true on the host):
#   * Docker daemon running and the egress stack up (compose.yaml [+ compose.tun.yaml
#     if TUN is enabled]).
#   * The host client installed (egressctl + egress-run on PATH) with
#     /etc/vibelab-egress/client.yaml + token + auth.
#   * The OpenScience container running (its name is auto-detected; override with
#     VIBELAB_EGRESS_OS_CONTAINER if detection fails).
#   * The TUN helper socket present for the TUN checks (compose.tun.yaml).
#
# Usage:
#   bash scripts/run-live-acceptance.sh                 # default (non-destructive)
#   bash scripts/run-live-acceptance.sh --recreate      # observe OpenScience
#                                                       # identity after restart;
#                                                       # full recreate is not automated
#   bash scripts/run-live-acceptance.sh --out PATH      # write the report to PATH
#                                                       # (default: reports/host_acceptance_report.json)
# Any other flag (--no-tun, --check, --timeout, ...) is forwarded to
# host/live-acceptance.py unchanged.
#
# Optional environment overrides:
#   VIBELAB_EGRESS_OS_CONTAINER   OpenScience container name (default: auto-detect)
#   VIBELAB_EGRESS_LITELLM_URL    LiteLLM health URL (default http://127.0.0.1:4000/health/liveliness)
#   VIBELAB_EGRESS_OS_CONFIG      OpenScience bundle client.yaml (for static checks)
#   VIBELAB_EGRESS_TUN_HELPER_SOCKET  TUN helper socket (default /run/vibelab-egress/tun-helper.sock)
#   VIBELAB_EGRESS_REPO           repo root (default: this script's repo)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${VIBELAB_EGRESS_REPO:-$(cd "$HERE/.." && pwd)}"
# The producer resolves the repo (for host/egress-host-test + compose.tun.yaml)
# and the persistent data/secrets roots from these env vars; export them so a
# bare `bash scripts/run-live-acceptance.sh` works from any checkout location.
export VIBELAB_EGRESS_REPO="$REPO"
export VIBELAB_EGRESS_ROOT="${VIBELAB_EGRESS_ROOT:-/home/vibelab/openscience/egress}"
export VIBELAB_EGRESS_SECRETS="${VIBELAB_EGRESS_SECRETS:-/etc/vibelab-egress/secrets}"
# Parse args: extract an optional `--out <path>` (or `--out=<path>`) for the
# report location; everything else (e.g. --recreate, --no-tun, --check ...) is
# forwarded to live-acceptance.py. The report path must NOT be read from $1,
# because the first argument is usually a flag, not a path.
OUT="reports/host_acceptance_report.json"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      OUT="${2:?--out requires a value}"
      shift 2
      ;;
    --out=*)
      OUT="${1#--out=}"
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

# --- auto-detect the OpenScience container name -----------------------------
detect_os_container() {
  local name
  # 1. explicit override
  if [ -n "${VIBELAB_EGRESS_OS_CONTAINER:-}" ]; then
    printf '%s' "$VIBELAB_EGRESS_OS_CONTAINER"; return
  fi
  # 2. a running container whose name mentions openscience
  name="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i 'openscience' | head -1)"
  if [ -n "$name" ]; then printf '%s' "$name"; return; fi
  # 3. a running container that has the egress client installed
  name="$(docker ps --format '{{.Names}}' 2>/dev/null | while read -r c; do
            docker exec "$c" test -x /usr/local/bin/egressctl 2>/dev/null && printf '%s' "$c"
          done | head -1)"
  if [ -n "$name" ]; then printf '%s' "$name"; return; fi
  # 4. fall back to the literal default
  printf 'openscience'
}

OS_CONTAINER="$(detect_os_container)"
echo "[run-live-acceptance] repo=$REPO"
echo "[run-live-acceptance] out=$OUT"
echo "[run-live-acceptance] openscience container=$OS_CONTAINER"
echo "[run-live-acceptance] litellm=${VIBELAB_EGRESS_LITELLM_URL:-http://127.0.0.1:4000/health/liveliness}"

mkdir -p "$(dirname "$OUT")"

python3 "$REPO/host/live-acceptance.py" \
  --out "$OUT" \
  --os-container "$OS_CONTAINER" \
  ${ARGS[@]+"${ARGS[@]}"}
rc=$?

echo
echo "======================================================================"
echo "Report written to: $OUT"
echo "RESULT: exit=$rc (0 = no check FAILED; 1 = at least one check FAILED)"
echo "Send THIS SINGLE FILE back for re-verification."
echo "Re-verification command (run by the lead, or by you to preview):"
echo "  python3 $REPO/scripts/verify-live-report.py $OUT"
echo "======================================================================"
exit $rc
