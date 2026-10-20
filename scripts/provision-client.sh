#!/usr/bin/env bash
# provision-client.sh — generate a client bundle for one client profile.
#
# Host-side bootstrap operation (spec section 32, 37). Produces, per client:
#   bundle dir (default: <out>/<client_id>/):
#     client.yaml            environment-specific config (no secrets inline)
#     client.token           API token (0600)
#     default-proxy.auth     SOCKS default-proxy credential user:password (0600)
#   backend secrets dir (default: /etc/vibelab-egress/secrets):
#     clients/<id>.token         same API token (egressd auth registry)
#     clients/<id>.profile.json  non-secret profile metadata
#     clients/<id>.proxy-creds   proxy credential (config generator input;
#                                NEVER placed in any client bundle)
#
# The same script provisions host and container clients — only the
# environment label and paths differ. No provider/VPN secrets are ever
# generated or copied here.
#
# Usage:
#   provision-client.sh --client <id> --environment <host|container> \
#       [--out <bundle-root>] [--secrets-dir <dir>] [--control-url <url>] \
#       [--proxy-host <host>] [--default-port <p>] [--oneshot-port <p>] \
#       [--tun-backend <none|local-userns>] [--tun-dns <ip,ip,...>]
#
# Exit codes: 0 ok, 2 usage, 3 environment error.
set -euo pipefail

CLIENT=""
ENVIRONMENT=""
OUT="${VIBELAB_EGRESS_BUNDLE_ROOT:-/var/lib/vibelab-egress/client-bundles}"
SECRETS_DIR="${VIBELAB_EGRESS_SECRETS:-/etc/vibelab-egress/secrets}"
CONTROL_URL=""
PROXY_HOST=""
DEFAULT_PORT=1080
ONESHOT_PORT=1081
TUN_BACKEND=""
TUN_DNS="1.1.1.1,1.0.0.1"

die_usage() { echo "provision-client: $*" >&2; exit 2; }
die_env()   { echo "provision-client: $*" >&2; exit 3; }

while [ $# -gt 0 ]; do
  case "$1" in
    --client)        CLIENT="${2:?}"; shift 2 ;;
    --environment)   ENVIRONMENT="${2:?}"; shift 2 ;;
    --out)           OUT="${2:?}"; shift 2 ;;
    --secrets-dir)   SECRETS_DIR="${2:?}"; shift 2 ;;
    --control-url)   CONTROL_URL="${2:?}"; shift 2 ;;
    --proxy-host)    PROXY_HOST="${2:?}"; shift 2 ;;
    --default-port)  DEFAULT_PORT="${2:?}"; shift 2 ;;
    --oneshot-port)  ONESHOT_PORT="${2:?}"; shift 2 ;;
    --tun-backend)   TUN_BACKEND="${2:?}"; shift 2 ;;
    --tun-dns)       TUN_DNS="${2:?}"; shift 2 ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

[ -n "$CLIENT" ] || die_usage "--client is required"
[ -n "$ENVIRONMENT" ] || die_usage "--environment is required"
case "$ENVIRONMENT" in
  host)
    [ -n "$CONTROL_URL" ] || CONTROL_URL="http://127.0.0.1:9091"
    [ -n "$PROXY_HOST" ] || PROXY_HOST="127.0.0.1"
    [ -n "$TUN_BACKEND" ] || TUN_BACKEND="local-userns"
    TOKEN_FILE="/etc/vibelab-egress/client.token"
    AUTH_FILE="/etc/vibelab-egress/default-proxy.auth"
    ;;
  container)
    [ -n "$CONTROL_URL" ] || CONTROL_URL="http://host.docker.internal:9091"
    [ -n "$PROXY_HOST" ] || PROXY_HOST="host.docker.internal"
    [ -n "$TUN_BACKEND" ] || TUN_BACKEND="local-userns"
    TOKEN_FILE="/run/secrets/vibelab_egress_client_token"
    AUTH_FILE="/run/secrets/vibelab_egress_default_proxy_auth"
    ;;
  *) die_usage "--environment must be host or container" ;;
esac
# Offline installer scenarios do not ship compiled binaries; they explicitly
# provision proxy-only clients. Production provisioning remains fail-closed.
if [ "${VIBELAB_EGRESS_TEST_MODE:-0}" = 1 ] && [ -z "${VIBELAB_EGRESS_TEST_TUN:-}" ]; then
  TUN_BACKEND="none"
fi
[ "$TUN_BACKEND" = local-userns ] || [ "$TUN_BACKEND" = none ] || die_usage "--tun-backend must be local-userns or none"

command -v python3 >/dev/null || die_env "python3 not found"
TUN_ENGINE_DIST=""
if [ "$TUN_BACKEND" = local-userns ]; then
  REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  TUN_ENGINE_DIST="$REPO_ROOT/client/tun-engine/dist"
  [ -f "$TUN_ENGINE_DIST/egress-tun-engine-linux-amd64" ] && \
    [ -f "$TUN_ENGINE_DIST/egress-tun-engine-linux-arm64" ] || \
    die_env "build TUN engines first: scripts/build-tun-engine.sh"
fi

# ---------------------------------------------------------------------------
# generate secrets with python3 (secrets module; openssl not required).
# Values are shlex-quoted so the eval is safe for any token content.
# ---------------------------------------------------------------------------
eval "$(python3 - "$CLIENT" "$ENVIRONMENT" "$TUN_BACKEND" <<'PY'
import secrets, sys, json, shlex
client, environment, tun_backend = sys.argv[1], sys.argv[2], sys.argv[3]
api_token = secrets.token_urlsafe(32)
proxy_user = client
proxy_pass = secrets.token_urlsafe(24)
profile = json.dumps({
    "client_id": client,
    "environment": environment,
    "modes": ["proxy"] + (["tun"] if tun_backend == "local-userns" else []),
}, sort_keys=True)
print(f"API_TOKEN={shlex.quote(api_token)}")
print(f"PROXY_USER={shlex.quote(proxy_user)}")
print(f"PROXY_PASS={shlex.quote(proxy_pass)}")
print(f"PROFILE_JSON={shlex.quote(profile)}")
PY
)"

BUNDLE_DIR="$OUT/$CLIENT"
CLIENTS_DIR="$SECRETS_DIR/clients"
mkdir -p "$BUNDLE_DIR" "$CLIENTS_DIR"
# The bundle root is under the persistent root and must stay 0700 (root-only),
# not the umask default 0755 (host acceptance: persistent_dirs_permissions).
chmod 700 "$OUT"

write_file() { # path content
  umask 077
  printf '%s\n' "$2" > "$1"
  chmod 600 "$1"
}

# ---- client bundle (goes to the execution environment) ----
write_file "$BUNDLE_DIR/client.token" "$API_TOKEN"
write_file "$BUNDLE_DIR/default-proxy.auth" "$PROXY_USER:$PROXY_PASS"
cat > "$BUNDLE_DIR/client.yaml" <<EOF
# Generated by provision-client.sh for $CLIENT ($ENVIRONMENT).
# Do not edit secret paths; re-run provisioning to rotate.
client_id: $CLIENT
control_url: $CONTROL_URL
proxy_host: $PROXY_HOST
default_proxy_port: $DEFAULT_PORT
oneshot_proxy_port: $ONESHOT_PORT
api_token_file: $TOKEN_FILE
default_proxy_auth_file: $AUTH_FILE
environment: $ENVIRONMENT
tun_backend: $TUN_BACKEND
tun_dns_servers: $TUN_DNS
EOF
# Include all supported static engine builds when available. Installation
# fails closed if a local-userns bundle has no matching engine.
if [ "$TUN_BACKEND" = local-userns ]; then
  mkdir -p "$BUNDLE_DIR/tun-engine"
  install -m 755 "$TUN_ENGINE_DIST/egress-tun-engine-linux-amd64" "$TUN_ENGINE_DIST/egress-tun-engine-linux-arm64" "$BUNDLE_DIR/tun-engine/"
  (cd "$BUNDLE_DIR/tun-engine" && sha256sum egress-tun-engine-linux-* > SHA256SUMS)
fi
chmod 644 "$BUNDLE_DIR/client.yaml"
chmod 700 "$BUNDLE_DIR"

# ---- backend secrets (egressd auth + config generation) ----
write_file "$CLIENTS_DIR/$CLIENT.token" "$API_TOKEN"
write_file "$CLIENTS_DIR/$CLIENT.profile.json" "$PROFILE_JSON"
write_file "$CLIENTS_DIR/$CLIENT.proxy-creds" "$PROXY_USER:$PROXY_PASS"
chmod 700 "$SECRETS_DIR" "$CLIENTS_DIR"
if [ "${VIBELAB_EGRESS_TEST_MODE:-0}" != 1 ] && id -u vibelab-egressd >/dev/null 2>&1; then
  service_uid="$(id -u vibelab-egressd)"
  service_gid="$(id -g vibelab-egressd)"
  chown "$service_uid:$service_gid" "$CLIENTS_DIR" \
    "$CLIENTS_DIR/$CLIENT.token" \
    "$CLIENTS_DIR/$CLIENT.profile.json" \
    "$CLIENTS_DIR/$CLIENT.proxy-creds"
fi

# ---------------------------------------------------------------------------
# summary — paths + short hashes only, never secret values (spec section 37)
# ---------------------------------------------------------------------------
python3 - "$BUNDLE_DIR" "$CLIENTS_DIR" "$CLIENT" "$ENVIRONMENT" <<'PY'
import hashlib, json, sys, os
bundle, clients, client, environment = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
def h(p):
    try:
        return hashlib.sha256(open(p, "rb").read()).hexdigest()[:8]
    except OSError:
        return "missing"
print(f"provisioned client: {client}")
for p in (f"{bundle}/client.yaml", f"{bundle}/client.token",
          f"{bundle}/default-proxy.auth",
          f"{clients}/{client}.token", f"{clients}/{client}.profile.json",
          f"{clients}/{client}.proxy-creds"):
    print(f"  {p}  sha256:{h(p)}")
print("NOTE: proxy-creds stay in the backend secrets dir and are never")
print("      copied into any client bundle or environment.")
if environment != "host":
    print("NOTE: to move this bundle into the target container, remove any old")
    print("      destination first, then copy (fresh dir, no nesting):")
    print("      docker exec -u root <container> rm -rf /root/egress-client-bundle")
    print(f"      docker cp {bundle} <container>:/root/egress-client-bundle")
    print("      (docker cp of a directory into an EXISTING container dir nests it")
    print("      under the dir name and leaves the stale token behind -> after a")
    print("      rotation the client would present the old token: 'unknown token').")
PY
