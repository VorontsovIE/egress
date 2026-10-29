# Personal subscription checks on a local host — 2026-10-18

Testing was performed on a local host. The original `subscriptions.txt` was moved from the repository into a private user directory (mode `0700`); the file and derived configs use mode `0600`. Subscription URLs, URIs, keys, and contents are intentionally omitted from this report.

## Coverage

| Source | Format | Nodes found | Accepted | Rejected by validator |
|---|---|---:|---:|---:|
| batch1 | Base64 VLESS | 62 | 62 | 0 |
| batch2 | Mixed URI list (Hysteria2, Shadowsocks, Trojan, VLESS, VMess) | 317 | 305 | 12 |
| batch3 | Base64 VLESS | 37 | 37 | 0 |
| batch4 | Single VLESS URI | 1 | 1 | 0 |

All four normalized configs passed `mihomo -t` with v1.19.32. The client profile imported 405 proxy routes; including `direct`, the catalog contains 406 routes. Twelve batch2 nodes were rejected because their server addresses were non-public or could not be resolved, according to `personal.validate_yaml`.

A local Docker Compose backend and host TUN helper were installed. Both containers became healthy; `egressctl doctor` confirmed configuration, control plane, identity, and proxy endpoint. A working Shadowsocks route returned HTTP 204 through both `egress-run` and TUN; DNS and UDP/QUIC were also confirmed. Concurrent TUN and one-shot jobs remained isolated. `direct` worked in both modes. The host user's personal directory was not visible in a separate `host-main` profile.

The checks sampled individual nodes; they did not establish availability for all 405 routes. Initial VLESS/Trojan/Hysteria2/VMess probes returned TLS/EOF network errors, which does not establish that the whole subscription is broken. Of the first 16 sampled Shadowsocks routes in batch2, seven were healthy and returned HTTP 204. Availability depends on time, destination, and provider state.

## Defects found and fixes

| Area | Symptom | Cause and fix |
|---|---|---|
| Backend install | Running the installer from another working directory broke Compose at deployment. | The script now changes to the repository root before `docker compose` and adds `/usr/local/bin` to PATH for acceptance. Added a test for invocation outside the checkout. |
| Client install | Reinstalling the host client removed the service UID's access to backend secrets. | `/etc/vibelab-egress` is mode `0755` so the service UID can traverse to its own protected files. Reinstall and `whoami` were verified live. |
| Safe URL import | A subscription URL appeared in process argv with `source add --url`. | Added `--url -` input from stdin; tested URLs were not recorded in shell history. |
| Live harness: large catalog | JSON from `list`, `probe-domain`, and `best` was truncated at 8 KB and could not be parsed. | Removed the output-size limit for these responses. |
| Live harness: proxy/IP | Proxy checks used `urllib` without SOCKS and compared a changing public IP. | Proxy checks now use SOCKS-aware `curl` and expect HTTP 204. Direct mode checks public addressing and absence of TUN without requiring exact IP equality. |
| Live harness: missing Docker container | An absent container caused a failure. | The harness now records an explicit skip when the container is absent. |
| CLI: long network operations | `probe-domain --all`, `best --domain`, and benchmark stopped after 15 seconds. | These operations now wait up to 150 seconds; timeout guidance no longer incorrectly suggests `source list`. |
| CLI benchmark | `benchmark ROUTE --bytes 1M` ignored the requested size. | The size is passed to the API; a regression test was added. |
| Backend probe | A node's TLS handshake timeout caused HTTP 500 for the entire request. | Timeout is classified as that node's `tls_failure`; a stalled handshake test was added. |
| Backend benchmark | SOCKS requests targeted host `http` due to incorrect URL parsing; measured bytes included HTTP headers. | Parse the URL as HTTP, use a working bounded endpoint, count body bytes only, and verify HTTP status and download completeness. |

## Acceptance and limits

The local offline suite passed **373 tests**; install scenarios passed **75**. Separate root host-TUN checks: **18 pass, 0 fail, 4 skip**. Root live acceptance: **17 pass, 0 fail, 11 skip**. `host-main` had no personal subscriptions, so provider-dependent checks were skipped. Reports were retained in protected host and user directories; the earlier host report was not modified.

Final user live acceptance: **23 pass, 0 fail, 5 skip**. Independent `verify-live-report.py` confirmed the hash, all 23 passes, and no secrets in the report. A domain probe covered 50 routes: 12 `ok`, 38 `tls_failure`; `best --domain` returned 22 ranked candidates. A benchmark of a working Shadowsocks route downloaded exactly 1 MiB in 2.3 seconds. Both containers remained healthy at the end.

The full JSON report is in the private file `~/.local/share/vibelab-egress-test/live-user-finalfull.json` (mode `0600`). The full user run has 28 checks. Docker-container-dependent checks are skipped when no Docker container is present; its restart check is disabled by default. The host harness requires root and was checked separately. Comparing the same released client on host and in Docker is not applicable without a container.

`probe-domain --all` currently covers the first 50 of 406 routes in one request. This is a resource limit, not evidence that every route was checked. Working routes from other providers were confirmed separately with `egressctl test`, `egress-run`, and TUN jobs.

## TLS regrouping after fixes — 2026-10-19

After rebuilding and restarting the local backend, all 401 routes in the UID 1000 profile were checked, including `direct`. Groups were divided by source and protocol; each batch had at most 50 routes, with parallelism 8 and eight one-shot slots. Each route used a route-specific probe against two independent HTTPS targets expecting statuses 200 and 204, with a five-second timeout. The table contains aggregate data only; it omits URLs, route IDs, subscriptions, and configs.

| Source | Protocol | Routes | Both targets available | TLS failure on both targets | Different result/error by target |
|---|---|---:|---:|---:|---:|
| direct baseline | direct | 1 | 0 | 0 | 1 |
| batch1 | VLESS | 56 | 0 | 44 | 12 |
| batch2 | Hysteria2 | 21 | 0 | 21 | 0 |
| batch2 | Shadowsocks | 78 | 33 | 44 | 1 |
| batch2 | Trojan | 95 | 0 | 95 | 0 |
| batch2 | VLESS | 56 | 0 | 56 | 0 |
| batch2 | VMess | 56 | 0 | 56 | 0 |
| batch3 | VLESS | 37 | 0 | 29 | 8 |
| batch4 | VLESS | 1 | 0 | 1 | 0 |
| **Total** |  | **401** | **33** | **346** | **22** |

Repeated TLS failures against two targets may indicate network blocking or incompatibility of individual nodes/protocols; they do not establish a provider-side cause. Thirty-three Shadowsocks routes passed both targets. The direct baseline succeeded for one target and failed for the other, so it is classified as target-specific rather than a stable failure.

Aggregated JSON was saved privately at `~/.local/share/vibelab-egress-test/probe-domain-groups.json` (mode `0600`). Final user live acceptance after the fixes was 23 PASS / 0 FAIL / 5 SKIP; independent hash and secret checks returned `VERIFIED`. The report has mode `0600` at `~/.local/share/vibelab-egress-test/live-user-after-fixes.json`.
