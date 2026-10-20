#!/usr/bin/env bash
# Run the installed client in an ordinary, isolated Docker container.
# Usage: tests/docker/run-client-container.sh <client-bundle> [route ...]
set -euo pipefail

BUNDLE="${1:?usage: run-client-container.sh <client-bundle> [route ...]}"
ROUTES=("${@:2}")
[ "${#ROUTES[@]}" -gt 0 ] || { echo "at least one non-direct proxy route is required" >&2; exit 2; }
for route in "${ROUTES[@]}"; do
  [ "$route" != direct ] || { echo "route direct bypasses the proxy; pass a provider route" >&2; exit 2; }
done
[ -d "$BUNDLE" ] || { echo "bundle not found: $BUNDLE" >&2; exit 2; }
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="vibelab-egress-test-client:local"
NAME="egress-test-client-$$"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -f "$REPO/tests/docker/Dockerfile" -t "$IMAGE" "$REPO/tests/docker"
docker run -d --name "$NAME" --network bridge \
  --cap-drop NET_ADMIN --cap-drop SYS_ADMIN \
  --device /dev/net/tun:/dev/net/tun:rwm \
  --security-opt "seccomp=$REPO/client/seccomp-tun.json" \
  --add-host=host.docker.internal:host-gateway \
  -v "$REPO:/workspace:ro" -v "$BUNDLE:/bundle:ro" \
  "$IMAGE" sleep infinity >/dev/null

docker exec -u root "$NAME" bash /workspace/scripts/install-client.sh \
  --client "$(sed -n 's/^client_id:[[:space:]]*//p' "$BUNDLE/client.yaml" | head -1)" \
  --target container --bundle /bundle

docker exec "$NAME" egressctl whoami
docker exec "$NAME" egressctl doctor
OUTER_NETNS="$(docker exec "$NAME" readlink /proc/self/ns/net)"
for route in "${ROUTES[@]}"; do
  docker exec "$NAME" egress-run --mode proxy "$route" -- sh -c 'test -e /.dockerenv'
  TUN_NETNS_A="$(docker exec "$NAME" egress-run --mode tun "$route" -- readlink /proc/self/ns/net)"
  TUN_NETNS_B="$(docker exec "$NAME" egress-run --mode tun "$route" -- readlink /proc/self/ns/net)"
  [ "$TUN_NETNS_A" != "$OUTER_NETNS" ] && [ "$TUN_NETNS_B" != "$OUTER_NETNS" ] \
    && [ "$TUN_NETNS_A" != "$TUN_NETNS_B" ] || {
      echo "route $route: TUN invocations did not get separate network namespaces" >&2
      exit 1
    }
  docker exec "$NAME" egress-run --mode tun "$route" -- sh -c 'test -e /.dockerenv'
  docker exec "$NAME" egress-run --mode tun "$route" -- python3 -c \
    's=open("/proc/self/status").read(); assert "NoNewPrivs:\t1" in s; assert "CapEff:\t0000000000000000" in s'
  docker exec "$NAME" egress-run --mode tun "$route" -- sh -c \
    'if unshare --user --map-current-user true 2>/dev/null; then exit 1; fi'
  docker exec "$NAME" egress-run --mode tun "$route" -- \
    curl --ipv4 --fail --silent --show-error --max-time 30 \
    https://www.cloudflare.com/cdn-cgi/trace >/dev/null
  printf 'route %s: PASS\n' "$route"
done
printf 'ordinary Docker client container: PASS (all child processes stayed inside %s)\n' "$NAME"
