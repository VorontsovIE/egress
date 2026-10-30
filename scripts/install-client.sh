#!/usr/bin/env bash
# install-client.sh — install the client package into one execution
# environment (spec section 32, step B).
#
# The SAME package (egressctl + egress-run, stdlib-only) is installed
# everywhere; only the config/secret layout differs per environment.
#
# Targets:
#   host           -> /etc/egress/ (client.yaml + secret files)
#                     and the package under /usr/local/lib/egress
#                     with wrappers in /usr/local/bin
#   container -> a reproducible bundle dir (baked into the image or
#                     mounted read-only per spec section 32); this script
#                     stages the bundle and, when run INSIDE the container,
#                     installs from the bundle into the container's
#                     /etc/egress + /run/secrets (if writable).
#
# Usage:
#   install-client.sh --client <id> --target <host|container> \
#       --bundle <bundle-dir> [--install-root <prefix>]
#
# Exit codes: 0 ok, 2 usage, 3 environment error.
set -euo pipefail

CLIENT=""
TARGET=""
BUNDLE=""
INSTALL_ROOT="${EGRESS_INSTALL_ROOT:-/usr/local}"

die_usage() { echo "install-client: $*" >&2; exit 2; }
die_env()   { echo "install-client: $*" >&2; exit 3; }

while [ $# -gt 0 ]; do
  case "$1" in
    --client)       CLIENT="${2:?}"; shift 2 ;;
    --target)       TARGET="${2:?}"; shift 2 ;;
    --bundle)       BUNDLE="${2:?}"; shift 2 ;;
    --install-root) INSTALL_ROOT="${2:?}"; shift 2 ;;
    -h|--help)      grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

[ -n "$CLIENT" ] || die_usage "--client is required"
[ -n "$TARGET" ] || die_usage "--target is required"
[ -n "$BUNDLE" ] || die_usage "--bundle is required"
[ -d "$BUNDLE" ] || die_env "bundle dir not found: $BUNDLE"
[ -f "$BUNDLE/client.yaml" ] || die_env "bundle missing client.yaml: $BUNDLE"
[ -f "$BUNDLE/client.token" ] || die_env "bundle missing client.token: $BUNDLE"
[ -f "$BUNDLE/default-proxy.auth" ] || die_env "bundle missing default-proxy.auth: $BUNDLE"

# sanity: bundle client_id must match the requested client
BUNDLE_ID="$(sed -n 's/^client_id:[[:space:]]*//p' "$BUNDLE/client.yaml" | head -1)"
if [ -n "$BUNDLE_ID" ] && [ "$BUNDLE_ID" != "$CLIENT" ]; then
  die_env "bundle client_id=$BUNDLE_ID does not match --client=$CLIENT"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_SRC="$REPO_ROOT/client/src/egress_client"
[ -d "$PKG_SRC" ] || die_env "client package source not found: $PKG_SRC"

install_package() { # prefix
  local prefix="$1"
  local libdir="$prefix/lib/egress"
  local bindir="$prefix/bin"
  mkdir -p "$libdir" "$bindir"
  local wheel_dir="$REPO_ROOT/release-assets"
  local wheel=""
  if [ -d "$wheel_dir" ]; then
    set -- "$wheel_dir"/egress_client-*-py3-none-any.whl
    [ -f "$1" ] && [ "$#" -eq 1 ] && wheel="$1"
  fi
  if [ -z "$wheel" ] && [ -d "$REPO_ROOT/client/dist" ]; then
    set -- "$REPO_ROOT"/client/dist/egress_client-*-py3-none-any.whl
    [ -f "$1" ] && [ "$#" -eq 1 ] && wheel="$1"
  fi

  if [ -n "$wheel" ]; then
    local version target current_tmp
    version="${wheel##*/egress_client-}"
    version="${version%-py3-none-any.whl}"
    target="$libdir/versions/$version"
    mkdir -p "$libdir/versions"
    if [ ! -d "$target/venv" ]; then
      mkdir -p "$target"
      python3 -m venv "$target/venv"
      "$target/venv/bin/python" -m pip install --no-index --no-deps "$wheel"
    fi
    local engine=""
    if [ "$(sed -n 's/^tun_backend:[[:space:]]*//p' "$BUNDLE/client.yaml" | head -1)" = local-userns ]; then
      case "$(uname -m)" in
        x86_64|amd64) engine="$BUNDLE/tun-engine/egress-tun-engine-linux-amd64" ;;
        aarch64|arm64) engine="$BUNDLE/tun-engine/egress-tun-engine-linux-arm64" ;;
        *) die_env "local-userns TUN has no engine build for $(uname -m)" ;;
      esac
      [ -f "$engine" ] || die_env "bundle has no matching TUN engine"
      (cd "$BUNDLE/tun-engine" && sha256sum -c SHA256SUMS >/dev/null) || die_env "TUN engine checksum verification failed"
      install -m 0755 "$engine" "$target/egress-tun-engine"
    fi
    current_tmp="$libdir/.current-new"
    rm -f "$current_tmp"
    ln -s "versions/$version" "$current_tmp"
    mv -Tf "$current_tmp" "$libdir/current"
    cat > "$bindir/egressctl" <<EOF
#!/usr/bin/env bash
exec "$libdir/current/venv/bin/egressctl" "\$@"
EOF
    cat > "$bindir/egress-run" <<EOF
#!/usr/bin/env bash
export EGRESS_TUN_ENGINE="$libdir/current/egress-tun-engine"
exec "$libdir/current/venv/bin/egress-run" "\$@"
EOF
  elif [ "${EGRESS_CLIENT_SOURCE_INSTALL:-0}" = 1 ] || [ "${EGRESS_TEST_MODE:-0}" = 1 ]; then
    # Explicit dev/test fallback for a checkout without release wheel assets.
    cp -r "$PKG_SRC" "$libdir/"
    cp -r "$REPO_ROOT/adapters" "$libdir/"
    chmod -R a+rX "$libdir/egress_client" "$libdir/adapters"
    cat > "$bindir/egressctl" <<EOF
#!/usr/bin/env bash
exec python3 -c "import sys; sys.path.insert(0, '$libdir'); from egress_client.egressctl import main; main()" "\$@"
EOF
    cat > "$bindir/egress-run" <<EOF
#!/usr/bin/env bash
export EGRESS_TUN_ENGINE="$prefix/libexec/egress/egress-tun-engine"
exec python3 -c "import sys; sys.path.insert(0, '$libdir'); from egress_client.egress_run import main; main()" "\$@"
EOF
  else
    die_env "release wheel missing; build a wheel or set EGRESS_CLIENT_SOURCE_INSTALL=1 for an explicit source install"
  fi

  local engine=""
  if [ "$(sed -n 's/^tun_backend:[[:space:]]*//p' "$BUNDLE/client.yaml" | head -1)" = local-userns ]; then
    case "$(uname -m)" in
      x86_64|amd64) engine="$BUNDLE/tun-engine/egress-tun-engine-linux-amd64" ;;
      aarch64|arm64) engine="$BUNDLE/tun-engine/egress-tun-engine-linux-arm64" ;;
      *) die_env "local-userns TUN has no engine build for $(uname -m)" ;;
    esac
    [ -f "$engine" ] || die_env "bundle has no matching TUN engine; rebuild bundle with scripts/build-tun-engine.sh"
    (cd "$BUNDLE/tun-engine" && sha256sum -c SHA256SUMS >/dev/null) || die_env "TUN engine checksum verification failed"
    mkdir -p "$prefix/libexec/egress"
    install -m 755 "$engine" "$prefix/libexec/egress/egress-tun-engine"
  fi
  chmod 755 "$bindir/egressctl" "$bindir/egress-run"
}

install_config() { # etc_dir
  local etc="$1"
  mkdir -p "$etc"
  umask 077
  cp "$BUNDLE/client.yaml" "$etc/client.yaml"
  cp "$BUNDLE/client.token" "$etc/client.token"
  cp "$BUNDLE/default-proxy.auth" "$etc/default-proxy.auth"
  chmod 700 "$etc"
  chmod 600 "$etc/client.token" "$etc/default-proxy.auth"
  chmod 644 "$etc/client.yaml"
}

case "$TARGET" in
  host)
    install_package "$INSTALL_ROOT"
    install_config /etc/egress
    # The host CLI is invoked by the human who ran sudo, not by root. Give
    # only that account access to its two 0600 client credentials. Keep the
    # directory root-owned so the user cannot replace backend secrets below
    # /etc/egress/secrets or redirect other config paths. Enrollment
    # runs as egressd and needs traverse access to its own secrets.
    HOST_USER="${EGRESS_HOST_USER:-${SUDO_USER:-}}"
    if [ -n "$HOST_USER" ] && [ "$HOST_USER" != root ]; then
      id -u -- "$HOST_USER" >/dev/null 2>&1 || die_env "unknown host user: $HOST_USER"
      HOST_GROUP="$(id -gn -- "$HOST_USER")"
      chgrp -- "$HOST_GROUP" /etc/egress
      chown -- "$HOST_USER" /etc/egress/client.token \
        /etc/egress/default-proxy.auth
      chmod 600 /etc/egress/client.token \
        /etc/egress/default-proxy.auth
      HOST_HOME="$(getent passwd "$HOST_USER" | cut -d: -f6)"
      if [ -n "$HOST_HOME" ] && [ -d "$HOST_HOME" ]; then
        USER_LIBEXEC="$HOST_HOME/.local/libexec/egress"
        install -d -o "$HOST_USER" -g "$HOST_GROUP" -m 0755 "$USER_LIBEXEC"
        cat > "$USER_LIBEXEC/upgrade-client" <<'EOF'
#!/bin/sh
exec /usr/local/lib/egress/current/venv/bin/python -m egress_client.updater "$@"
EOF
        cat > "$USER_LIBEXEC/uninstall-client" <<'EOF'
#!/bin/sh
set -eu
rm -rf "$HOME/.local/share/egress"
rm -f "$HOME/.local/bin/egressctl" "$HOME/.local/bin/egress-run"
echo "user egress client removed"
EOF
        chown "$HOST_USER:$HOST_GROUP" "$USER_LIBEXEC/upgrade-client" "$USER_LIBEXEC/uninstall-client"
        chmod 0755 "$USER_LIBEXEC/upgrade-client" "$USER_LIBEXEC/uninstall-client"
      fi
    fi
    chmod 755 /etc/egress
    echo "installed client $CLIENT (host) under $INSTALL_ROOT and /etc/egress"
    ;;
  container)
    # Inside the container: install into the container's own /etc and
    # /run/secrets. When staging from the host, set --install-root to the
    # bundle's target prefix; the image build (or a deterministic mount)
    # makes this survive container recreate (spec section 32).
    install_package "$INSTALL_ROOT"
    install_config /etc/egress
    # /run/secrets is the documented in-container secret location. In
    # production images it is provided as a read-only mount; in a plain
    # root container it does not exist at all — create it in the writable
    # rootfs so the install is self-contained (any-container support).
    # A pre-existing read-only /run/secrets is left alone: the image build
    # must provide the files there.
    # NOTE: if the image mounts a tmpfs on /run, these files do not survive
    # a container restart — use a named-volume mount or a baked image then.
    run_secrets_ok=0
    if [ -d /run/secrets ] && [ -w /run/secrets ]; then
      run_secrets_ok=1
    elif [ ! -e /run/secrets ] && [ "$(id -u)" = "0" ]; then
      mkdir -p /run/secrets
      run_secrets_ok=1
    else
      echo "note: /run/secrets not writable here; if this container uses the"
      echo "      documented /run/secrets/egress_* mounts, the image"
      echo "      build must provide them (see INSTALL.md, step B)."
    fi
    if [ "$run_secrets_ok" = "1" ]; then
      install_config /run/secrets/egress
      # the example config references /run/secrets/egress_* files;
      # provide them as symlinks to the installed copies.
      ln -sf /run/secrets/egress/client.token \
             /run/secrets/egress_client_token
      ln -sf /run/secrets/egress/default-proxy.auth \
             /run/secrets/egress_default_proxy_auth
    fi
    echo "installed client $CLIENT (container) under $INSTALL_ROOT and /etc/egress"
    ;;
  *) die_usage "--target must be host or container" ;;
esac
