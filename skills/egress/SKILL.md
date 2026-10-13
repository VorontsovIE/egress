# Skill: egress (vibelab-egress)

You have a local egress client (`egressctl` + `egress-run`) installed in this
environment. It controls a shared egress backend (Mihomo) through a narrow,
authenticated API. Use it when a task needs a specific exit location, a
specific provider, or a baseline comparison against `direct`.

## Hard rules (never violate)

1. **Start with identity.** Before anything else run `egressctl whoami` (and
   `egressctl status` if you need backend health). It tells you your client
   profile, environment and capabilities. If it fails, stop and report the
   config error — do not guess.
2. **Defaults are per-client.** Your default route/mode is yours alone.
   Changing it does not affect the host or any other client — and their
   changes do not affect you. When in doubt, check `egressctl route current`
   before setting a persistent default.
3. **One-shot for one-off tasks.** For a single geo-specific command, use
   `egress-run ROUTE -- command` (or `egressctl` one-shot). Do **not** change
   the persistent default just to run one command.
4. **`direct` is a normal route.** It is in the route catalog, works as a
   default, as a one-shot, and as the baseline for probes/benchmarks.
5. **Parallel tasks = parallel invocations.** For concurrent work, start
   independent `egress-run ROUTE` processes. Never switch the default back
   and forth between tasks.
6. **Fresh health data before choosing.** Before selecting a route, look at
   current health/latency (`egressctl list --healthy`, `egressctl test`,
   `egressctl best`). Do not pick a route on stale memory.
7. **Domain access questions use `probe-domain`.** GeoIP/GeoSite data is a
   heuristic. For "can I reach example.com through this route?" run
   `egressctl probe-domain example.com --all` and read the per-route error
   class (dns / tcp / tls / http).
8. **Repeated failures → provider update.** If many routes are dead or the
   inventory looks stale, trigger `egressctl update` (optionally
   `--provider P`, `--if-older-than 6h`). If the provider's subscription URL
   is blocked from here, update through a working route:
   `egressctl update --provider P --via ROUTE`.
9. **Never benchmark the whole fleet.** `egressctl benchmark` (no route)
   benchmarks a latency shortlist plus the `direct` baseline — that is the
   intended usage. Do not loop over hundreds of routes.
10. **`tun` only when proxy is not enough AND capabilities allow it.** Check
    `whoami`/capabilities for `tun`. If it is not available, the command
    fails with an explicit `unsupported_in_environment` error — there is no
    silent fallback to proxy. Do not rely on one.
11. **Never route internal traffic through the VPN.** Docker-internal
    destinations, the LiteLLM endpoint, localhost and other HARD DIRECT /
    internal targets must stay direct. `egress-run` already sets `no_proxy`
    for RFC1918/localhost; do not override it.
12. **Never touch the backend internals.** No raw Mihomo admin API, no
    provider secrets, no host root, no Docker socket. You only get the public
    `egressd` API via `egressctl`/`egress-run`. If you need something else,
    report the gap — do not improvise.
13. **After `route set`, verify with `route current`.**
14. **Country-specific tasks: verify the exit.** When the exit country
    actually matters, confirm it (probe + exit-IP/GeoIP enrichment shown in
    `probe-domain`/`best` output), do not assume it from the node name.

## Commands (quick reference)

```bash
egressctl whoami                      # identity + capabilities (start here)
egressctl status                      # backend status for this client
egressctl doctor                      # local + backend health checks

egressctl route current               # my persistent default route
egressctl route set ROUTE             # set my default (verify after!)
egressctl mode current
egressctl mode set proxy|tun          # tun only if capabilities allow

egressctl list [--country FI] [--provider blanc] [--healthy]
egressctl test [ROUTE]                # health/latency (mihomo delay; direct = baseline)
egressctl probe-domain DOMAIN --all   # DNS/TCP/TLS/HTTP per route
egressctl benchmark [ROUTE]           # one route, or shortlist+direct baseline
egressctl best [--country FI] [--provider P] [--domain D] \
               [--max-latency 150] [--speed-aware]

egressctl update [--provider P] [--via ROUTE] [--if-older-than 6h]
egressctl oneshot active
egressctl geo status
egressctl geo update
egressctl config show|validate
```

All read commands accept `--json`. Exit codes: 0 ok, 1 runtime/API error,
2 usage, 3 config, 4 auth, 5 identity mismatch, 6 unsupported (tun).

```bash
egress-run --default -- command ...   # my persistent default route+mode
egress-run ROUTE -- command ...       # one-shot, explicit route
egress-run direct -- command ...      # one-shot, no proxy
egress-run --mode proxy ROUTE -- command ...
```

`egress-run` executes the command **locally** in this environment with the
proxy env set; it never sends the command to the backend. Exit status and
SIGINT/SIGTERM are preserved.

## Decision tree

```
Task needs a specific exit location / provider / baseline?
├─ no  → run the command normally (or egress-run direct -- for an explicit direct path)
└─ yes
   ├─ egressctl whoami → ok? (if not: report config/auth error, stop)
   ├─ one-off command?
   │  ├─ yes → egress-run ROUTE -- command
   │  │        (ROUTE from: egressctl best --country X / list --healthy)
   │  └─ no  → persistent change:
   │           egressctl route current → route set ROUTE → route current (verify)
   ├─ "can I reach domain D?"
   │  → egressctl probe-domain D --all  (read error classes per route)
   │  → egressctl best --domain D       (only routes with a fresh successful probe)
   ├─ "which route is best?"
   │  → egressctl best [--country X] [--speed-aware]
   │  → need real throughput? egressctl benchmark (shortlist) first, then --speed-aware
   ├─ many routes dead / stale inventory?
   │  → egressctl update [--provider P]
   │  → subscription blocked? egressctl update --provider P --via <working ROUTE>
   ├─ parallel tasks with different routes?
   │  → independent egress-run ROUTE invocations (never mutate the default)
   └─ proxy mode insufficient (proxy-unaware binary, UDP/QUIC, setuid)?
      → check whoami capabilities for tun
         ├─ tun available → egress-run --mode tun ROUTE -- command
         └─ not available → report unsupported_in_environment; do NOT fall back silently
```

## Examples

### 1. Check identity and current default

```bash
egressctl whoami
# client_id=openscience-main environment=gvisor-container modes=proxy

egressctl route current
# default route: direct
```

### 2. Set and verify a persistent default

```bash
egressctl route set blanc:FI-Helsinki-1
egressctl route current
# default route: blanc:FI-Helsinki-1
```

### 3. Direct one-shot (baseline / no proxy)

```bash
egress-run direct -- curl -s https://api.ipify.org
```

### 4. VPN one-shot

```bash
egress-run blanc:FI-Helsinki-1 -- curl -s https://api.ipify.org
```

### 5. Two concurrent one-shots, different routes

```bash
egress-run blanc:FI-Helsinki-1 -- curl -s https://api.ipify.org &
egress-run xeovo:US-New-York-1 -- curl -s https://api.ipify.org &
wait
```

Both run locally and independently; neither touches the default.

### 6. Domain probe across routes

```bash
egressctl probe-domain example.com --all
# per route: reachable + error class (ok / dns_failure / tcp_timeout /
# tcp_refused / tls_failure / http_status_denied / redirect / ...)
```

### 7. Provider update — direct and via a working route

```bash
egressctl update --provider blanc                 # fetch subscription directly
egressctl update --provider blanc --via blanc:FI-Helsinki-1
# the second form transports the subscription fetch through a working route
# (use it when the subscription URL is blocked from the direct path)
```

### 8. Proxy → TUN decision

```bash
egressctl whoami
# modes=proxy  → tun not available in this environment

egress-run --mode tun blanc:FI-Helsinki-1 -- command
# egress-run: tun mode is not available in environment 'gvisor-container'
# (tun_backend=none); use --mode proxy        [exit 6]
```

If `modes` includes `tun`, use `--mode tun` for proxy-unaware TCP clients,
UDP/QUIC, or setuid/static binaries that ignore proxy env. There is no
silent fallback: if tun is unsupported you get exit 6 and must decide.

### 9. Diagnosing `unsupported_in_environment`

```bash
egressctl whoami            # check 'modes'
egressctl config show       # check tun_backend (none | host-helper)
egressctl mode set tun      # server-side check; 400 unsupported_in_environment
```

Meaning: this client profile's environment cannot provide TUN (e.g. gVisor
container without an approved local backend). Options: use proxy mode, or
report the capability gap. Do not attempt host-level workarounds.

### 10. Best route with a domain constraint

```bash
egressctl best --country FI --domain example.com
# best: blanc:FI-Helsinki-1
#   #1 blanc:FI-Helsinki-1  score=78.4  health(40.0) + freshness(18.2) + latency(20.2)
# (2 excluded: blanc:FI-Helsinki-2 [no fresh probe for example.com], ...)
```

Only routes with a fresh successful probe of the domain are candidates;
the explanation shows exactly which components scored.

## What this skill must NOT lead you to do

- calling the Mihomo admin API (port 9090) or any non-`/v1` backend endpoint;
- reading provider subscription files or client tokens of other clients;
- requesting host root / Docker socket / raw shell on the backend host;
- routing LiteLLM, Docker-internal or localhost traffic through a proxy;
- assuming a node name's country is the verified exit country;
- benchmarking or probing the entire route inventory.
