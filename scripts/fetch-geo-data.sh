#!/bin/sh
# fetch-geo-data.sh — download GEO datasets (spec 14/41).
#
# GEO data is NOT stored in git (stripped from history in commit 1c9d3e3).
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
#   sh scripts/fetch-geo-data.sh --full         # + all release assets ->
#                                               #   poc/meta-rules-dat-release/ (~70 MB)
#   sh scripts/fetch-geo-data.sh --release TAG  # pin a release tag (default: latest)
#   sh scripts/fetch-geo-data.sh --check        # verify host/geo vs poc/geo sync
#
# Exit codes: 0 ok, 2 usage, 3 environment/download error.
#
# Source: GitHub release assets ONLY (same URLs as egressd geo_update.py /
# mihomo geox-url). github.com redirects the download to
# objects.githubusercontent.com — if that host is blocked on your network
# there is NO codeload alternative: upstream does not commit the .dat/.mmdb
# files to git (the only tag `latest` is a 20-file source snapshot without
# them, verified 2026-10-15). In that case seed manually: place
# geoip.dat/geosite.dat/GeoLite2-ASN.mmdb into host/geo/ as
# GeoIP.dat/GeoSite.dat/asn.mmdb and re-run (existing files are kept), or
# skip seeding entirely — GEO is optional at install time, mihomo downloads
# it at runtime via geox-url (same host requirement).
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

UPSTREAM="https://github.com/MetaCubeX/meta-rules-dat/releases/download/${RELEASE}"
GEO_HOST="$REPO_ROOT/host/geo"
GEO_POC="$REPO_ROOT/poc/geo"

die() { echo "fetch-geo-data: $*" >&2; exit 3; }

die_download() { # failed_upstream_name
  cat >&2 <<EOS
fetch-geo-data: GEO download failed for '${1}'.

  The release assets are served from objects.githubusercontent.com (via the
  github.com redirect) and that host is unreachable from this machine.
  There is no codeload fallback: upstream does not commit the .dat/.mmdb
  files to git, so no git-based mirror of them exists.

  Options:
    1. seed manually: put the three files into host/geo/ as
         GeoIP.dat  GeoSite.dat  asn.mmdb
       and re-run this script (existing files are kept);
    2. skip seeding: GEO is optional at install time — mihomo downloads it
       at runtime via geox-url (same host requirement applies).

  Diagnose connectivity with:
    curl -sIL -o /dev/null -w '%{http_code}\\n' '${UPSTREAM}/${1}'
EOS
  exit 3
}

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

mkdir -p "$GEO_HOST" "$GEO_POC"

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
  echo "  direct release URL failed for $1"
  die_download "$1"
}

fetch_one geoip.dat GeoIP.dat
fetch_one geosite.dat GeoSite.dat
fetch_one GeoLite2-ASN.mmdb asn.mmdb

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
    command -v python3 >/dev/null 2>&1 || die "python3 is required for --full"
    API="https://api.github.com/repos/MetaCubeX/meta-rules-dat/releases"
    if [ "$RELEASE" = "latest" ]; then
      API="${API}/latest"
    else
      API="${API}/tags/${RELEASE}"
    fi
    http_get "$API" "$OUT.api.json.tmp" 2>/dev/null || die_download "release asset list (api.github.com)"
    mkdir -p "$OUT"
    python3 - "$OUT.api.json.tmp" "$OUT" <<'PYEOF'
import json, os, sys, subprocess
meta_path, out_dir = sys.argv[1], sys.argv[2]
with open(meta_path, encoding="utf-8") as fh:
    assets = json.load(fh).get("assets") or []
if not assets:
    sys.exit("fetch-geo-data: no assets in the release response")
for asset in assets:
    name = asset["name"]
    url = asset["browser_download_url"]
    dest = os.path.join(out_dir, name)
    if os.path.exists(dest) and os.path.getsize(dest) == asset.get("size", 0):
        print(f"  keep {name}")
        continue
    tmp = dest + ".tmp"
    print(f"  fetching {name} ({asset.get('size', 0)} bytes)")
    if subprocess.run(["curl", "-fL", "--connect-timeout", "15", "--retry", "2", "-o", tmp, url]).returncode != 0:
        subprocess.run(["rm", "-f", tmp])
        sys.exit(f"fetch-geo-data: asset download failed: {url}")
    os.replace(tmp, dest)
PYEOF
    rm -f "$OUT.api.json.tmp"
    echo "  fetched full release bundle -> $OUT"
  fi
fi

echo "fetch-geo-data: done"
