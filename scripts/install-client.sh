#!/usr/bin/env bash
# install-client.sh — install the client package into one execution
# environment (spec section 32, step B).
#
# The SAME package (egressctl + egress-run, stdlib-only) is installed
# everywhere; only the config/secret layout differs per environment.
#
# Targets:
#   host           -> /etc/vibelab-egress/ (client.yaml + secret files)
#                     and the package under /usr/local/lib/vibelab-egress
#                     with wrappers in /usr/local/bin
#   gvisor-container -> a reproducible bundle dir (baked into the image or
#                     mounted read-only per spec section 32); this script
#                     stages the bundle and, when run INSIDE the container,
#                     installs from the bundle into the container's
#                     /etc/vibelab-egress + /run/secrets (if writable).
#
# Usage:
#   install-client.sh --client <id> --target <host|gvisor-container> \
#       --bundle <bundle-dir> [--install-root <prefix>]
#
# Exit codes: 0 ok, 2 usage, 3 environment error.
set -euo pipefail

CLIENT=""
TARGET=""
BUNDLE=""
INSTALL_ROOT="${VIBELAB_EGRESS_INSTALL_ROOT:-/usr/local}"

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
PKG_SRC="$REPO_ROOT/client/src/vibelab_egress_client"
[ -d "$PKG_SRC" ] || die_env "client package source not found: $PKG_SRC"

install_package() { # prefix
  local prefix="$1"
  local libdir="$prefix/lib/vibelab-egress"
  local bindir="$prefix/bin"
  mkdir -p "$libdir" "$bindir"
  # Copy the package DIRECTORY (not its contents) so that
  # $libdir/vibelab_egress_client/ exists and the wrapper's
  #   sys.path.insert(0, '$libdir'); from vibelab_egress_client.egressctl import main
  # resolves. (Copying `$PKG_SRC/.` flattens the module one level up and
  # breaks the import.)
  cp -r "$PKG_SRC" "$libdir/"
  # cp -r propagates the SOURCE directory's mode (the repo source tree is
  # 750 root:group), which would leave the installed package dir unreadable
  # by the user running egressctl -> "No module named ...egressctl". The
  # package is plain stdlib Python source (no secrets), so normalize the
  # installed tree to be world-readable + traversable.
  chmod 755 "$libdir/vibelab_egress_client"
  chmod -R a+rX "$libdir/vibelab_egress_client"
  # wrappers: fixed python3 invocation, no build step needed (stdlib-only)
  cat > "$bindir/egressctl" <<EOF
#!/usr/bin/env bash
exec python3 -c "import sys; sys.path.insert(0, '$libdir'); \
from vibelab_egress_client.egressctl import main; main()" "\$@"
EOF
  cat > "$bindir/egress-run" <<EOF
#!/usr/bin/env bash
exec python3 -c "import sys; sys.path.insert(0, '$libdir'); \
from vibelab_egress_client.egress_run import main; main()" "\$@"
EOF
  chmod 755 "$bindir/egressctl" "$bindir/egress-run"
  chmod 755 "$libdir"
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
    install_config /etc/vibelab-egress
    echo "installed client $CLIENT (host) under $INSTALL_ROOT and /etc/vibelab-egress"
    ;;
  gvisor-container)
    # Inside the container: install into the container's own /etc and
    # /run/secrets. When staging from the host, set --install-root to the
    # bundle's target prefix; the image build (or a deterministic mount)
    # makes this survive container recreate (spec section 32).
    install_package "$INSTALL_ROOT"
    install_config /etc/vibelab-egress
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
      echo "      documented /run/secrets/vibelab_egress_* mounts, the image"
      echo "      build must provide them (see INSTALL.md, step B)."
    fi
    if [ "$run_secrets_ok" = "1" ]; then
      install_config /run/secrets/vibelab-egress
      # the example config references /run/secrets/vibelab_egress_* files;
      # provide them as symlinks to the installed copies.
      ln -sf /run/secrets/vibelab-egress/client.token \
             /run/secrets/vibelab_egress_client_token
      ln -sf /run/secrets/vibelab-egress/default-proxy.auth \
             /run/secrets/vibelab_egress_default_proxy_auth
    fi
    echo "installed client $CLIENT (gvisor-container) under $INSTALL_ROOT and /etc/vibelab-egress"
    ;;
  *) die_usage "--target must be host or gvisor-container" ;;
esac
