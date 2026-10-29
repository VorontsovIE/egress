# PoC Report — vibelab-egress (Phase 0)

**Result: all 22/22 checks passed.** The Mihomo routing architecture was confirmed with a real binary, unblocking production implementation phases A1–A8.

Run date: 2026-10-10. Command: `python3 poc/run_poc.py` (idempotent; starts and stops its own processes).

> Binaries and vendor sources described here (`poc/mihomo*`, `poc/mihomo-src*`, `poc/wgserver/wgserver`, and the vendored WireGuard tree) are excluded from Git history. Restore pinned sources with `sh scripts/fetch-poc-vendor.sh`; build instructions are in `DEVELOPMENT.md`. The tracked one-line vendor patch is `poc/wgserver/third_party/wireguard-go-sticky-sockets.patch`.

## 1. Versions and environment

| Component | Version / value | Notes |
|---|---|---|
| Mihomo (Meta) | **1.19.32** | Built from source |
| `poc/mihomo` | CGO disabled | Main controller and SOCKS listeners |
| `poc/mihomo-gvisor` | `with_gvisor` build tag | Userspace WireGuard client |
| Go | 1.27.2 | `/usr/local/go` |
| wireguard-go | `a6cecdd7f57f` | Vendored under `poc/wgserver/third_party/` |
| sing-wireguard | `c3ae17d19f9e` | gVisor IP stack |
| gVisor | `79317d808312` | Userspace TCP/UDP forwarding |
| GEO | GeoIP.dat 16.6 MB, GeoSite.dat 4.3 MB, asn.mmdb 9.0 MB | Downloaded separately |

Environment: Linux x86_64, 24 CPUs, 256 GiB RAM, container (`eth0=172.17.0.2/16`). Docker/podman were unavailable, so Mihomo ran directly as a binary. GitHub release hosts were blocked, while codeload source archives were available. `/proc/net/wireguard` was absent, `unshare -n` was denied, and route netlink was unsupported without NET_ADMIN; TUN/network-namespace checks were therefore unavailable in this environment.

## 2. Architecture confirmed by the PoC

One SOCKS listener serves each client and one serves one-shot sessions. Routing uses Mihomo's `IN-USER` rules:

```text
client user  -> IN-USER,<user> -> per-client selector (host-main / container-main)
slot user    -> IN-USER,os_N    -> per-slot selector (os_0..os_7)
```

- Default proxy traffic selects the calling client's selector.
- One-shot sessions use fixed users `os_0..os_N`; rule count does not scale with provider node count (300 nodes and one node both produced 39 rules).
- `MATCH,DIRECT` is the final fallback; no implicit VPN route is selected.
- Hard-direct private, loopback, link-local, and Docker CIDRs precede `IN-USER` rules on both listeners.
- Mihomo `geodata-mode` is boolean; `rules` is a list of strings. `DIRECT` and `REJECT` are built-ins, not entries in `proxies`. Provider nodes are included in groups with `use:`.
- Supported group types include `url-test`, `select`, `fallback`, and `load-balance`; `relay` is removed. The API reports `select` as `Selector`.
- Provider nodes are not top-level proxies: `/proxies/<node>/delay` returns 404. Delay checks work through a group endpoint.
- `profile.store-selected: true` persists selected nodes across hot reload through the bbolt cache.
- The Mihomo controller requires Bearer authentication; client tokens do not authenticate to the Mihomo API.

## 3. Measured checks

| ID | Check | Result |
|---|---|---|
| P01 | Controller version | PASS — Meta v1.19.32 |
| P02 | Controller requires authentication | PASS — unauthenticated request returns 401 |
| P03 | Per-client selectors | PASS — host/container selectors; DIRECT available |
| P04 | One-shot slot groups | PASS — eight groups, `os_0..os_7` |
| P05 | Set host default | PASS — PUT returns 204 |
| P06 | Client isolation | PASS — host and one-shot selections remain independent |
| P07 | Host default proxy | PASS — non-LAN request reaches selected node |
| P08 | Container default proxy | PASS — request reaches its selected node |
| P09 | One-shot route | PASS — request reaches selected route through a slot |
| P10 | One-shot preserves defaults | PASS |
| P11 | Concurrent one-shot sessions | PASS — three concurrent requests, including DIRECT |
| P12 | Hard-direct rule precedence | PASS — hard-direct rule precedes IN-USER |
| P13 | Provider update API | PASS — update returns 204 |
| P14 | Node delay API | PASS — delay query through a group |
| P15 | Update preserves selection | PASS |
| P16 | GEOIP rules loaded | PASS — one GEOIP rule, 16 rules total |
| P17 | DNS query | PASS — 200 |
| P18 | Mapping scale | PASS — 39 rules for both 300 and one node |
| P19 | Client token cannot access Mihomo API | PASS — 401 |
| P20 | VLESS end-to-end | PASS — traffic traversed the VLESS tunnel to fake upstream |
| P21 | WireGuard end-to-end | PASS — request traversed WG, gVisor, relay, and back |

## 4. Data-plane end-to-end evidence

The target `203.0.113.1` (TEST-NET-3) is non-LAN, avoiding interception by `GEOIP,LAN,DIRECT`.

**VLESS:** client → main Mihomo (`os_4` → VLESS node) → VLESS tunnel → fake upstream → canned response `NODE:vless-upstream`. The response confirms traffic traversed VLESS.

**WireGuard:** client → main Mihomo (`os_5` → WG node) → WG tunnel → userspace `wgserver` (wireguard-go + gVisor) → relay at `127.0.0.1:18999` → JSON `{"token_ok": true}` → client.

The WG failure was caused by wireguard-go attempting to create a netlink route listener without NET_ADMIN. The interface remained down. The PoC uses a vendored build with `conn.StdNetSupportsStickySockets = false`; sticky sockets are a kernel optimization and were not needed. The interface then came up immediately and the end-to-end check passed.

## 5. Phase 0 coverage map

The 22 checks cover architectural behavior; remaining requirements were assigned to later phases.

| Phase 0 items | Status at PoC | Follow-up |
|---|---|---|
| Native Mihomo provider/update, custom VLESS, WireGuard, delay API, selectors, independent defaults, DIRECT, authenticated routing, one-shot concurrency/isolation, scaling, hard-direct CIDRs, GEO loading, private admin API | PASS | Checks P02–P21 |
| Real BlancVPN/Xeovo subscription formats and any required adapters | Deferred | A2 provider adapters and fixtures |
| Domain probing | Deferred | A5 probe engine |
| Provider disappearance fallback | Deferred | A2/A3 |
| Shared client package, identity API/ACL, CLI, local command execution, host/container e2e, update via route | Deferred | A3/A4/A8 |
| TUN feasibility and capability matrix | Deferred | A6; unavailable in this PoC environment |

## 6. Decisions

- **2026-10-09:** Pin Mihomo v1.19.32.
- **2026-10-09:** Route through `IN-USER` to per-client selectors for defaults and per-slot selectors for one-shot sessions. Rule count does not depend on provider node count.
- **2026-10-09:** Proxy tests against `127.0.0.1` were invalid because LAN-DIRECT intercepted them; use a non-LAN TEST-NET-3 target.
- **2026-10-10:** The WG e2e failure was caused by the unavailable netlink route socket; disable sticky sockets in the vendored PoC build. Result: 22/22.
- **2026-10-10:** Replace the fixed two-second wgserver startup sleep with polling because gVisor and wireguard-go initialization can take longer under load.

## 7. Artifacts

- `poc/run_poc.py`: idempotent orchestrator for 22 checks.
- `poc/fake_node.py`: fake SOCKS5 nodes with canned responses and JSONL logs.
- `poc/wgserver/`: userspace WG server and relay.
- `poc/wgserver/third_party/wireguard-go-sticky-sockets.patch`: tracked vendor patch.
- `poc/mihomo`, `poc/mihomo-gvisor`: generated Mihomo v1.19.32 binaries (not tracked).
- `poc/geo/`: downloaded GEO data (not tracked).
- `poc/results.json`: latest PoC results.
- `poc/run/`: run artifacts.
- `egressd/src/egressd/configgen.py`: production configuration generator.
