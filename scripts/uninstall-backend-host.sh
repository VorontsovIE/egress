#!/usr/bin/env bash
# uninstall-backend-host.sh — remove the egress backend (rollback).
#
# Stops and removes the compose stack, disables host units, and optionally
# removes the persistent root, backend secrets, and host client install.
# The persistent root (mihomo state, metrics, GEO) is KEPT by default so a
# reinstall restores client selections; pass --remove-data to wipe it.
#
# Usage:
#   uninstall-backend-host.sh [--root <persistent-root>] [--secrets-dir <dir>] \
#       [--remove-data] [--remove-client] [--yes]
#
# Exit codes: 0 ok, 2 usage, 3 environment error.
set -euo pipefail

ROOT="${EGRESS_ROOT:-/var/lib/egress}"
SECRETS_DIR="${EGRESS_SECRETS:-/etc/egress/secrets}"
REMOVE_DATA=0
REMOVE_CLIENT=0
ASSUME_YES=0
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

die_usage() { echo "uninstall-backend: $*" >&2; exit 2; }
die_env()   { echo "uninstall-backend: $*" >&2; exit 3; }
say()       { echo "[uninstall-backend] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --root)          ROOT="${2:?}"; shift 2 ;;
    --secrets-dir)   SECRETS_DIR="${2:?}"; shift 2 ;;
    --remove-data)   REMOVE_DATA=1; shift ;;
    --remove-client) REMOVE_CLIENT=1; shift ;;
    --yes|-y)        ASSUME_YES=1; shift ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

[ "$(id -u)" = "0" ] || die_env "must run as root"

confirm() {
  [ "$ASSUME_YES" = "1" ] && return 0
  read -r -p "$1 [y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

# 1. stop + remove the stack
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  say "stopping compose stack"
  (cd "$REPO_ROOT" && docker compose down --remove-orphans 2>/dev/null) || true
  say "removing built egressd image"
  docker rmi egress-egressd:local 2>/dev/null || true
else
  say "docker not available; skipping stack removal"
fi

# 2. host units
if command -v systemctl >/dev/null; then
  for u in egress-cidr-sync.timer egress-cidr-sync.service \
           egress-devbridge.service; do
    systemctl disable --now "$u" 2>/dev/null || true
    rm -f "/etc/systemd/system/$u"
  done
  systemctl daemon-reload 2>/dev/null || true
  say "host units removed"
fi

# 3. host client install
if [ "$REMOVE_CLIENT" = "1" ]; then
  say "removing host client install"
  rm -f /etc/egress/client.yaml /etc/egress/client.token \
    /etc/egress/default-proxy.auth
  rm -rf /usr/local/lib/egress
  rm -f /usr/local/bin/egressctl /usr/local/bin/egress-run
else
  say "host client install kept (use --remove-client to remove)"
fi

# 4. remove runtime credentials but keep provider subscription inputs.
if [ -d "$SECRETS_DIR" ]; then
  if [ "$REMOVE_DATA" = "1" ]; then
    if confirm "REMOVE backend secrets and provider subscriptions at $SECRETS_DIR?"; then
      rm -rf "$SECRETS_DIR"
      say "backend secrets and provider subscriptions removed"
    else
      say "backend secrets and provider subscriptions kept"
    fi
  else
    rm -f "$SECRETS_DIR/mihomo_api_secret" "$SECRETS_DIR/devbridge.token"
    rm -rf "$SECRETS_DIR/clients"
    say "runtime credentials removed; provider subscription inputs kept"
  fi
fi

# 5. persistent data (kept by default)
if [ "$REMOVE_DATA" = "1" ] && [ -d "$ROOT" ]; then
  if confirm "REMOVE persistent data at $ROOT (mihomo state, metrics, GEO)?"; then
    rm -rf "$ROOT"
    say "persistent data removed"
  else
    say "persistent data kept"
  fi
else
  say "persistent data kept at $ROOT (use --remove-data to wipe)"
fi

say "uninstall complete"
