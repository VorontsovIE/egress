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
  --add-host=host.docker.internal:host-gateway \
  -v "$REPO:/workspace:ro" -v "$BUNDLE:/bundle:ro" \
  "$IMAGE" sleep infinity >/dev/null

docker exec -u root "$NAME" bash /workspace/scripts/install-client.sh \
  --client "$(sed -n 's/^client_id:[[:space:]]*//p' "$BUNDLE/client.yaml" | head -1)" \
  --target container --bundle /bundle

docker exec "$NAME" egressctl whoami
for route in "${ROUTES[@]}"; do
  docker exec "$NAME" egress-run "$route" -- sh -c 'test -e /.dockerenv'
  docker exec "$NAME" egress-run "$route" -- \
    curl --fail --silent --show-error --max-time 30 \
    https://www.cloudflare.com/cdn-cgi/trace >/dev/null
  printf 'route %s: PASS\n' "$route"
done
printf 'ordinary Docker client container: PASS (all child processes stayed inside %s)\n' "$NAME"
