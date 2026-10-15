#!/bin/sh
# fetch-poc-vendor.sh — download POC vendor sources (not in git since 2026-10-15).
#
# The heavy third-party content was stripped from the repository history to
# keep the GitHub clone small. This script restores the two vendor sources
# the Phase-0 POC builds need, pinned by sha256:
#
#   poc/mihomo-src.tar.gz                 MetaCubeX/mihomo v1.19.32 source (cache)
#   poc/mihomo-src/                       extracted copy (build root)
#   poc/wgserver/third_party/wireguard-go-a6cecdd7f57f.tar.gz   (cache)
#                                         MetaCubeX/wireguard-go @ a6cecdd7f57f
#   poc/wgserver/third_party/wireguard-go/
#                                         extracted copy + the 1-line patch
#                                         third_party/wireguard-go-sticky-sockets.patch
#                                         (kept in git; see DEVELOPMENT.md)
#
# Build recipes for the three POC binaries (poc/mihomo, poc/mihomo-gvisor,
# poc/wgserver/wgserver) are in DEVELOPMENT.md, section «Сборка (редко)».
#
# Usage:
#   sh scripts/fetch-poc-vendor.sh     # fetch missing parts (idempotent;
#                                      #   works offline if all trees are present)
#   sh scripts/fetch-poc-vendor.sh --check
#                                      # verify the extracted trees (and tarball
#                                      #   caches when present); rc 0 ok / 3 mismatch
#
# Exit codes: 0 ok, 2 usage, 3 environment/download/check error.
#
# Sources (codeload only — objects.githubusercontent.com is blocked in the
# dev container, codeload is not; both hosts work on the target host):
#   https://codeload.github.com/MetaCubeX/mihomo/tar.gz/refs/tags/v1.19.32
#   https://codeload.github.com/MetaCubeX/wireguard-go/tar.gz/a6cecdd7f57f
#
# Pinned sha256 (verified against upstream on 2026-10-15):
#   ab130b7fab3893d01aa44c0d39be34a26da716c1d36832fbac9fa054a1d862a0  mihomo-src.tar.gz
#   5ce5456f575a73be6331224fe1d6423cdb166141a60f474da166d89b6e276588  wireguard-go-a6cecdd7f57f.tar.gz
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK=0

if [ "$#" -gt 0 ]; then
  if [ "$1" = "--check" ]; then
    CHECK=1
  else
    echo "fetch-poc-vendor: unknown argument: $1" >&2
    exit 2
  fi
fi

MIHOMO_TAG="v1.19.32"
MIHOMO_TARBALL="$REPO_ROOT/poc/mihomo-src.tar.gz"
MIHOMO_DIR="$REPO_ROOT/poc/mihomo-src"
MIHOMO_SHA256="ab130b7fab3893d01aa44c0d39be34a26da716c1d36832fbac9fa054a1d862a0"
MIHOMO_URL="https://codeload.github.com/MetaCubeX/mihomo/tar.gz/refs/tags/${MIHOMO_TAG}"

WGGO_SHA="a6cecdd7f57f"
WGGO_TARBALL="$REPO_ROOT/poc/wgserver/third_party/wireguard-go-${WGGO_SHA}.tar.gz"
WGGO_DIR="$REPO_ROOT/poc/wgserver/third_party/wireguard-go"
WGGO_PATCH="$REPO_ROOT/poc/wgserver/third_party/wireguard-go-sticky-sockets.patch"
WGGO_SHA256="5ce5456f575a73be6331224fe1d6423cdb166141a60f474da166d89b6e276588"
WGGO_URL="https://codeload.github.com/MetaCubeX/wireguard-go/tar.gz/${WGGO_SHA}"
WGGO_MARKER='const StdNetSupportsStickySockets = false'

die() { echo "fetch-poc-vendor: $*" >&2; exit 3; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

hash_ok() { # file expected
  if [ ! -f "$1" ]; then
    return 1
  fi
  [ "$(sha256_of "$1")" = "$2" ]
}

download() { # url dest
  if ! curl -fsSL --retry 3 -o "$2.tmp" "$1"; then
    rm -f "$2.tmp"
    die "download failed: $1"
  fi
  mv "$2.tmp" "$2"
}

extract_tree() { # tarball dest_dir   (tarball holds a single top-level dir)
  rm -rf "$2"
  mkdir -p "$2"
  tar xzf "$1" -C "$2" --strip-components=1
}

tree_matches_tarball() { # tarball dir
  tmpdir="$(mktemp -d)"
  mkdir -p "$tmpdir/src"
  if ! tar xzf "$1" -C "$tmpdir/src" --strip-components=1 2>/dev/null; then
    rm -rf "$tmpdir"
    return 1
  fi
  rc=0
  diff -rq "$tmpdir/src" "$2" >/dev/null 2>&1 || rc=1
  rm -rf "$tmpdir"
  return "$rc"
}

ensure_mihomo_src() {
  if [ -d "$MIHOMO_DIR" ] && [ ! -f "$MIHOMO_TARBALL" ]; then
    # tree from an earlier verified run, no cache to compare against
    return 0
  fi
  if [ -f "$MIHOMO_TARBALL" ]; then
    if hash_ok "$MIHOMO_TARBALL" "$MIHOMO_SHA256" \
       && [ -d "$MIHOMO_DIR" ] \
       && tree_matches_tarball "$MIHOMO_TARBALL" "$MIHOMO_DIR"; then
      return 0
    fi
    if [ "$CHECK" -eq 1 ]; then
      die "poc/mihomo-src.tar.gz sha256 mismatch or poc/mihomo-src differs from it"
    fi
  fi
  if [ "$CHECK" -eq 1 ]; then
    die "poc/mihomo-src absent — run: sh scripts/fetch-poc-vendor.sh"
  fi
  if ! hash_ok "$MIHOMO_TARBALL" "$MIHOMO_SHA256"; then
    download "$MIHOMO_URL" "$MIHOMO_TARBALL"
    hash_ok "$MIHOMO_TARBALL" "$MIHOMO_SHA256" \
      || die "poc/mihomo-src.tar.gz sha256 mismatch after download"
  fi
  extract_tree "$MIHOMO_TARBALL" "$MIHOMO_DIR"
}

ensure_wireguard_go() {
  if [ ! -f "$WGGO_PATCH" ]; then
    die "patch file missing: $WGGO_PATCH (it is tracked in git — incomplete checkout)"
  fi
  if [ -d "$WGGO_DIR" ] && grep -q "$WGGO_MARKER" "$WGGO_DIR/conn/sticky_linux.go"; then
    if [ -f "$WGGO_TARBALL" ] && ! hash_ok "$WGGO_TARBALL" "$WGGO_SHA256"; then
      if [ "$CHECK" -eq 1 ]; then
        die "wireguard-go tarball cache sha256 mismatch"
      fi
      rm -f "$WGGO_TARBALL"
    fi
    return 0
  fi
  if [ "$CHECK" -eq 1 ]; then
    die "poc/wgserver/third_party/wireguard-go absent or vendor patch not applied — run: sh scripts/fetch-poc-vendor.sh"
  fi
  if ! hash_ok "$WGGO_TARBALL" "$WGGO_SHA256"; then
    mkdir -p "$(dirname "$WGGO_TARBALL")"
    download "$WGGO_URL" "$WGGO_TARBALL"
    hash_ok "$WGGO_TARBALL" "$WGGO_SHA256" \
      || die "wireguard-go tarball sha256 mismatch after download"
  fi
  extract_tree "$WGGO_TARBALL" "$WGGO_DIR"
  if ! command -v patch >/dev/null 2>&1; then
    die "patch(1) is required to apply the vendor patch"
  fi
  if ! (cd "$WGGO_DIR" && patch -p1 -N < "$WGGO_PATCH"); then
    die "failed to apply $WGGO_PATCH"
  fi
}

if [ "$CHECK" -eq 1 ]; then
  rc=0
  ensure_mihomo_src || rc=3
  ensure_wireguard_go || rc=3
  if [ "$rc" -eq 0 ]; then
    echo "fetch-poc-vendor: check ok (mihomo ${MIHOMO_TAG}, wireguard-go @ ${WGGO_SHA} + patch)"
  fi
  exit "$rc"
fi

if ! command -v curl >/dev/null 2>&1; then
  die "curl is required"
fi
ensure_mihomo_src
ensure_wireguard_go
echo "fetch-poc-vendor: ok (mihomo ${MIHOMO_TAG} -> poc/mihomo-src, wireguard-go @ ${WGGO_SHA} + patch -> poc/wgserver/third_party/wireguard-go)"
