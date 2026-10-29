#!/usr/bin/env bash
# One-command client setup for a host or the repository's generic Docker test.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT=""
ENVIRONMENT=""
EGRESS_ROOT="${VIBELAB_EGRESS_ROOT:-}"
SECRETS_DIR="${VIBELAB_EGRESS_SECRETS:-}"
if [ -r /etc/vibelab-egress/compose.env ]; then
  # Read only these data values; do not source/evaluate the Compose env file.
  while IFS='=' read -r key value; do
    case "$key" in
      VIBELAB_EGRESS_ROOT) [ -n "$EGRESS_ROOT" ] || EGRESS_ROOT="$value" ;;
      VIBELAB_EGRESS_SECRETS) [ -n "$SECRETS_DIR" ] || SECRETS_DIR="$value" ;;
    esac
  done < /etc/vibelab-egress/compose.env
fi
EGRESS_ROOT="${EGRESS_ROOT:-/var/lib/vibelab-egress}"
SECRETS_DIR="${SECRETS_DIR:-/etc/vibelab-egress/secrets}"
OUT="${VIBELAB_EGRESS_BUNDLE_ROOT:-$EGRESS_ROOT/client-bundles}"
OUT_EXPLICIT=0
DOCKER_TEST=0
COPY_PERSONAL_SOURCE=0
ROUTES=()

usage() {
  cat <<'EOF'
Usage:
  sudo scripts/setup-egress-client.sh --environment host [--client <id>]
  sudo scripts/setup-egress-client.sh --environment container [--client <id>] \
      --docker-test --route <provider:route> [--route <provider:route> ...] \
      [--copy-personal-source]

Requires an installed backend. Builds the pinned TUN engine if needed,
provisions the client, and installs it on the host or runs the generic Docker
test. Building the engine requires stable Go 1.26.3 or newer.
Without --client, the invoking UID gets one stable default profile:
host-uid-<uid> for host setup and container-uid-<uid> for container setup.
Pass --client to use a different profile.
Options: --out <bundle-root>, --secrets-dir <path>, --root <backend-root>.
--copy-personal-source snapshots a personal provider under the test client's
own identity before running the Docker test; every supplied route must be a
personal provider node. Each distinct source provider is copied once. This does
not grant access to its owner.
EOF
}
fail() { echo "setup-egress-client: $*" >&2; exit 2; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --client) [ "$#" -ge 2 ] || fail "--client needs a value"; CLIENT="$2"; shift 2 ;;
    --environment) [ "$#" -ge 2 ] || fail "--environment needs a value"; ENVIRONMENT="$2"; shift 2 ;;
    --out) [ "$#" -ge 2 ] || fail "--out needs a value"; OUT="$2"; OUT_EXPLICIT=1; shift 2 ;;
    --secrets-dir) [ "$#" -ge 2 ] || fail "--secrets-dir needs a value"; SECRETS_DIR="$2"; shift 2 ;;
    --root)
      [ "$#" -ge 2 ] || fail "--root needs a value"
      EGRESS_ROOT="$2"
      [ "$OUT_EXPLICIT" = 1 ] || OUT="$EGRESS_ROOT/client-bundles"
      shift 2
      ;;
    --docker-test) DOCKER_TEST=1; shift ;;
    --copy-personal-source) COPY_PERSONAL_SOURCE=1; shift ;;
    --route) [ "$#" -ge 2 ] || fail "--route needs a value"; ROUTES+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo 'setup-egress-client: run with sudo' >&2; exit 3; }
case "$ENVIRONMENT" in
  host) [ "$DOCKER_TEST" = 0 ] || fail "--docker-test applies only to container" ;;
  container)
    if [ "$DOCKER_TEST" = 1 ]; then
      [ "${#ROUTES[@]}" -gt 0 ] || fail "--docker-test requires at least one --route"
      if [ "$COPY_PERSONAL_SOURCE" = 1 ]; then
        for route in "${ROUTES[@]}"; do
          route_provider="${route%%:*}"
          route_node="${route#*:}"
          [[ "$route_provider" =~ ^p-[0-9a-f]{24}-[A-Za-z0-9._-]{1,32}$ && \
             "$route_node" != "$route" && "$route_node" != *:* && \
             "$route_node" =~ ^[A-Za-z0-9._-]+$ ]] || \
            fail "--copy-personal-source requires personal provider:node route IDs"
        done
      fi
    elif [ "${#ROUTES[@]}" -gt 0 ]; then
      fail "--route requires --docker-test"
    elif [ "$COPY_PERSONAL_SOURCE" = 1 ]; then
      fail "--copy-personal-source requires --docker-test"
    fi
    ;;
  *) fail "--environment must be host or container" ;;
esac
if [ -z "$CLIENT" ]; then
  DEFAULT_UID="${SUDO_UID:-$(id -u)}"
  [[ "$DEFAULT_UID" =~ ^[0-9]+$ ]] || fail "cannot determine invoking UID; pass --client explicitly"
  case "$ENVIRONMENT" in
    host) CLIENT="host-uid-$DEFAULT_UID" ;;
    container) CLIENT="container-uid-$DEFAULT_UID" ;;
  esac
  echo "using default client: $CLIENT (pass --client to override)"
fi
[ "$ENVIRONMENT" = container ] || [ "$COPY_PERSONAL_SOURCE" = 0 ] || \
  fail "--copy-personal-source applies only to a Docker container test"

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
  if [ "$COPY_PERSONAL_SOURCE" = 1 ]; then
    source_providers=()
    target_providers=()
    copied_routes=()
    for route in "${ROUTES[@]}"; do
      source_provider="${route%%:*}"
      node="${route#*:}"
      target_provider=""
      for index in "${!source_providers[@]}"; do
        if [ "${source_providers[$index]}" = "$source_provider" ]; then
          target_provider="${target_providers[$index]}"
          break
        fi
      done
      if [ -z "$target_provider" ]; then
        result="$(VIBELAB_EGRESS_ROOT="$EGRESS_ROOT" \
          VIBELAB_EGRESS_SECRETS="$SECRETS_DIR" \
          python3 "$ROOT/host/egress-hostctl" copy-personal-source \
            --source-route "$route" --target-client "$CLIENT")"
        copied_route="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["target_route"])' <<< "$result")"
        target_provider="${copied_route%%:*}"
        copied_node="${copied_route#*:}"
        [ "$copied_node" = "$node" ] || fail "snapshot API returned an unexpected route"
        source_providers+=("$source_provider")
        target_providers+=("$target_provider")
        printf 'copied personal snapshot: %s -> %s\n' \
          "$source_provider" "$target_provider"
      fi
      copied_routes+=("$target_provider:$node")
    done
    ROUTES=("${copied_routes[@]}")
    printf 'using test-owned snapshot routes: %s\n' "${ROUTES[*]}"
  fi
  bash "$ROOT/tests/docker/run-client-container.sh" "$BUNDLE" "${ROUTES[@]}"
else
  cat <<EOF
Container bundle ready: $BUNDLE
Install it inside the target container with:
  bash $ROOT/scripts/install-client.sh --client $CLIENT --target container --bundle <bundle-path>
For Docker TUN, start the container with /dev/net/tun and:
  --security-opt seccomp=$ROOT/client/seccomp-tun.json
The image also needs util-linux (unshare) and iproute2 (ip).
For TUN DNS over HTTPS, install ca-certificates in the image as well.
EOF
fi
