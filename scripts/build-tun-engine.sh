#!/usr/bin/env bash
# Build the pinned local TUN packet engine for Linux amd64 and arm64.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODULE="$ROOT/client/tun-engine"
OUT="$MODULE/dist"
EXPECTED_GO="go1.26.3"

command -v go >/dev/null 2>&1 || {
  echo "build-tun-engine: Go $EXPECTED_GO is required" >&2
  exit 3
}
GOVERSION="$(go env GOVERSION)"
[ "$GOVERSION" = "$EXPECTED_GO" ] || {
  echo "build-tun-engine: expected $EXPECTED_GO, found $GOVERSION" >&2
  exit 3
}
mkdir -p "$OUT"
for arch in amd64 arm64; do
  (cd "$MODULE" && CGO_ENABLED=0 GOOS=linux GOARCH="$arch" \
    go build -mod=readonly -trimpath -ldflags='-s -w -buildid=' \
      -o "$OUT/egress-tun-engine-linux-$arch" .)
done
(cd "$OUT" && sha256sum egress-tun-engine-linux-* > SHA256SUMS)
echo "built pinned TUN engine artifacts in $OUT"
