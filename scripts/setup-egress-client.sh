#!/usr/bin/env bash
# One-command client setup for a host or the repository's generic Docker test.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT=""
ENVIRONMENT=""
OUT="${VIBELAB_EGRESS_BUNDLE_ROOT:-/var/lib/vibelab-egress/client-bundles}"
SECRETS_DIR="${VIBELAB_EGRESS_SECRETS:-/etc/vibelab-egress/secrets}"
DOCKER_TEST=0
ROUTES=()

usage() {
  cat <<'EOF'
Usage:
  sudo scripts/setup-egress-client.sh --client <id> --environment host
  sudo scripts/setup-egress-client.sh --client <id> --environment container \
      --docker-test --route <provider:route> [--route <provider:route> ...]

Requires an installed backend. Builds the pinned TUN engine if needed,
provisions the client, and installs it on the host or runs the generic Docker
test. Building the engine requires Go 1.26.3.
Options: --out <bundle-root>, --secrets-dir <path>.
EOF
}
fail() { echo "setup-egress-client: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --client) [ "$#" -ge 2 ] || fail "--client needs a value"; CLIENT="$2"; shift 2 ;;
    --environment) [ "$#" -ge 2 ] || fail "--environment needs a value"; ENVIRONMENT="$2"; shift 2 ;;
    --out) [ "$#" -ge 2 ] || fail "--out needs a value"; OUT="$2"; shift 2 ;;
    --secrets-dir) [ "$#" -ge 2 ] || fail "--secrets-dir needs a value"; SECRETS_DIR="$2"; shift 2 ;;
    --docker-test) DOCKER_TEST=1; shift ;;
    --route) [ "$#" -ge 2 ] || fail "--route needs a value"; ROUTES+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo 'setup-egress-client: run with sudo' >&2; exit 3; }
[ -n "$CLIENT" ] || fail "--client is required"
case "$ENVIRONMENT" in
  host) [ "$DOCKER_TEST" = 0 ] || fail "--docker-test applies only to container" ;;
  container)
    if [ "$DOCKER_TEST" = 1 ]; then
      [ "${#ROUTES[@]}" -gt 0 ] || fail "--docker-test requires at least one --route"
    elif [ "${#ROUTES[@]}" -gt 0 ]; then
      fail "--route requires --docker-test"
    fi
    ;;
  *) fail "--environment must be host or container" ;;
esac

DIST="$ROOT/client/tun-engine/dist"
if [ ! -x "$DIST/egress-tun-engine-linux-amd64" ] || \
   [ ! -x "$DIST/egress-tun-engine-linux-arm64" ]; then
  bash "$ROOT/scripts/build-tun-engine.sh"
fi

BUNDLE="$OUT/$CLIENT"
if [ -f "$BUNDLE/client.yaml" ] && [ -f "$BUNDLE/client.token" ] && \
   [ -f "$BUNDLE/default-proxy.auth" ] && [ -f "$BUNDLE/tun-engine/SHA256SUMS" ] && \
   grep -qx "environment: $ENVIRONMENT" "$BUNDLE/client.yaml" && \
   grep -qx 'tun_backend: local-userns' "$BUNDLE/client.yaml" && \
   [ -f "$SECRETS_DIR/clients/$CLIENT.token" ] && \
   [ -f "$SECRETS_DIR/clients/$CLIENT.profile.json" ] && \
   [ -f "$SECRETS_DIR/clients/$CLIENT.proxy-creds" ]; then
  echo "reusing existing client bundle: $BUNDLE"
else
  bash "$ROOT/scripts/provision-client.sh" \
    --client "$CLIENT" --environment "$ENVIRONMENT" \
    --out "$OUT" --secrets-dir "$SECRETS_DIR"
fi

if [ "$ENVIRONMENT" = host ]; then
  bash "$ROOT/scripts/install-client.sh" \
    --client "$CLIENT" --target host --bundle "$BUNDLE"
  echo "Host client is ready. Check TUN support with: egressctl capabilities"
elif [ "$DOCKER_TEST" = 1 ]; then
  bash "$ROOT/tests/docker/run-client-container.sh" "$BUNDLE" "${ROUTES[@]}"
else
  cat <<EOF
Container bundle ready: $BUNDLE
Install it inside the target container with:
  bash $ROOT/scripts/install-client.sh --client $CLIENT --target container --bundle <bundle-path>
For Docker TUN, start the container with /dev/net/tun and:
  --security-opt seccomp=$ROOT/client/seccomp-tun.json
The image also needs util-linux (unshare) and iproute2 (ip).
EOF
fi
