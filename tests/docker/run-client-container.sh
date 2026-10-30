#!/usr/bin/env bash
# Run the installed client in an ordinary, isolated Docker container.
# Usage: tests/docker/run-client-container.sh <client-bundle> [route ...]
# Two or more routes also trigger a concurrent TUN check for the first two.
set -euo pipefail

BUNDLE="${1:?usage: run-client-container.sh <client-bundle> [route ...]}"
ROUTES=("${@:2}")
[ "${#ROUTES[@]}" -gt 0 ] || { echo "at least one non-direct proxy route is required" >&2; exit 2; }
for route in "${ROUTES[@]}"; do
  [ "$route" != direct ] || { echo "route direct bypasses the proxy; pass a provider route" >&2; exit 2; }
done
[ -d "$BUNDLE" ] || { echo "bundle not found: $BUNDLE" >&2; exit 2; }
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="egress-test-client:local"
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
ROUTE_INDEX=0
for route in "${ROUTES[@]}"; do
  ROUTE_INDEX=$((ROUTE_INDEX + 1))
  docker exec "$NAME" egress-run --mode proxy "$route" -- sh -c 'test -e /.dockerenv'
  # Keep both workloads alive while comparing netns inode IDs. Linux may
  # recycle an ID after a namespace exits, so sequential readlink calls can
  # report the same value even though each invocation got a new namespace.
  NS_CHECK_DIR="/tmp/egress-netns-${NAME}-${ROUTE_INDEX}"
  NS_A="$NS_CHECK_DIR/a"
  NS_B="$NS_CHECK_DIR/b"
  NS_RELEASE="$NS_CHECK_DIR/release"
  NS_LOG_A="$NS_CHECK_DIR/a.log"
  NS_LOG_B="$NS_CHECK_DIR/b.log"
  NS_STATUS_A="$NS_CHECK_DIR/a.status"
  NS_STATUS_B="$NS_CHECK_DIR/b.status"
  docker exec "$NAME" mkdir -m 700 "$NS_CHECK_DIR"
  docker exec -d "$NAME" sh -c '
    egress-run --mode tun "$1" -- python3 /workspace/tests/docker/netns-worker.py \
      "$2" "$3" "$4" >"$5" 2>&1
    rc=$?
    printf "%s\n" "$rc" >"$6"
    exit "$rc"
  ' sh "$route" "$NS_A" "$NS_B" "$NS_RELEASE" "$NS_LOG_A" "$NS_STATUS_A"
  docker exec -d "$NAME" sh -c '
    egress-run --mode tun "$1" -- python3 /workspace/tests/docker/netns-worker.py \
      "$2" "$3" "$4" >"$5" 2>&1
    rc=$?
    printf "%s\n" "$rc" >"$6"
    exit "$rc"
  ' sh "$route" "$NS_B" "$NS_A" "$NS_RELEASE" "$NS_LOG_B" "$NS_STATUS_B"
  NS_READY=0
  for _ in {1..600}; do
    if docker exec "$NAME" test -s "$NS_A" && docker exec "$NAME" test -s "$NS_B"; then
      NS_READY=1
      break
    fi
    if docker exec "$NAME" test -f "$NS_STATUS_A" || docker exec "$NAME" test -f "$NS_STATUS_B"; then
      break
    fi
    sleep 0.1
  done
  if [ "$NS_READY" != 1 ]; then
    docker exec "$NAME" sh -c 'cat "$1" "$2" 2>/dev/null || true' \
      sh "$NS_LOG_A" "$NS_LOG_B" >&2
    echo "route $route: concurrent TUN invocations failed to enter their namespaces" >&2
    exit 1
  fi
  TUN_NETNS_A="$(docker exec "$NAME" cat "$NS_A")"
  TUN_NETNS_B="$(docker exec "$NAME" cat "$NS_B")"
  [ "$TUN_NETNS_A" != "$OUTER_NETNS" ] && [ "$TUN_NETNS_B" != "$OUTER_NETNS" ] \
    && [ "$TUN_NETNS_A" != "$TUN_NETNS_B" ] || {
      docker exec "$NAME" touch "$NS_RELEASE"
      echo "route $route: concurrent TUN invocations shared a network namespace" >&2
      exit 1
    }
  docker exec "$NAME" touch "$NS_RELEASE"
  NS_DONE=0
  for _ in {1..600}; do
    if docker exec "$NAME" test -f "$NS_STATUS_A" && docker exec "$NAME" test -f "$NS_STATUS_B"; then
      NS_DONE=1
      break
    fi
    sleep 0.1
  done
  if [ "$NS_DONE" != 1 ] || \
     [ "$(docker exec "$NAME" cat "$NS_STATUS_A" 2>/dev/null || true)" != 0 ] || \
     [ "$(docker exec "$NAME" cat "$NS_STATUS_B" 2>/dev/null || true)" != 0 ]; then
    docker exec "$NAME" sh -c 'cat "$1" "$2" 2>/dev/null || true' \
      sh "$NS_LOG_A" "$NS_LOG_B" >&2
    echo "route $route: concurrent TUN namespace checks did not complete cleanly" >&2
    exit 1
  fi
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

if [ "${#ROUTES[@]}" -ge 2 ]; then
  ROUTE_A="${ROUTES[0]}"
  ROUTE_B="${ROUTES[1]}"
  [ "$ROUTE_A" != "$ROUTE_B" ] || {
    echo "concurrent TUN check needs two different route IDs" >&2
    exit 2
  }
  CONCURRENT_DIR="/tmp/egress-concurrent-${NAME}"
  NS_A="$CONCURRENT_DIR/a.netns"
  NS_B="$CONCURRENT_DIR/b.netns"
  RELEASE="$CONCURRENT_DIR/release"
  RESULT_A="$CONCURRENT_DIR/a.ip"
  RESULT_B="$CONCURRENT_DIR/b.ip"
  LOG_A="$CONCURRENT_DIR/a.log"
  LOG_B="$CONCURRENT_DIR/b.log"
  STATUS_A="$CONCURRENT_DIR/a.status"
  STATUS_B="$CONCURRENT_DIR/b.status"
  docker exec "$NAME" mkdir -m 700 "$CONCURRENT_DIR"

  docker exec -d "$NAME" sh -c '
    egress-run --mode tun "$1" -- python3 /workspace/tests/docker/concurrent-route-worker.py \
      "$2" "$3" "$4" "$5" >"$6" 2>&1
    rc=$?
    printf "%s\n" "$rc" >"$7"
    exit "$rc"
  ' sh "$ROUTE_A" "$NS_A" "$NS_B" "$RELEASE" "$RESULT_A" "$LOG_A" "$STATUS_A"
  docker exec -d "$NAME" sh -c '
    egress-run --mode tun "$1" -- python3 /workspace/tests/docker/concurrent-route-worker.py \
      "$2" "$3" "$4" "$5" >"$6" 2>&1
    rc=$?
    printf "%s\n" "$rc" >"$7"
    exit "$rc"
  ' sh "$ROUTE_B" "$NS_B" "$NS_A" "$RELEASE" "$RESULT_B" "$LOG_B" "$STATUS_B"

  CONCURRENT_READY=0
  for _ in {1..1200}; do
    if docker exec "$NAME" test -s "$NS_A" && docker exec "$NAME" test -s "$NS_B"; then
      CONCURRENT_READY=1
      break
    fi
    if docker exec "$NAME" test -f "$STATUS_A" || docker exec "$NAME" test -f "$STATUS_B"; then
      break
    fi
    sleep 0.1
  done
  if [ "$CONCURRENT_READY" != 1 ]; then
    docker exec "$NAME" sh -c 'cat "$1" "$2" 2>/dev/null || true' \
      sh "$LOG_A" "$LOG_B" >&2
    echo "concurrent TUN routes failed to enter their namespaces" >&2
    exit 1
  fi

  TUN_NETNS_A="$(docker exec "$NAME" cat "$NS_A")"
  TUN_NETNS_B="$(docker exec "$NAME" cat "$NS_B")"
  [ "$TUN_NETNS_A" != "$OUTER_NETNS" ] && [ "$TUN_NETNS_B" != "$OUTER_NETNS" ] \
    && [ "$TUN_NETNS_A" != "$TUN_NETNS_B" ] || {
      docker exec "$NAME" touch "$RELEASE"
      echo "concurrent TUN routes shared a network namespace" >&2
      exit 1
    }
  docker exec "$NAME" touch "$RELEASE"

  CONCURRENT_DONE=0
  for _ in {1..600}; do
    if docker exec "$NAME" test -f "$STATUS_A" && docker exec "$NAME" test -f "$STATUS_B"; then
      CONCURRENT_DONE=1
      break
    fi
    sleep 0.1
  done
  if [ "$CONCURRENT_DONE" != 1 ] || \
     [ "$(docker exec "$NAME" cat "$STATUS_A" 2>/dev/null || true)" != 0 ] || \
     [ "$(docker exec "$NAME" cat "$STATUS_B" 2>/dev/null || true)" != 0 ] || \
     ! docker exec "$NAME" test -s "$RESULT_A" || \
     ! docker exec "$NAME" test -s "$RESULT_B"; then
    docker exec "$NAME" sh -c 'cat "$1" "$2" 2>/dev/null || true' \
      sh "$LOG_A" "$LOG_B" >&2
    echo "concurrent TUN route workload failed" >&2
    exit 1
  fi

  EXIT_IP_A="$(docker exec "$NAME" cat "$RESULT_A")"
  EXIT_IP_B="$(docker exec "$NAME" cat "$RESULT_B")"
  [ "$EXIT_IP_A" != "$EXIT_IP_B" ] || {
    echo "concurrent route check needs routes with distinct public exit IPs" >&2
    exit 1
  }
  printf 'concurrent TUN routes: PASS\n'
  printf '  %s netns=%s exit_ip=%s\n' "$ROUTE_A" "$TUN_NETNS_A" \
    "$EXIT_IP_A"
  printf '  %s netns=%s exit_ip=%s\n' "$ROUTE_B" "$TUN_NETNS_B" \
    "$EXIT_IP_B"
fi

printf 'ordinary Docker client container: PASS (all child processes stayed inside %s)\n' "$NAME"
