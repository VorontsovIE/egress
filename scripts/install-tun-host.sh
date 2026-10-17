#!/usr/bin/env bash
# Install the optional host-native strict/TUN helper. Run on the Docker host
# as root after reviewing the checkout. No Docker socket is given to helper.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST_USER="${SUDO_USER:-}"
DOCKER_GATEWAY="${DOCKER_GATEWAY:-172.17.0.1}"
IMAGE='metacubex/mihomo:v1.19.32@sha256:bac1a74de365ea59270ba134016762c6be05654d55246518de256113bd1c225e'
MIHOMO_SHA256='6de241191ca39f9ac1cd6e3194fc9303c19055d3549c8bdb4ff1349c89173819'
LIBEXEC='/usr/local/libexec/vibelab-egress'
UNIT='/etc/systemd/system/vibelab-egress-tun-helper.service'

usage() {
  echo 'usage: install-tun-host.sh [--docker-gateway IPv4]' >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --user) HOST_USER="${2:?}"; shift 2 ;;
    --docker-gateway) DOCKER_GATEWAY="${2:?}"; shift 2 ;;
    *) usage ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo 'install-tun-host: root required' >&2; exit 3; }
if [ -n "$HOST_USER" ]; then
  id "$HOST_USER" >/dev/null 2>&1 || { echo 'install-tun-host: invalid --user' >&2; exit 2; }
fi
python3 - "$DOCKER_GATEWAY" <<'PY'
import ipaddress
import sys
try:
    ipaddress.IPv4Address(sys.argv[1])
except ValueError:
    raise SystemExit('install-tun-host: invalid Docker gateway IPv4')
PY
for tool in docker ip nft iptables setpriv systemctl sha256sum; do
  command -v "$tool" >/dev/null || { echo "install-tun-host: missing $tool" >&2; exit 3; }
done
[ -c /dev/net/tun ] || { echo 'install-tun-host: /dev/net/tun missing' >&2; exit 3; }

# Remove only the retired, exact-name Docker helper. Its container filesystem
# must never become the workload filesystem, and it must not keep advertising
# a misleading legacy capability after the native service is installed.
if docker container inspect vibelab-egress-tun-helper >/dev/null 2>&1; then
  docker rm -f vibelab-egress-tun-helper >/dev/null
fi

getent group vibelab-egress-tun >/dev/null || groupadd --system vibelab-egress-tun
getent group vibelab-egress-worker >/dev/null || groupadd --system vibelab-egress-worker
id vibelab-egress-worker >/dev/null 2>&1 || \
  useradd --system --gid vibelab-egress-worker --no-create-home \
          --shell /usr/sbin/nologin vibelab-egress-worker
# Access is now granted by the local socket's mode and SO_PEERCRED, so no
# per-account group change or new login session is required.

install -d -o root -g root -m 0755 "$LIBEXEC"
for file in egress-tun-helper.py native_netns.py exec_as_caller.py; do
  install -o root -g root -m 0755 "$REPO_ROOT/host/strict-helper/$file" "$LIBEXEC/$file"
done

tmp="$(mktemp -d)"
container=''
cleanup() {
  if [ -n "$container" ]; then docker rm "$container" >/dev/null 2>&1 || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT
docker pull "$IMAGE" >/dev/null
container="$(docker create "$IMAGE")"
docker cp "$container:/mihomo" "$tmp/mihomo"
echo "$MIHOMO_SHA256  $tmp/mihomo" | sha256sum --check --status || {
  echo 'install-tun-host: pinned Mihomo binary SHA256 mismatch' >&2
  exit 3
}
install -o root -g root -m 0755 "$tmp/mihomo" "$LIBEXEC/mihomo"

install -d -o root -g root -m 0755 /etc/vibelab-egress
printf 'VIBELAB_EGRESS_DOCKER_GATEWAY=%s\n' "$DOCKER_GATEWAY" \
  > /etc/vibelab-egress/tun-host.env
chmod 0644 /etc/vibelab-egress/tun-host.env
install -o root -g root -m 0644 \
  "$REPO_ROOT/host/units/vibelab-egress-tun-helper.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now vibelab-egress-tun-helper.service
systemctl restart vibelab-egress-tun-helper.service
systemctl is-active --quiet vibelab-egress-tun-helper.service || {
  echo 'install-tun-host: helper failed to start; inspect journalctl -u vibelab-egress-tun-helper' >&2
  exit 4
}
echo "install-tun-host: installed; every local UID can use the socket"
