#!/usr/bin/env bash
# Build the pinned local TUN packet engine for Linux amd64 and arm64.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODULE="$ROOT/client/tun-engine"
OUT="$MODULE/dist"
MIN_GO="1.26.3"

command -v go >/dev/null 2>&1 || {
  echo "build-tun-engine: Go $MIN_GO or newer is required" >&2
  exit 3
}
GOVERSION="$(go env GOVERSION)"
if [[ ! "$GOVERSION" =~ ^go([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "build-tun-engine: cannot verify Go version '$GOVERSION'; stable Go $MIN_GO or newer is required" >&2
  exit 3
fi
GO_MAJOR="${BASH_REMATCH[1]}"
GO_MINOR="${BASH_REMATCH[2]}"
GO_PATCH="${BASH_REMATCH[3]}"
if (( GO_MAJOR < 1 || (GO_MAJOR == 1 && GO_MINOR < 26) || \
      (GO_MAJOR == 1 && GO_MINOR == 26 && GO_PATCH < 3) )); then
  echo "build-tun-engine: Go $MIN_GO or newer is required, found $GOVERSION" >&2
  exit 3
fi
mkdir -p "$OUT"
for arch in amd64 arm64; do
  (cd "$MODULE" && CGO_ENABLED=0 GOOS=linux GOARCH="$arch" \
    go build -mod=readonly -trimpath -ldflags='-s -w -buildid=' \
      -o "$OUT/egress-tun-engine-linux-$arch" .)
done
(cd "$OUT" && sha256sum egress-tun-engine-linux-* > SHA256SUMS)
echo "built TUN engine artifacts with Go $GOVERSION in $OUT"
