#!/bin/sh
# fetch-geo-data.sh — download GEO datasets (spec 14/41).
#
# GEO data is NOT stored in git (stripped from history on 2026-10-15).
# This script downloads the datasets from the upstream release
# MetaCubeX/meta-rules-dat into the local (untracked) paths the project
# expects:
#
#   host/geo/GeoIP.dat     <- geoip.dat         (install seed, backend step 2)
#   host/geo/GeoSite.dat   <- geosite.dat
#   host/geo/asn.mmdb      <- GeoLite2-ASN.mmdb
#   poc/geo/{GeoIP.dat,GeoSite.dat,asn.mmdb}    (same trio; offline poc runs)
#
# Usage:
#   sh scripts/fetch-geo-data.sh                # core trio -> host/geo + poc/geo
#   sh scripts/fetch-geo-data.sh --full         # + complete release bundle ->
#                                               #   poc/meta-rules-dat-release/ (~70 MB)
#   sh scripts/fetch-geo-data.sh --release TAG  # pin a release tag (codeload URL)
#   sh scripts/fetch-geo-data.sh --check        # verify host/geo vs poc/geo sync
#
# Exit codes: 0 ok, 2 usage, 3 environment/download error.
#
# Source URLs (same as egressd geo_update.py / mihomo geox-url):
#   https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat
#   https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat
#   https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/GeoLite2-ASN.mmdb
# The OpenScience container blocks objects.githubusercontent.com, so when a
# direct download fails the script falls back to the codeload release
# tarball: https://codeload.github.com/MetaCubeX/meta-rules-dat/tar.gz/refs/tags/<tag>
#
# Snapshot checksums of the last in-repo copy (2026-10-13; provenance only —
# upstream "latest" moves on; --check compares the local copies against each
# other, not against this snapshot):
#   116cc0f03d48991962f7f9cdbbcf53d45d89777f9c3cd1a663210853e5df6093  geoip.dat
#   7a17130f9fdd19cd0df73b1adacf7d7900f42cf76750507f538f066c37da4a93  geosite.dat
#   d82d2f39ea82a175a3d63484c8b4723040d134ba1edf4b27d6819c2f96c14a9b  GeoLite2-ASN.mmdb
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE="latest"
FULL=0
CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --full)    FULL=1 ;;
    --release) RELEASE="${2:?--release needs a tag}"; shift ;;
    --check)   CHECK=1 ;;
    *) echo "fetch-geo-data: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

UPSTREAM="https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest"
CODELOAD="https://codeload.github.com/MetaCubeX/meta-rules-dat/tar.gz/refs/tags/${RELEASE}"
GEO_HOST="$REPO_ROOT/host/geo"
GEO_POC="$REPO_ROOT/poc/geo"

die() { echo "fetch-geo-data: $*" >&2; exit 3; }

http_get() { # url dest
  if command -v curl >/dev/null 2>&1; then
    curl -fL --connect-timeout 15 --retry 2 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    die "curl or wget is required"
  fi
}

sha_of() { # file
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

if [ "$CHECK" -eq 1 ]; then
  rc=0
  for f in GeoIP.dat GeoSite.dat asn.mmdb; do
    if [ ! -f "$GEO_HOST/$f" ] || [ ! -f "$GEO_POC/$f" ]; then
      echo "MISSING   $f (host/geo and/or poc/geo)"
      rc=3
    elif [ "$(sha_of "$GEO_HOST/$f")" != "$(sha_of "$GEO_POC/$f")" ]; then
      echo "MISMATCH  $f (host/geo != poc/geo)"
      rc=3
    else
      echo "OK        $f  $(sha_of "$GEO_HOST/$f" | cut -c1-16)"
    fi
  done
  exit "$rc"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch_one() { # upstream_name local_name
  if [ -s "$GEO_HOST/$2" ]; then
    echo "  keep existing host/geo/$2"
    return 0
  fi
  if http_get "$UPSTREAM/$1" "$GEO_HOST/$2.tmp" 2>/dev/null; then
    mv "$GEO_HOST/$2.tmp" "$GEO_HOST/$2"
    echo "  fetched $1 -> host/geo/$2 (direct release URL)"
    return 0
  fi
  rm -f "$GEO_HOST/$2.tmp"
  echo "  direct release URL failed for $1 (codeload fallback will run)"
  return 1
}

mkdir -p "$GEO_HOST" "$GEO_POC"

missing=0
fetch_one geoip.dat GeoIP.dat || missing=1
fetch_one geosite.dat GeoSite.dat || missing=1
fetch_one GeoLite2-ASN.mmdb asn.mmdb || missing=1

if [ "$missing" -eq 1 ]; then
  echo "  downloading codeload release tarball (tag: $RELEASE)..."
  http_get "$CODELOAD" "$TMP/release.tar.gz" || die "codeload download failed"
  tar -xzf "$TMP/release.tar.gz" -C "$TMP" || die "tarball extraction failed"
  SRC_DIR="$TMP/meta-rules-dat-${RELEASE}"
  [ -d "$SRC_DIR" ] || die "tarball has no meta-rules-dat-${RELEASE}/ directory"
  cp -f "$SRC_DIR/geoip.dat" "$GEO_HOST/GeoIP.dat"
  cp -f "$SRC_DIR/geosite.dat" "$GEO_HOST/GeoSite.dat"
  cp -f "$SRC_DIR/GeoLite2-ASN.mmdb" "$GEO_HOST/asn.mmdb"
  echo "  fetched core trio from codeload tarball"
fi

for f in GeoIP.dat GeoSite.dat asn.mmdb; do
  [ -s "$GEO_HOST/$f" ] || die "host/geo/$f is missing or empty after download"
  cp -f "$GEO_HOST/$f" "$GEO_POC/$f"
done
echo "  mirrored core trio to poc/geo/"

if [ "$FULL" -eq 1 ]; then
  OUT="$REPO_ROOT/poc/meta-rules-dat-release"
  if [ -d "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then
    echo "  keep existing full bundle: $OUT"
  else
    [ -f "$TMP/release.tar.gz" ] || {
      http_get "$CODELOAD" "$TMP/release.tar.gz" || die "codeload download failed"
    }
    tar -xzf "$TMP/release.tar.gz" -C "$TMP" || die "tarball extraction failed"
    SRC_DIR="$TMP/meta-rules-dat-${RELEASE}"
    [ -d "$SRC_DIR" ] || die "tarball has no meta-rules-dat-${RELEASE}/ directory"
    mkdir -p "$OUT"
    cp -R "$SRC_DIR/." "$OUT/"
    echo "  fetched full release bundle -> $OUT"
  fi
fi

echo "fetch-geo-data: done"
