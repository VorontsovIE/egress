#!/usr/bin/env bash
# Offline install suite for scripts/install-backend-host.sh and the
# host/egress-host-test harness.
#
# Runs WITHOUT a Docker daemon (EGRESS_TEST_MODE=1 + stubs in
# tests/install/stubs shadow docker/id/sleep/... on PATH). The host client
# installer writes to system paths, so every scenario skips it; client
# packaging and install behavior have focused tests of their own.
#
# Scenarios:
#   0. syntax: bash -n all scripts/*.sh, py_compile host utilities
#   1. success path: rc 0, log file with run header + all 10 step markers,
#      generated/config.yaml, secrets 0600, harness report 0 failed
#   2. fail A: STUB_DOCKER_UP_RC=1 -> rc 3, log has step 9,
#      'docker compose up -d', 'full log:'
#   3. fail B: STUB_DOCKER_HEALTH=never -> rc 3, redacted egressd log dump
#      + RestartCount in the install log
#   4. fail C: STUB_DOCKER_HEALTH=restarting (stale healthy read) -> rc 3
#   5. root escape: --help rc 0; run without EGRESS_TEST_MODE as
#      non-root -> rc 3 'must run as root'
#   6. negative harness (ported from the lost /tmp/a1_test.sh): public
#      bind, docker.sock mount, provider secret in a client bundle
#   7. verify-backend-host.sh: live bind allowlist (safe binds -> rc 0;
#      0.0.0.0 bind -> rc 3 with the offending mapping named)
#
# Exit: 0 all pass, 1 any failure.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
STUBS="$HERE/stubs"

PASS=0
FAIL=0
FAILED_NAMES=""

ok()  { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); FAILED_NAMES="$FAILED_NAMES $1"; echo "FAIL - $1: $2"; }
check() { # <name> <rc> [detail]
  if [ "$2" -eq 0 ]; then ok "$1"; else bad "$1" "${3:-condition failed}"; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# The host may already have a production client under /etc. Stubbed tests
# must inspect only their temporary fixture, never the machine's live files.
export EGRESS_HOST_CLIENT_DIR="$TMP/absent-host-client"

echo "# offline install suite (isolated system paths, bash stubs: $STUBS)"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
make_env() { # <dir> — root + secrets dirs; provision client host-main
  local d="$1"
  mkdir -p "$d/root" "$d/secrets"
  chmod 700 "$d/root"
  env PATH="$STUBS:$PATH" EGRESS_TEST_MODE=1 \
    bash "$REPO/scripts/provision-client.sh" \
    --client host-main --environment host \
    --secrets-dir "$d/secrets" --out "$d/root/client-bundles" \
    > "$d/provision.out" 2>&1
  check "env: provision-client.sh host-main" $? \
    "$(head -1 "$d/provision.out" 2>/dev/null)"
}

# scenario_install <dir> [KEY=VAL...] [--flag...]
# KEY=VAL pairs are passed to the install script's environment.
scenario_install() {
  local d="$1"; shift
  local envs=() flags=()
  local a
  for a in "$@"; do
    case "$a" in
      *=*) envs+=("$a") ;;
      *)   flags+=("$a") ;;
    esac
  done
  # --skip-firewall by default (the stub ufw is a no-op, but keep the
  # suite from touching any real firewall state); SCENARIO_WITH_FIREWALL=1
  # opts into the real step-6 code path for the firewall scenario.
  local skip_fw="--skip-firewall"
  [ "${SCENARIO_WITH_FIREWALL:-0}" = "1" ] && skip_fw=""
  ( cd "${SCENARIO_CWD:-$REPO}"
    env "${envs[@]+"${envs[@]}"}" PATH="$STUBS:$PATH" \
      EGRESS_HOST_CLIENT_DIR="$d/root/client-bundles/host-main" \
      EGRESS_CLIENT_LIB_DIR="$d/absent-client-lib" \
      EGRESS_TEST_MODE=1 \
      bash "$REPO/scripts/install-backend-host.sh" \
      --root "$d/root" --secrets-dir "$d/secrets" \
      --yes ${skip_fw} ${flags[@]+"${flags[@]}"} \
      > "$d/install.out" 2> "$d/install.err" )
}

log_of() { echo "$1/root/install-backend-host.log"; }

# ---------------------------------------------------------------------------
# 0. syntax
# ---------------------------------------------------------------------------
echo "== 0. syntax"
for sh in "$REPO"/scripts/*.sh; do
  bash -n "$sh" 2> "$TMP/syn.err"
  check "bash -n $(basename "$sh")" $? "$(head -1 "$TMP/syn.err" 2>/dev/null)"
done
for py in "$REPO"/host/egress-host-test "$REPO"/host/egress-hostctl \
          "$REPO"/host/live-acceptance.py; do
  python3 -m py_compile "$py" 2> "$TMP/syn.err"
  check "py_compile $(basename "$py")" $? "$(head -1 "$TMP/syn.err" 2>/dev/null)"
done

# ---------------------------------------------------------------------------
# 1. success path
# ---------------------------------------------------------------------------
echo "== 1. success path"
d="$TMP/success"
make_env "$d"
scenario_install "$d" --skip-client
rc=$?
check "success: install rc=0" $rc "$(tail -1 "$d/install.err" 2>/dev/null)"
logf="$(log_of "$d")"
[ -f "$logf" ]
check "success: install log file exists" $? "missing $logf"
grep -q "run header: hostname=" "$logf" 2>/dev/null
check "success: run header (hostname/kernel/user)" $?
grep -q "compose=" "$logf" 2>/dev/null
check "success: run header (docker/compose versions)" $?
grep -q "git_rev=" "$logf" 2>/dev/null
check "success: run header (git rev)" $?
for i in 1 2 3 4 5 6 7 8 9 10; do
  grep -q "\[step $i/10\]" "$logf" 2>/dev/null
  check "success: step marker $i/10" $?
done
[ -s "$d/root/generated/config.yaml" ]
check "success: generated/config.yaml created" $?
[ "$(stat -c '%a' "$d/secrets/mihomo_api_secret" 2>/dev/null)" = "600" ]
check "success: mihomo_api_secret mode 600" $?
[ "$(stat -c '%a' "$d/secrets/devbridge.token" 2>/dev/null)" = "600" ]
check "success: devbridge.token mode 600" $?
[ "$(stat -c '%a' "$d/secrets/clients/host-main.token" 2>/dev/null)" = "600" ]
check "success: clients/host-main.token mode 600" $?
python3 - "$d/root/acceptance-report.json" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if r.get("summary", {}).get("failed") == 0 else 1)
PY
check "success: harness report 0 failed" $?
grep -q '"summary"' "$logf" 2>/dev/null
check "success: harness JSON report teed into install log" $?
grep -q "docker compose up -d --force-recreate" "$logf" 2>/dev/null
check "success: regenerated config forces container recreate" $?
stop_line="$(grep -n -m1 'run: docker compose stop egressd egress-mihomo' "$logf" | cut -d: -f1)"
bootstrap_line="$(grep -n -m1 'run: python3 -m egressd.bootstrap_config' "$logf" | cut -d: -f1)"
[ -n "$stop_line" ] && [ -n "$bootstrap_line" ] && [ "$stop_line" -lt "$bootstrap_line" ]
check "success: old egressd stopped before bootstrap regeneration" $?

# pkexec starts the installer outside the checkout. Compose must still find
# this repository's compose.yaml during step 8.
d="$TMP/outside-cwd"
make_env "$d"
SCENARIO_CWD="$TMP" scenario_install "$d" --skip-client
check "outside-cwd: install rc=0" $? "$(tail -1 "$d/install.err" 2>/dev/null)"

# ---------------------------------------------------------------------------
# 2. fail A: compose up fails
# ---------------------------------------------------------------------------
echo "== 2. fail A (compose up rc 1)"
d="$TMP/fail-up"
make_env "$d"
scenario_install "$d" STUB_DOCKER_UP_RC=1 --skip-client
rc=$?
[ "$rc" -eq 3 ]
check "fail-up: rc=3 (got $rc)" $? "$(tail -1 "$d/install.err" 2>/dev/null)"
logf="$(log_of "$d")"
grep -q "\[step 9/10\]" "$logf" 2>/dev/null
check "fail-up: log has step 9" $?
grep -q "docker compose up -d" "$logf" 2>/dev/null
check "fail-up: log has 'docker compose up -d'" $?
grep -q "full log:" "$logf" 2>/dev/null
check "fail-up: log has 'full log:' pointer" $?

# ---------------------------------------------------------------------------
# 3. fail B: never healthy (timeout dump + redaction)
# ---------------------------------------------------------------------------
echo "== 3. fail B (never healthy)"
d="$TMP/fail-never"
make_env "$d"
# pre-seed the mihomo secret so its value can be planted in the (stub)
# container logs and must come back redacted
printf 'offline-test-mihomo-secret-value' > "$d/secrets/mihomo_api_secret"
chmod 600 "$d/secrets/mihomo_api_secret"
{
  echo "egressd stub log: starting"
  echo "egressd stub log: token offline-test-mihomo-secret-value leaked"
} > "$d/container-logs.txt"
scenario_install "$d" STUB_DOCKER_HEALTH=never STUB_DOCKER_LOGS="$d/container-logs.txt" --skip-client
rc=$?
[ "$rc" -eq 3 ]
check "fail-never: rc=3 (got $rc)" $? "$(tail -1 "$d/install.err" 2>/dev/null)"
logf="$(log_of "$d")"
grep -q "last 100 lines of egressd logs" "$logf" 2>/dev/null
check "fail-never: log has egressd docker-logs dump" $?
grep -q "RestartCount=" "$logf" 2>/dev/null
check "fail-never: log has RestartCount" $?
grep -q "egressd stub log: starting" "$logf" 2>/dev/null
check "fail-never: stub log content captured" $?
if grep -qF "offline-test-mihomo-secret-value" "$logf" 2>/dev/null; then
  check "fail-never: secret value redacted in log" 1 "raw secret found in install log"
else
  ok "fail-never: secret value redacted in log"
fi
grep -q "\[REDACTED\]" "$logf" 2>/dev/null
check "fail-never: [REDACTED] marker present in log" $?

# ---------------------------------------------------------------------------
# 4. fail C: restarting (stale healthy read must NOT pass the gate)
# ---------------------------------------------------------------------------
echo "== 4. fail C (restarting, stale healthy)"
d="$TMP/fail-restarting"
make_env "$d"
scenario_install "$d" STUB_DOCKER_HEALTH=restarting --skip-client
rc=$?
[ "$rc" -eq 3 ]
check "fail-restarting: rc=3 (got $rc)" $? "$(tail -1 "$d/install.err" 2>/dev/null)"
logf="$(log_of "$d")"
grep -q "state=restarting" "$logf" 2>/dev/null
check "fail-restarting: log shows restarting state" $?
grep -q "full log:" "$logf" 2>/dev/null
check "fail-restarting: log has 'full log:' pointer" $?

# ---------------------------------------------------------------------------
# 5. root escape
# ---------------------------------------------------------------------------
echo "== 5. root escape"
mkdir -p "$TMP/root-esc/root" "$TMP/root-esc/secrets"
( cd "$REPO"
  env PATH="$STUBS:$PATH" STUB_ID_UID=1000 \
    bash scripts/install-backend-host.sh --help > "$TMP/root-esc/help.out" 2>&1 )
check "root-escape: --help rc 0 (without root)" $?
( cd "$REPO"
  env PATH="$STUBS:$PATH" STUB_ID_UID=1000 \
    bash scripts/install-backend-host.sh \
    --root "$TMP/root-esc/root" --secrets-dir "$TMP/root-esc/secrets" \
    --yes --skip-firewall > "$TMP/root-esc/run.out" 2> "$TMP/root-esc/run.err" )
rc=$?
[ "$rc" -eq 3 ]
check "root-escape: run without TEST_MODE as non-root rc=3 (got $rc)" $?
grep -q "must run as root" "$TMP/root-esc/run.err" 2>/dev/null
check "root-escape: stderr says 'must run as root'" $?

# ---------------------------------------------------------------------------
# 6. negative harness (ported from the lost /tmp/a1_test.sh)
# ---------------------------------------------------------------------------
echo "== 6. negative harness (--static, tmp trees)"

# 6a. public bind 0.0.0.0:1080
d="$TMP/neg-public"
mkdir -p "$d/repo" "$d/root" "$d/secrets"
cat > "$d/repo/compose.yaml" <<'YAML'
services:
  egress-mihomo:
    image: metacubex/mihomo:v1.19.32
    ports:
      - "0.0.0.0:1080"
  egressd:
    build: ./egressd
    ports:
      - "127.0.0.1:9091"
YAML
env EGRESS_REPO="$d/repo" EGRESS_ROOT="$d/root" \
    EGRESS_SECRETS="$d/secrets" \
    python3 "$REPO/host/egress-host-test" --static \
    > "$d/report.json" 2> "$d/report.err"
rc=$?
[ "$rc" -eq 1 ]
check "neg-public: harness rc=1 (got $rc)" $?
python3 - "$d/report.json" no_public_binds <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
c = next((x for x in r["checks"] if x["id"] == sys.argv[2]), None)
sys.exit(0 if c and c["status"] == "fail" else 1)
PY
check "neg-public: no_public_binds=fail" $?

# 6b. docker.sock mounted
d="$TMP/neg-socket"
mkdir -p "$d/repo" "$d/root" "$d/secrets"
cat > "$d/repo/compose.yaml" <<'YAML'
services:
  egressd:
    build: ./egressd
    ports:
      - "127.0.0.1:9091"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
YAML
env EGRESS_REPO="$d/repo" EGRESS_ROOT="$d/root" \
    EGRESS_SECRETS="$d/secrets" \
    python3 "$REPO/host/egress-host-test" --static \
    > "$d/report.json" 2> "$d/report.err"
python3 - "$d/report.json" docker_socket_not_mounted <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
c = next((x for x in r["checks"] if x["id"] == sys.argv[2]), None)
sys.exit(0 if c and c["status"] == "fail" else 1)
PY
check "neg-socket: docker_socket_not_mounted=fail" $?

# 6c. provider secret leaked into a client bundle
d="$TMP/neg-leak"
mkdir -p "$d/repo" "$d/root/client-bundles/host-main" "$d/secrets/providers"
chmod 700 "$d/root" "$d/root/client-bundles"
printf 'fake-provider-subscription-value-0001\n' > "$d/secrets/providers/sub1.yaml"
cat > "$d/root/client-bundles/host-main/client.yaml" <<'YAML'
client_id: host-main
control_url: http://127.0.0.1:9091
subscription: fake-provider-subscription-value-0001
YAML
env EGRESS_REPO="$d/repo" EGRESS_ROOT="$d/root" \
    EGRESS_SECRETS="$d/secrets" \
    python3 "$REPO/host/egress-host-test" --static \
    > "$d/report.json" 2> "$d/report.err"
python3 - "$d/report.json" no_backend_secrets_in_client_bundles <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
c = next((x for x in r["checks"] if x["id"] == sys.argv[2]), None)
sys.exit(0 if c and c["status"] == "fail" else 1)
PY
check "neg-leak: no_backend_secrets_in_client_bundles=fail" $?

# ---------------------------------------------------------------------------
# 7. verify-backend-host.sh (live bind allowlist, stubbed docker)
# ---------------------------------------------------------------------------
echo "== 7. verify-backend-host"

scenario_verify() { # <dir> [KEY=VAL...]
  local d="$1"; shift
  local envs=()
  local a
  for a in "$@"; do envs+=("$a"); done
  ( cd "$REPO"
    env "${envs[@]+"${envs[@]}"}" PATH="$STUBS:$PATH" \
      EGRESS_HOST_CLIENT_DIR="$d/root/client-bundles/host-main" \
      EGRESS_CLIENT_LIB_DIR="$d/absent-client-lib" \
      EGRESSD_UID=1000 EGRESSD_GID=1000 \
      EGRESS_ROOT="$d/root" EGRESS_SECRETS="$d/secrets" \
      bash scripts/verify-backend-host.sh \
      --root "$d/root" --secrets-dir "$d/secrets" \
      > "$d/verify.out" 2> "$d/verify.err" )
}

# 7a. safe binds (127.0.0.1 + gateway) -> rc 0, allowlist PASS line
d="$TMP/verify-ok"
make_env "$d"
scenario_install "$d" --skip-client
scenario_verify "$d"
rc=$?
[ "$rc" -eq 0 ]
check "verify-ok: rc=0 (got $rc)" $? "$(tail -1 "$d/verify.err" 2>/dev/null)"
grep -q "PASS  all published ports bound to 127.0.0.1 / 172.17.0.1" \
  "$d/verify.out" 2>/dev/null
check "verify-ok: safe binds within allowlist" $?
[ -f "$d/root/verify-backend-host.log" ]
check "verify-ok: verify log file exists" $?

# 7b. public bind (0.0.0.0) -> rc 3, FAIL line names the mapping
d="$TMP/verify-public"
make_env "$d"
scenario_install "$d" --skip-client
scenario_verify "$d" STUB_DOCKER_BINDS=public
rc=$?
[ "$rc" -eq 3 ]
check "verify-public: rc=3 (got $rc)" $? "$(tail -1 "$d/verify.err" 2>/dev/null)"
grep -q "FAIL  published port(s) on unexpected interface" \
  "$d/verify.out" 2>/dev/null
check "verify-public: FAIL line for the bad binds" $?
grep -q "0.0.0.0" "$d/verify.out" 2>/dev/null
check "verify-public: 0.0.0.0 binding reported" $?

# 7c. install WITH firewall (stub ufw no-op): docker subnets from the CIDR
#     artifact are allowed for 9091/1080:1081 (spec 36)
d="$TMP/fw-subnets"
make_env "$d"
SCENARIO_WITH_FIREWALL=1 scenario_install "$d" --skip-client
rc=$?
[ "$rc" -eq 0 ]
check "fw-subnets: install rc=0 (got $rc)" $? "$(tail -1 "$d/install.err" 2>/dev/null)"
logf="$(log_of "$d")"
grep -q "container subnet" "$logf" 2>/dev/null
check "fw-subnets: ufw docker-subnet rules logged (spec 36)" $?
grep -q "allow 172.17.0.0/16 -> 9091,1080:1081" "$logf" 2>/dev/null
check "fw-subnets: ufw rule for the (stub) bridge subnet" $?

# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------
echo
echo "== offline install suite: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
  echo "failed checks:$FAILED_NAMES"
  # Surface only failed check identifiers/details from generated harness
  # reports. The reports may also contain environment evidence, so keep
  # diagnostic output narrowly scoped and never dump the full JSON here.
  for scenario in success outside-cwd verify-ok verify-public fw-subnets; do
    report="$TMP/$scenario/root/acceptance-report.json"
    [ -f "$report" ] || continue
    python3 - "$report" "$scenario" <<'PY'
import json, sys
try:
    report = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    print(f"diagnostic {sys.argv[2]}: could not read acceptance report: {exc}")
    raise SystemExit(0)
for check in report.get("checks", []):
    if check.get("status") == "fail":
        print("diagnostic %s: %s: %s" % (
            sys.argv[2], check.get("id"), check.get("detail", "")))
PY
  done
  exit 1
fi
exit 0
