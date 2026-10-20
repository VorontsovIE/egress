#!/usr/bin/env bash
# Remove the retired privileged host TUN helper after its jobs have drained.
set -euo pipefail
UNIT=vibelab-egress-tun-helper.service
STATE_DIR=/run/vibelab-egress/jobs
BINARY_DIR=/usr/local/libexec/vibelab-egress
[ "$(id -u)" = 0 ] || { echo 'remove-legacy-tun-helper: run as root' >&2; exit 3; }

# Stop the host entry point before inspecting its state directory. This closes
# the check/remove race: no new helper run can be accepted after the scan.
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "$UNIT" --no-legend 2>/dev/null | grep -q "$UNIT"; then
  systemctl stop "$UNIT"
fi
if [ -d "$STATE_DIR" ] && find "$STATE_DIR" -mindepth 2 -maxdepth 2 -name state.json -print -quit | grep -q .; then
  echo "remove-legacy-tun-helper: helper is stopped; active or stale TUN job state exists under $STATE_DIR; inspect/drain jobs, then retry" >&2
  exit 4
fi

if command -v docker >/dev/null 2>&1 && docker container inspect vibelab-egress-tun-helper >/dev/null 2>&1; then
  running="$(docker inspect --format '{{.State.Running}}' vibelab-egress-tun-helper)"
  if [ "$running" = true ]; then
    # Freeze the legacy container so no new job can appear between inspecting
    # its state and removing it. docker exec is unavailable on paused
    # containers, so inspect the frozen state via docker cp. If it is busy,
    # resume it and leave it intact.
    docker pause vibelab-egress-tun-helper >/dev/null
    tmpdir="$(mktemp -d)"
    mkdir "$tmpdir/jobs"
    if ! docker cp vibelab-egress-tun-helper:/run/vibelab-egress/jobs/. "$tmpdir/jobs/" >/dev/null 2>&1; then
      rm -rf "$tmpdir"
      docker unpause vibelab-egress-tun-helper >/dev/null
      echo 'remove-legacy-tun-helper: cannot inspect legacy container jobs; resumed it and refused removal' >&2
      exit 4
    fi
    if find "$tmpdir/jobs" -mindepth 2 -maxdepth 2 -name state.json -print -quit | grep -q .; then
      rm -rf "$tmpdir"
      docker unpause vibelab-egress-tun-helper >/dev/null
      echo 'remove-legacy-tun-helper: legacy container has active or stale jobs; resumed it and refused removal' >&2
      exit 4
    fi
    rm -rf "$tmpdir"
    # No job was active while the container was frozen, so force removal is
    # safe and avoids briefly reopening its request path via docker unpause.
    docker rm --force vibelab-egress-tun-helper >/dev/null
  else
    docker rm vibelab-egress-tun-helper >/dev/null
  fi
fi
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files "$UNIT" --no-legend 2>/dev/null | grep -q "$UNIT"; then
  systemctl disable "$UNIT" >/dev/null 2>&1 || true
fi
rm -f "/etc/systemd/system/$UNIT" "/usr/lib/systemd/system/$UNIT" "/lib/systemd/system/$UNIT"
if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload; fi
rm -f "$BINARY_DIR/egress-tun-helper.py" "$BINARY_DIR/native_netns.py" "$BINARY_DIR/exec_as_caller.py"
rm -f /run/vibelab-egress/tun-helper.sock
if getent group vibelab-egress-tun >/dev/null 2>&1; then groupdel vibelab-egress-tun; fi
echo 'remove-legacy-tun-helper: retired helper service and files removed; client and backend state preserved'
