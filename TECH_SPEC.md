---
title: "Technical Specification: Mihomo-Based Managed Egress"
status: "Architecture review / client-local CLI model"
date: "2026-10-09"
lang: en-US
---

# 0. Purpose

> **Implementation status:** This document contains current requirements as well as historical/planned requirements. See `STATUS.md` for implementation status and `INSTALL.md`, `USER_GUIDE.md`, and `CLI_REFERENCE.md` for current user-facing behavior. Treat a requirement as planned until the status summary or code confirms it is implemented.

## Implementation status map

This map describes the current source tree. Live availability still depends on deployment configuration and provider routes.

| Status | Capabilities |
|---|---|
| **Implemented** | Backend `/v1` API; client identity and per-client defaults; provider catalog/update and personal sources; `egressctl` route/mode/source/update/test/probe/benchmark/best/doctor commands; local `egress-run` proxy/direct execution; client-local TUN bootstrap and packet engine; host/container provisioning and acceptance tooling. |
| **Environment-dependent** | Live provider connectivity, public endpoint/firewall reachability, Docker acceptance, and TUN availability. TUN requires documented Linux namespace/device/runtime capabilities and a matching engine binary. |
| **Not shipped** | Admin UI (Section 42) and MCP server (Section 43) are optional design extensions and are not part of the current implementation. |

Sections 45–51 preserve PoC evidence, historical work sequencing, and acceptance criteria. Their presence does not mean every deployment-specific acceptance check has passed.

This document defines the architecture, requirements, installation, and testing of a managed egress layer for workloads and coding agents.

It is intended to be self-contained and usable without access to project discussion history. Requirements are normative; use `STATUS.md` and the source code to distinguish implemented behavior from planned work.

Deployment assumptions:

- Linux host (Ubuntu 24.04 is a tested reference environment, not a required host identity);
- Docker Engine, cgroup v2;
- one client may be a nested container runtime and is treated as an untrusted execution environment;
- Mihomo is the networking and proxy engine;
- production deployments MUST pin a stable Mihomo version and image digest rather than use `latest`.

The term “client container” means any Docker container using the egress service. The project is standalone and does not depend on a particular application, model gateway, or repository checkout path.

# 1. Solution summary

Deploy a dedicated Docker Compose stack, `egress`, based on Mihomo.

The backend is deployed separately from clients and their execution environments:

- Mihomo and `egressd` run in a dedicated Docker Compose stack.
- They do not run inside client containers or replace the host's general data/control plane.
- One backend serves multiple environments: hosts, Docker containers, and optionally other containers or VMs.
- **Install `egressctl` and `egress-run` locally in each environment where workloads execute.**
- Clients share the same protocol/API but use separate profiles, configurations, and backend addresses.
- `egress-run` is a local execution wrapper: it runs the command in the same environment as the wrapper, never remotely on the backend.
- Install the agent skill alongside client tools when an agent uses the service.
- TUN runs locally in a per-invocation namespace; the current design does not use a privileged host service.

Mihomo SHOULD handle as much functionality as possible through its native mechanisms:

- proxy providers/subscriptions;
- WireGuard/VLESS and other supported outbounds;
- provider updates;
- health/latency tests;
- per-client selector/default routing;
- SOCKS/listener data plane;
- GeoIP/GeoSite/ASN routing data;
- routing rules;
- DIRECT bypass.

Custom code SHOULD remain minimal and consist of:

1. `egressd`, a narrow public control/security adapter over the Mihomo API;
2. a shared client package with `egressctl` and `egress-run`;
3. provider adapters, **only if** a provider does not offer a Mihomo-compatible subscription;
4. bounded throughput benchmark;
5. domain availability probe across routes;
6. client-local strict/TUN namespace bootstrap and packet engine;
7. the agent skill at `skills/egress/SKILL.md`;
8. installation/bootstrap scripts and test harnesses;
9. user documentation, including agent usage guidance.

## 1.1. Deployment topology

The deployment topology is normative for the architecture.

### Backend Docker Compose stack `egress`

The main egress stack MUST run in Docker and MUST be deployed separately from client containers.

```text
Docker host
|
+-- container stack
|   `-- container
|
`-- egress stack
    |-- mihomo
    `-- egressd
```

Prefer running Mihomo and `egressd` as two services in one Compose project:

```text
egress-mihomo
egress-egressd
```

Combining them in one container requires a separate justification and MUST NOT automatically grant `egressd` the privileges required by Mihomo or TUN.

### Components inside the egress stack

The Docker stack contains:

- Mihomo;
- `egressd`;
- provider subscriptions/configuration;
- generated Mihomo configuration;
- persistent Mihomo state;
- per-client selector state;
- health/latency state;
- throughput/domain-probe state, if persisted;
- authenticated default and one-shot SOCKS/proxy endpoints;
- public narrow `egressd` API;
- Mihomo's own TUN endpoint, if required by backend-side features.

VPN credentials and subscription secrets are available to the egress stack, but not to client environments.

### Client package: installed where the workload runs

Install the same client package independently in each environment:

```text
host environment
├── egressctl
├── egress-run
└── client.yaml

Docker container
├── egressctl
├── egress-run
├── client.yaml
└── skills/egress/SKILL.md

other container / VM
├── egressctl
├── egress-run
└── client.yaml
```

Key invariant:

> `egress-run ROUTE -- command` runs `command` locally in the same execution environment as `egress-run`.

Therefore:

- Host `egress-run` starts a host process with the host filesystem and environment.
- Container `egress-run` starts a process inside that container with its filesystem and environment.
- The backend never receives an arbitrary command payload for normal proxy execution.
- `egressd` is not a remote execution service.

### Environment-specific client config

The same client binary MUST work with an environment-specific configuration.

Recommended canonical path:

```text
/etc/egress/client.yaml
```

and the override:

```text
EGRESS_CONFIG=/path/to/client.yaml
```

Minimum configuration:

```yaml
client_id: container-main
control_url: http://host.docker.internal:9091
proxy_host: host.docker.internal
default_proxy_port: 1080
oneshot_proxy_port: 1081
api_token_file: /run/secrets/egress_client_token
default_proxy_auth_file: /run/secrets/egress_egress_default_proxy_auth
environment: container
tun_backend: local-userns
tun_dns_servers: 1.1.1.1,1.0.0.1
```

For a host client:

```yaml
client_id: host-main
control_url: http://127.0.0.1:9091
proxy_host: 127.0.0.1
default_proxy_port: 1080
oneshot_proxy_port: 1081
api_token_file: /etc/egress/client.token
default_proxy_auth_file: /etc/egress/default-proxy.auth
environment: host
tun_backend: local-userns
tun_dns_servers: 1.1.1.1,1.0.0.1
```

The local `client_id` is for diagnostics. The server MUST determine the actual client identity from its credential/token and MUST NOT trust an arbitrary client-supplied ID.

Client configuration contains no provider/VPN secrets or Mihomo admin secret. The API token and default proxy credentials SHOULD be stored in separate secret files, not inline in YAML.

### Per-client default state

The default route and execution mode are **per client**, not global to the backend.

Example:

```text
host-main:
  default route = direct
  default mode  = proxy

container-main:
  default route = xeovo:fi-helsinki-03
  default mode  = proxy
```

Changing a container default MUST NOT change the host default, or vice versa.

Mihomo/config generation SHOULD provide a separate selector group for each provisioned client, for example:

```text
CLIENT::host-main
CLIENT::container-main
```

`egressd` changes only the selector associated with the authenticated client.

Provisioning and removing client profiles are administrative/bootstrap operations and are not part of the public agent API.

### Components installed on the host

The host may contain:

1. A regular copy of the client package (`egressctl`, `egress-run`, and host `client.yaml`).
2. Installation/bootstrap scripts.
3. Firewall rules.
4. A fixed acceptance/test harness.
5. A narrow deployment helper.
6. A Docker CIDR synchronization helper.
7. The client-local strict/TUN namespace and static packet engine.

The host client package is an ordinary backend client and does not receive Mihomo admin API access or provider secrets.

The host MUST NOT run a second persistent Mihomo instance or full egress control plane without a separate architecture decision.

### Backend access

One backend serves multiple local clients:

```text
HOST workload
    |
    | local egressctl / egress-run
    | config -> 127.0.0.1
    v
+----------------------------+
|                            |
|      egress        |----> Internet / VPN
|      egressd + Mihomo      |
|                            |
+----------------------------+
    ^
    | config -> host.docker.internal
    | local egressctl / egress-run
    |
Docker container workload
```

Agent-facing endpoints are published:

- on host loopback for host clients;
- on an allowed Docker gateway/interface for container clients;
- never on a public network interface.

The raw Mihomo admin API is not published on a public interface or directly to client networks. Only `egressd` and tightly restricted administrative procedures may access it.

### Privilege separation

For egress access, a client container MUST NOT receive:

- Docker socket;
- raw Mihomo admin API;
- provider subscription secrets/private keys;
- host root shell;
- the ability to change host nftables/routes;
- TUN access except through `/dev/net/tun` and a separate per-run user/network namespace; the outer container receives no `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, host networking, or privileged mode.

The client creates nested user/network namespaces. The data-plane engine receives only the open TUN file descriptor and selected route credentials. It does not launch workloads and is not a generic execution API.

# 2. Terms

## 2.1. Route

`route` is a logical egress destination.

Route IDs may identify:

- `direct`;
- a location from a configured provider;
- a location from a provider-free source, including VLESS and other protocols supported by Mihomo.

Examples:

```text
direct
blanc:fi-helsinki-03
xeovo:nl-amsterdam-01
vless:personal-01
```

`direct` is a **full route ID**, not the absence of a route.

It is available for:

- persistent default routing;
- an explicit one-shot route;
- domain probes;
- benchmarks/health tests where meaningful.

## 2.2. Client profile

`client profile` is the identity of one execution environment using the backend.

Examples:

```text
host-main
container-main
research-container-02
```

The backend derives client identity from the credential/token. A profile defines:

- a separate persistent default route;
- a separate persistent default execution mode;
- allowed capabilities/modes;
- audit identity;
- default proxy authentication identity/selector;
- rate/resource limits, if required.

One execution environment usually has one profile, but the architecture MUST allow multiple profiles.

## 2.3. Default route

The persistent route for a **specific client profile**, used by `egress-run --default` and that client's default proxy identity.

The default route is NOT the host OS default route and is NOT one global selector shared by all clients.

Ordinary host services that are not launched through the egress interface MUST NOT be routed through egress automatically.

## 2.4. One-shot route

The route selected for one operation or command. A one-shot route does not change the client's default route.

One-shot operations MUST support **concurrent execution through different routes**, including `direct`.

## 2.5. Execution mode

An execution mode is independent of route:

- `proxy` — userspace SOCKS/proxy path;
- `tun` — strict, network-enforced path that does not require applications to understand proxies.

Route and execution mode are independent:

```text
route=direct, mode=proxy
route=blanc:fi-helsinki-03, mode=proxy
route=xeovo:nl-amsterdam-01, mode=tun
route=direct, mode=tun
```

For `direct + tun`, TUN means strict/network-enforced execution without proxy/VPN egress; an implementation may use an isolated network namespace and direct NAT/path.

TUN support is a capability of the specific execution environment. The client MUST NOT silently fall back from TUN to proxy mode.

## 2.6. Local client

`egressctl` and `egress-run` are local clients.

- `egressctl` manages the backend through the public `egressd` API.
- `egress-run` obtains the required network capability/session from the backend and launches a local command.
- Neither client contains provider secrets or requires the Mihomo admin secret.
- The same client package works on a host and inside containers with different configurations.

# 3. Architectural principles

1. **Mihomo first.** Do not build protocol engines, provider updaters, selectors, or latency engines that Mihomo already provides.
2. **Local execution.** `egress-run` starts commands in its own execution environment; the backend never executes commands remotely.
3. **One backend, multiple clients.** Hosts and containers share one backend but retain separate client profiles, configurations, and defaults.
4. **Treat client containers as untrusted.** Do not give them the Docker socket, host root shell, provider secrets, or Mihomo admin secret.
5. **Public API parity.** `egressctl` is a convenient client of the public `egressd` API, not a security boundary.
6. **No hidden privileged CLI.** `egressctl` MUST NOT have administrative endpoints or capabilities unavailable to a direct API client with the same credential.
7. **Separate data plane and control plane.** Control uses `egressd`; traffic uses Mihomo/data-plane endpoints.
8. **Keep internal Docker destinations DIRECT for container clients.** This is a hard security/routing invariant.
9. **Keep the operator's control plane DIRECT.** Additional direct destinations are configured explicitly by the operator.
10. **Per-client defaults.** A client's default route/mode MUST NOT affect other clients.
11. **Default routes and one-shot routes are independent.**
12. **Support concurrent one-shot sessions.** A single mutable global `ONESHOT` selector guarded by a lock is not an acceptable final design.
13. **Make `direct` available wherever a proxy/VPN location can be selected.**
14. **Proxy first, TUN when needed.** Proxy mode is required for v1; TUN support may vary by execution environment.
15. **No global host VPN.** Egress applies only to explicitly connected clients/commands.
16. **Keep provider subscriptions and keys on the backend.**
17. **Fail safe with last-known-good state.** A failed provider update MUST NOT destroy working configuration.
18. **Measure domain availability with a request**, not only by inferring it from GEO data.
19. **No arbitrary remote execution.** The public API MUST NOT accept arbitrary shell commands for execution on the host/backend.
20. **Report capabilities explicitly.** Unsupported TUN, UDP, or other capabilities MUST be diagnosable.

# 4. Goals

From a host or container execution environment, users/agents should be able to:

- install the same `egressctl`/`egress-run` client package;
- connect to one backend through environment-specific configuration;
- inspect their own client identity and capabilities and list available routes;
- inspect and change their own default route/mode without affecting other clients;
- run local commands through any route, including `direct`;
- run concurrent one-shot commands through different routes;
- update providers, optionally through a specified working route;
- check route health/latency and domain availability through selected or all routes;
- run bounded throughput benchmarks and rank routes by country, provider, latency, speed, health, or domain availability;
- use GeoIP/GeoSite/ASN metadata as additional context;
- diagnose the deployment with `egressctl doctor` and use the agent skill;
- install/configure backend and clients using reproducible documentation;
- run local commands in strict/TUN mode when the environment supports it.

# 5. Non-goals for v1

The following are not included in v1:

- a global VPN for the entire host;
- changing the host default route;
- public proxy/API service;
- direct agent access to the full Mihomo API;
- custom implementations of WireGuard/VLESS;
- scraping provider web portals;
- full-fleet speed tests on every route selection;
- disabling the container runtime to enable strict mode;
- giving a container the Docker socket;
- generic privileged RPC / arbitrary host shell.

# 6. Reference infrastructure and DIRECT control plane

The original integration path used a client container connecting to a service on the host. The same pattern applies to any operator-selected local control-plane service:

```text
container (container runtime)
   |
   | Docker default bridge
   v
host.docker.internal / docker0 gateway
   |
   +--> host relay :4001
          |
          +--> 127.0.0.1:4000 operator control-plane service
```

Egress MUST NOT break this path.

Hard DIRECT MUST cover **all local Docker networks** and the explicitly configured operator control plane.

If a directly accessed service is on another machine, the operator MUST be able to configure that destination as HARD DIRECT.

# 7. Threat model

## 7.1. Untrusted subjects

- a client container agent;
- commands/code launched by the agent;
- any client with only an ordinary public API credential;
- external websites/repositories;
- provider subscription content;
- other containers on the Docker network without valid client credentials.

## 7.2. Trusted components

- host administrator;
- pinned Mihomo image/binary;
- `egressd`;
- install/bootstrap scripts;
- local per-invocation TUN namespace bootstrap;
- provider secret store.

`egressctl` and `egress-run` are not security boundaries: untrusted code can make the same public API calls directly.

## 7.3. Security invariants

1. A client container MUST NOT see provider subscription URLs, private keys, or VLESS secrets.
2. A client container MUST NOT see the Mihomo admin secret.
3. The Mihomo admin API MUST NOT be published on the host or Docker gateway as a client-facing service.
4. The public `egressd` API MUST be safe when called directly without `egressctl`.
5. The public API MUST NOT provide generic `exec`, arbitrary file access/config patching, or raw Mihomo API passthrough.
6. `egressctl` MUST use the same public API and MUST NOT have hidden privileged transport.
7. Client identity MUST come from the credential/token, not a trusted request `client_id` field.
8. Agent-facing ports MUST NOT listen on a public network interface.
9. Internal Docker CIDRs MUST remain DIRECT in default, one-shot, probe, and strict modes for container clients.
10. Operator-defined HARD DIRECT destinations take precedence over the selected route.
11. Provider updates use DIRECT by default. Updating through a route MUST NOT require client-side disclosure of provider secrets.
12. Local TUN bootstrap MUST NOT become a root-shell API; drop workload capabilities before execution.
13. Automatic fallback from a container runtime to `runc` is prohibited.
14. Provider configuration/subscription parsing is an input-validation boundary.
15. A client MUST NOT change another client's default route/mode through the public API.
16. Provider/API credentials from different trust domains MUST be isolated.
17. In ordinary proxy mode, `egress-run` MUST NOT send command arguments or environment to the backend.

# 8. Overall architecture

```text
                              HOST

   host workload                              Docker container workload
        |                                              |
   egressctl / egress-run                         egressctl / egress-run
        |                                              |
   client.yaml:                                  client.yaml:
   127.0.0.1                                    host.docker.internal
        |                                              |
        +----------------------+-----------------------+
                               |
                               v
                  +-----------------------------+
                  | egress Docker stack |
                  |                             |
                  | +---------+      private    |
                  | | egressd |-----------------+----+
                  | +----+----+                      |
                  |      |                           v
                  |      |                    +-------------+
                  |      |                    |   Mihomo    |
                  |      |                    | providers   |
                  |      |                    | selectors   |
                  |      |                    | rules/GEO   |
                  |      |                    +------+------+ 
                  +------+---------------------------+-------+
                         |                           |
                 metrics/catalog/state         Internet/VPN

client-local per-run optional:
  nested user/network namespace + TUN packet engine

Agent/client-facing:
  :1080  authenticated per-client default proxy listener
  :1081  authenticated route-addressable one-shot listener
  :9091  public narrow egressd API

Never client-facing:
  Mihomo admin API
  provider secrets
  generic host exec
```

The host and containers use one backend but separate client configurations and credentials.

Default state is isolated by client profile. One-shot sessions are isolated by route/session and may run concurrently.

# 9. Mihomo responsibilities

Mihomo is responsible for:

- proxy providers/subscriptions;
- provider health-check;
- per-node delay tests;
- WireGuard/VLESS and other supported protocols;
- per-client selector groups (`CLIENT::<client_id>` or equivalent);
- authenticated default SOCKS listener;
- route-addressable authenticated one-shot listener;
- routing rules/`IN-USER` or an equivalent mechanism confirmed by testing;
- GeoIP/GeoSite/ASN rules;
- HARD DIRECT bypass;
- provider refresh API;
- direct route.

A shared aggregate group may be used as an implementation detail, but **MUST NOT turn the default route into one global selector shared by all clients**.

Pin the production version by tag and digest after the PoC. Verify exact syntax/features against that pinned version.

# 10. egressd responsibilities

`egressd` is a narrow public control/security adapter.

It is responsible for:

- communicate with the internal Mihomo API;
- provide an authenticated public API matching `egressctl` capabilities;
- authenticate client profiles using credentials/tokens;
- return `/self` identity/capabilities;
- normalize the location catalog and stable route IDs;
- bind client profiles to Mihomo per-client selector groups during provisioning;
- store per-client default route/mode;
- change only the authenticated client's selector;
- update providers and reconciliation state;
- change only affected client defaults to `direct` when a route disappears;
- collect/store health metadata;
- run domain probes and bounded benchmarks;
- manage one-shot route credentials, mappings, and session metadata;
- issue network capabilities to `egress-run` without launching user commands;
- store audit logs with client identity;
- avoid disclosing provider secrets.

`egressd` MUST NOT:

- accept arbitrary shell commands for execution;
- execute client workloads;
- act as a generic proxy to the Mihomo admin API;
- provide arbitrary namespace/firewall operations;
- trust a client-provided `client_id` instead of the authenticated identity.

## 10.1. Public API parity rule

All ordinary `egressctl` control-plane capabilities MUST be exposed through the documented public `/v1` API.

`egressctl` MUST NOT have hidden privileged endpoints. A direct HTTP client using the same token has exactly the same permissions as `egressctl`.

Exception: local execution mechanics in `egress-run` (such as local `exec()` or a per-run TUN namespace) are not server control-plane capabilities and do not need REST endpoints.

# 11. Providers/subscriptions

## 11.1. Native provider — preferred option

If a provider supplies a Mihomo/Clash-compatible YAML, URI, or base64 subscription, use `proxy-providers` directly.

Mihomo supports `http`, `file`, and `inline` providers, interval updates, health checks, filters/exclude-filters, and overrides.

Conceptual example:

```yaml
proxy-providers:
  blanc:
    type: http
    url: <secret>
    path: ./providers/blanc.yaml
    interval: 21600
    proxy: DIRECT
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 300
      timeout: 5000
    override:
      additional-prefix: "[blanc] "
```

## 11.2. Adapter path

If a provider supplies a WireGuard `.conf` or another incompatible format:

```text
provider source -> provider-specific adapter -> Mihomo provider YAML
```

The adapter MUST:

- include fixtures without secrets;
- replace files atomically;
- preserve last-known-good state;
- never log private keys;
- have dedicated tests.

## 11.3. Custom VLESS

Custom VLESS nodes SHOULD be configured as static proxies or through a file provider.

# 12. Subscription updates

Required commands:

```bash
egressctl update
egressctl update --provider blanc
egressctl update --provider xeovo
egressctl update --if-older-than 6h
```

The user SHOULD also be able to select a transport route for provider refresh when direct updates are blocked:

```bash
egressctl update --provider blanc --via xeovo:fi-helsinki-03
egressctl update --provider blanc --via direct
```

`--via` selects the egress route for the backend-side download/update; it MUST NOT expose the subscription URL/token to the client.

If native Mihomo `proxy-providers` cannot safely refresh through an arbitrary route without a global selector race, `egressd` MUST perform a controlled fetch through a route-specific data plane, validate and atomically write the provider artifact, then reconcile. It MUST NOT temporarily change a shared default selector for a provider update.

TUN transport for provider refresh may be added in Phase 2 if needed; proxy-capable HTTP fetch is preferred.

After an update, the system MUST:

1. Update the provider through Mihomo or an adapter/controlled fetch.
2. Reload the inventory and normalized catalog.
3. Update one-shot route mappings.
4. Check defaults for all client profiles.
5. Change only a client whose default route disappeared to `direct`.
6. Run a fast health check for new/changed nodes.
7. Update GEO assets when required by the freshness policy.
8. Record an audit event with caller identity and update method.

A failed update MUST NOT destroy the previous working provider state.

# 13. Route catalog

Agent-facing route object:

```json
{
  "id": "blanc:fi-helsinki-03",
  "provider": "blanc",
  "display_name": "Helsinki 03",
  "country": "FI",
  "city": "Helsinki",
  "protocol": "wireguard",
  "healthy": true,
  "latency_ms": 43,
  "exit_ip": "...",
  "exit_country": "FI",
  "asn": 12345,
  "last_test_at": "...",
  "benchmark_mbps": 210,
  "benchmark_at": "..."
}
```

`direct` MUST appear in the catalog as a synthetic route:

```json
{
  "id": "direct",
  "provider": "builtin",
  "display_name": "Direct",
  "protocol": "direct"
}
```

A stable route ID MUST NOT depend solely on a mutable display name.

# 14. GeoIP / GeoSite / ASN

## 14.1. Using Mihomo GEO data

Configure Mihomo to use and automatically update GEO data.

Recommended upstream:

```text
https://github.com/MetaCubeX/meta-rules-dat
```

Use supported Mihomo assets:

- `geoip.dat` or `geoip.metadb`/`country.mmdb`;
- `geosite.dat`;
- `GeoLite2-ASN.mmdb`.

Use Mihomo's native configuration options:

```yaml
geo-auto-update: true
geo-update-interval: 24
geox-url:
  geoip: ...
  geosite: ...
  mmdb: ...
  asn: ...
```

## 14.2. Uses of GEO data

GEO data are used for:

- verifying the actual exit IP country;
- ASN enrichment;
- classifying routes;
- GEOIP/GEOSITE routing rules;
- heuristics when selecting candidates;
- explaining to the agent why a route is or is not suitable.

## 14.3. Limitation

GeoIP/GeoSite **are not an authoritative source for block status**.

If the question is whether `example.com` is reachable from country X, the final answer MUST be based on a real probe through a route.

GEO data provide context; a probe provides evidence.

# 15. Default route

The default route is per client.

Mihomo SHOULD have a separate selector group for every provisioned client:

```text
CLIENT::host-main = select(direct + all allowed provider nodes)
CLIENT::container-main = select(direct + all allowed provider nodes)
```

The `egressd` public API operates in the context of the authenticated caller.

CLI:

```bash
egressctl route current
egressctl route set direct
egressctl route set blanc:fi-helsinki-03
```

Use the `route` subcommand; the following are the supported forms:

```bash
egressctl status
egressctl route set direct
egressctl route set ROUTE
```

`route current` returns at least:

- authenticated `client_id`;
- route ID;
- provider/display name;
- current health;
- default execution mode;
- selector/backend reconciliation status.

Changing a route for one client MUST NOT affect other clients' selectors/defaults.

If a selected route disappears after a provider update, only the affected client profile falls back to `direct` and receives a warning/audit event.

# 16. Default execution mode

The system stores the default execution mode **per client**:

```text
proxy | tun
```

CLI:

```bash
egressctl mode current
egressctl mode set proxy
egressctl mode set tun
```

This does not change global host networking or the mode of other clients.

`egress-run --default` uses this mode locally in the corresponding execution environment.

The backend `/self`/capabilities endpoint reports which modes are allowed for a client profile. The local client also checks for local namespace/TUN capability.

If TUN is unavailable, the operation MUST return an explicit `unsupported`/`unsupported_in_environment` result and MUST NOT silently fall back to proxy mode.

# 17. Proxy data plane

## 17.1. Per-client default listener

`1080/tcp` — authenticated default SOCKS endpoint.

Each client receives its own default proxy identity/credential. Mihomo uses this identity to route traffic to that client's selector:

```text
client-default:host-main       -> CLIENT::host-main
client-default:container-main -> CLIENT::container-main
```

Actual usernames/passwords do not need to contain the client ID; opaque credentials are preferred.

Routing order:

1. hard DIRECT bypass rules;
2. client identity -> client selector;
3. selector -> current per-client default route.

This lets generic proxy-aware applications use the persistent client default without `egress-run`.

## 17.2. One-shot listener

`1081/tcp` — authenticated route-addressable SOCKS endpoint.

It MUST NOT use one mutable `ONESHOT` selector.

The route is selected through SOCKS authentication identity/`IN-USER` rules or another Mihomo-native route-addressable mechanism confirmed by the PoC.

Conceptually:

```text
(client A, route direct)               -> DIRECT
(client A, route blanc:fi-helsinki-03) -> [blanc] Helsinki 03
(client B, route xeovo:nl-01)          -> [xeovo] Amsterdam 01
```

Hard DIRECT bypass rules take precedence over the selected one-shot route.

## 17.3. Credentials/capabilities

The default proxy credential is a client-scoped secret.

A one-shot capability may use:

- a client+route credential created during reconciliation;
- short-lived session credential;
- another mechanism that supports concurrency and audit identity without a mutable global selector.

Provider private credentials MUST never be given to clients.

# 18. One-shot concurrency

Required behavior:

```bash
egress-run blanc:fi-helsinki-03 -- cmd-A
egress-run xeovo:us-new-york-02 -- cmd-B
egress-run direct -- cmd-C
```

may run concurrently in the same or different execution environments.

Concurrent calls MUST NOT:

- change any client's default route;
- change one another's routes;
- block one another using a global mutex;
- depend on a single mutable `ONESHOT.now` value.

`egressd` stores active one-shot sessions/jobs only as observability state:

```bash
egressctl oneshot active
```

By default, the command shows sessions belonging to the authenticated client; cross-client observability is not a public capability.

Example:

```json
{
  "id": "job-...",
  "client_id": "container-main",
  "route": "xeovo:us-new-york-02",
  "mode": "proxy",
  "started_at": "..."
}
```

There is no global “current one-shot route.”

# 19. One-shot routing map generation

During provider/client reconciliation, the config generator builds a mapping:

```text
(client identity, stable route ID)
    -> Mihomo proxy/group
    -> authenticated one-shot data-plane identity
```

Requirements:

- `direct` is always present;
- mappings are updated atomically;
- provider name collisions are prevented with prefixes/stable internal names;
- capabilities do not reveal provider keys;
- a disappeared route is no longer issued to new sessions;
- an existing session receives a deterministic error/termination if its upstream route disappears;
- generated identity/rule counts are measured on a real catalog with hundreds of routes and validated in the PoC;
- if client×route cardinality is too large, another Mihomo-native concurrency-safe mechanism may replace the mapping, but a shared mutable selector is not acceptable.

# 20. `egress-run` proxy mode

`egress-run` is a local, thin execution client.

Interface:

```bash
egress-run --default -- command ...
egress-run ROUTE -- command ...
egress-run direct -- command ...
egress-run --mode proxy ROUTE -- command ...
```

Proxy-mode algorithm:

1. Load local `client.yaml`.
2. Make an authenticated call to the public `egressd` API.
3. For `--default`, retrieve the current per-client default route/mode.
4. Request or retrieve a route-specific proxy capability/session.
5. Configure the proxy path for the **locally launched** child process.
6. Locally `exec`/spawn the target command.
7. Preserve exit codes and forward signals correctly.
8. Close the session/update observability state.

The command/argv is never sent to the backend for execution.

For maximum compatibility, the wrapper SHOULD support:

1. Native proxy environment/configuration for known clients, when reliable.
2. `proxychains-ng`/equivalent for generic dynamically linked TCP clients.

Document proxy-mode limitations:

- static binaries/direct syscalls may bypass `LD_PRELOAD`;
- UDP/QUIC support is incomplete;
- setuid binaries are incompatible;
- some runtimes ignore proxy environment variables;
- environment variables alone do not enforce network routing.

If an application requires guaranteed traffic interception, use `--mode tun` only when the environment has the required capabilities.

# 21. Strict/TUN mode

`egress-run --mode tun ROUTE -- command ...` runs the command locally in the
calling environment and routes its traffic through a fresh TUN for each
invocation. This applies to Linux hosts and Docker containers. `proxy` and
`tun` are independent per-invocation modes; each invocation can select its own
route. If TUN setup fails, the command does not start and there is no proxy
fallback.

The client creates nested user and network namespaces, configures TUN and
default routes in the new network namespace, redirects DNS requests to a local
forwarder, and starts the workload after passing the TUN file descriptor to the
packet engine. Nameserver addresses from the client's `/etc/resolv.conf` are
assigned to loopback only inside the new namespace; the outer namespace and
its resolver are unchanged. The forwarder sends queries through the selected
TUN route. The workload, argv, environment, cwd, and stdio remain in the client
environment. The engine receives only the TUN descriptor and one-shot SOCKS
endpoint/credentials for the selected route; it receives no argv and launches
no workload. The `direct` route uses direct packet forwarding. When the
command exits, its engine stops and the namespace is released.

For the default Cloudflare resolver IPs (`1.1.1.1`, `1.0.0.1`), the client
first sends DNS queries over DNS-over-HTTPS to a pinned IP and verifies TLS for
`cloudflare-dns.com`. If DoH is unavailable, it tries configured resolvers over
UDP and then TCP. Custom resolvers use only their configured addresses, first
over UDP and then TCP. All connections use the selected TUN route; DNS is not
sent through the outer Docker resolver. A nameserver matching every configured
`tun_dns_servers` cannot be used as an upstream because it is intercepted by
the local forwarder in the private namespace. In that case startup fails with
an explicit error instead of timing out.

On the host, TUN requires Linux, access to `/dev/net/tun`, util-linux `unshare`,
iproute2, and enabled unprivileged user namespaces. Each invocation uses
`unshare --user --map-root-user --net`: the caller's UID/GID map to UID/GID 0
inside the new user namespace. This gives bootstrap `CAP_NET_ADMIN` only in
that namespace for `TUNSETIFF` and network setup, without capabilities in the
outer host/container namespace. The workload sees UID 0 inside the namespace,
mapped to the caller's UID outside. A Docker client additionally receives only
`/dev/net/tun` and a narrow seccomp profile allowing the required `unshare`
flags. Do not grant the outer container `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, host
networking, privileged mode, or the Docker socket. After network setup, the
workload drops capabilities, receives `no_new_privs`, and cannot create or
enter other namespaces.

Docker acceptance uses an ordinary temporary container and checks proxy, TUN,
local workload execution, and a real HTTPS request. It does not use a shared
helper or move the workload/control process outside the client container.

# 22. Hard DIRECT bypass

The following traffic MUST never pass through egress:

- `127.0.0.0/8`;
- `::1/128`;
- all current local Docker network CIDRs;
- Docker bridge gateways;
- `host.docker.internal`;
- operator-defined `EGRESS_BYPASS_CIDRS`;
- operator-defined `EGRESS_BYPASS_DOMAINS`;
- egress control-plane service endpoints.

Do not unconditionally bypass every RFC1918 range: a private remote network may be reachable only through a VPN.

Generate Docker CIDRs on the host using `docker network inspect`; egress containers MUST NOT receive the Docker socket.

# 23. Health/latency checks

Mihomo provider health checks and per-node delay tests are the foundation.

CLI:

```bash
egressctl test
egressctl test ROUTE
egressctl test --provider blanc
egressctl test --country FI
```

The primary metric is HTTP/HTTPS delay to a configurable 204 endpoint.

Do not call this an ICMP ping.

Test `direct` as a baseline as well.

# 24. Domain availability checks through routes

This is a required feature.

CLI:

```bash
egressctl probe-domain example.com
egressctl probe-domain example.com --all
egressctl probe-domain example.com --country FI
egressctl probe-domain example.com --provider blanc
egressctl probe-domain example.com --routes direct,blanc:fi-01,xeovo:fi-02
egressctl probe-domain example.com --parallel 16 --json
```

Additional parameters:

```text
--scheme http|https
--port
--path /
--method HEAD|GET
--timeout
--expected-status 200-399
--check dns,tcp,tls,http
--max-routes N
```

Return at least the following fields for each route:

```json
{
  "route": "blanc:fi-helsinki-03",
  "reachable": true,
  "dns_ok": true,
  "tcp_ok": true,
  "tls_ok": true,
  "http_ok": true,
  "status": 200,
  "latency_ms": 182,
  "resolved_ip": "...",
  "exit_ip": "...",
  "exit_country": "FI",
  "asn": 12345,
  "error": null
}
```

The probe MUST use a route-specific one-shot path so probes can safely run in parallel.

Include `direct` alongside proxy routes.

Distinguish at least:

- DNS failure;
- TCP timeout/refused;
- TLS/SNI/certificate failure;
- HTTP status denial;
- redirect;
- connection reset;
- generic proxy failure.

This gives the agent useful information about the type of failure or blocking.

# 25. Throughput benchmark

Mihomo delay tests are not speed tests.

CLI:

```bash
egressctl benchmark ROUTE
egressctl benchmark --country FI --top-latency 5
egressctl benchmark --provider xeovo --top-latency 10 --bytes 8M
```

Pipeline:

```text
all routes -> cheap health/latency -> shortlist -> bounded download benchmark
```

Do not automatically benchmark hundreds of routes.

Benchmarks MUST use a route-specific one-shot proxy path and support parallel jobs with a configurable cap.

`direct` may be used as a baseline.

# 26. `egressctl best`

Commands:

```bash
egressctl best
egressctl best --country NL
egressctl best --provider xeovo
egressctl best --country US --max-latency 150
egressctl best --country FI --domain example.com
egressctl best --speed-aware
```

If `--domain` is specified, a route is a candidate only after a recent successful domain probe or an on-demand probe of the shortlist.

Default ranking:

1. country/provider filters;
2. health;
3. domain reachability, if a domain is specified;
4. freshness;
5. latency;
6. optional throughput score;
7. GEO verification/confidence.

The ranking formula MUST be transparent and included in the JSON explanation.

# 27. API egressd

Version prefix `/v1`.

The public API is for ordinary clients and corresponds to the control capabilities exposed by `egressctl`.

At minimum:

```text
GET  /v1/self
GET  /v1/status
GET  /v1/capabilities

GET  /v1/routes
GET  /v1/routes/{id}

GET  /v1/default
PUT  /v1/default
GET  /v1/default/mode
PUT  /v1/default/mode

GET  /v1/providers
POST /v1/providers/{id}/update
POST /v1/providers/update-all

POST /v1/test
POST /v1/routes/{id}/test
POST /v1/probe-domain
POST /v1/benchmark
GET  /v1/benchmarks

GET  /v1/proxy/default-capability

POST /v1/oneshot/session
DELETE /v1/oneshot/session/{id}
GET  /v1/oneshot/active

POST /v1/reconcile
GET  /v1/health
GET  /v1/ready
GET  /v1/doctor

GET  /v1/geo/status
POST /v1/geo/update
```

Semantics:

- The authenticated token identifies the client profile.
- `/v1/self` returns `client_id`, environment label, allowed modes/capabilities, and API compatibility metadata.
- `/v1/default*` always refers to the authenticated client.
- The public API does not accept an arbitrary target `client_id` to change another client's defaults.
- `/v1/proxy/default-capability` returns the authenticated client's default proxy endpoint/credential metadata without provider secrets.
- `/v1/oneshot/session` does not change a selector/default and returns a route-specific data-plane capability.
- The API never returns provider private credentials or executes arbitrary user commands.

Administrative provisioning of clients/secrets/configuration MUST use host-side bootstrap/admin tooling, not the ordinary public API.

## 27.1. Public API parity

Every public control action in `egressctl` MUST have a documented API endpoint/operation.

Integration tests SHOULD compare CLI behavior with direct API calls.

The absence of a CLI command is not a security control: the public API itself MUST enforce permissions.

# 28. Authentication / authorization

Each execution environment receives a separate client credential.

Examples:

```text
host-main token
container-main token
```

Bind each token server-side to its client profile and permissions/capabilities.

A container token may allow:

- self/status/list;
- read/set **its own** default route/mode;
- provider update trigger;
- health/probe/benchmark;
- create own one-shot sessions;
- read own active sessions;
- trigger GEO status/update operations, if allowed by policy.

It MUST NOT allow:

- impersonate another client;
- change another client's defaults;
- raw Mihomo API;
- read provider secrets;
- arbitrary config patch;
- arbitrary host command;
- perform firewall/network namespace operations directly;
- client provisioning/removal.

The Mihomo admin secret is available only to `egressd`/backend administration.

The client token is considered accessible to all code in its execution environment. Its permissions MUST therefore be safe for untrusted code in that environment.

# 29. Client configuration and discovery

The client package supports configuration discovery in this order:

1. explicit `--config PATH` (if supported by the command);
2. `EGRESS_CONFIG`;
3. `/etc/egress/client.yaml`;
4. optional user config path, only if explicitly documented.

The CLI provides:

```bash
egressctl config show
egressctl config validate
egressctl whoami
```

`config show` redacts/masks tokens and credentials by default.

At startup, the client SHOULD compare the local expected `client_id` with `/v1/self`; a mismatch is a configuration error.

Host and container configurations MUST be generated by provisioning/install tooling, not copied manually with secrets replaced.

# 30. CLI and `egress-run` contract

## 30.1. `egressctl`

Required commands:

```bash
egressctl status
egressctl whoami

egressctl route current
egressctl route set ROUTE

egressctl mode current
egressctl mode set proxy|tun

egressctl providers
egressctl update
egressctl update --provider PROVIDER
egressctl update --if-older-than 6h
egressctl update --provider PROVIDER --via ROUTE

egressctl list
egressctl list --country FI
egressctl list --provider blanc
egressctl list --healthy

egressctl test
egressctl test ROUTE

egressctl probe-domain DOMAIN --all
egressctl benchmark ROUTE

egressctl best [filters]

egressctl oneshot active

egressctl geo status
egressctl geo update

egressctl config show
egressctl config validate
egressctl doctor
```

All read commands support `--json` where applicable.

Exit codes are stable and documented.

`egressctl` uses only the documented public `egressd` API.

## 30.2. `egress-run`

```bash
egress-run --default -- command ...
egress-run ROUTE -- command ...
egress-run direct -- command ...
egress-run --mode proxy ROUTE -- command ...
egress-run --mode tun ROUTE -- command ...
```

`--default` uses the persistent default route and mode of the authenticated client profile.

An explicit route never changes the default route.

An explicit mode never changes the persistent default mode.

The target command runs locally; the ordinary backend API does not receive commands for execution.

`egress-run` MUST:

- use the same config/token discovery as `egressctl`;
- pass argv correctly without shell reparsing by default;
- preserve the exit status;
- forward SIGINT/SIGTERM;
- cleanup session capability;
- report unsupported modes clearly;
- support concurrent local invocations.

# 31. Agent skill

Required deliverable:

```text
skills/egress/SKILL.md
```

Install the skill in the agent execution environment alongside the local client package. It teaches agents to use local `egressctl`/`egress-run` commands.

The skill teaches agents to:

1. Start with `egressctl whoami`/`status` to inspect the client profile and capabilities.
2. Check `route current` before changing a persistent default.
3. Remember that defaults are per client: changing one profile's default does not affect another.
4. Prefer one-shot routing for a single location-specific task.
5. Use `direct` as a normal route and for baseline comparisons.
6. Do not change the default route for a single command.
7. Use fresh health metrics when selecting routes.
8. Use `probe-domain` to check site availability; do not rely only on GeoIP/GeoSite.
9. Update providers after repeated failures or stale inventory; use a working `--via ROUTE` if needed.
10. Do not benchmark the entire fleet; start with a shortlist.
11. Choose TUN only when proxy mode is insufficient and `whoami`/capabilities reports support.
12. Do not expect silent fallback from TUN to proxy.
13. Never try to route HARD DIRECT/internal Docker destinations through a VPN.
14. Never access the Mihomo admin API or provider secrets directly.
15. Check `route current` after `route set`.
16. For country-specific tasks, verify exit country with GEO lookup/exit-IP checks when needed.
17. For concurrent tasks, use independent `egress-run ROUTE` invocations instead of changing the default.
18. Understand proxy-mode limits (`proxychains`, UDP/QUIC, static/setuid binaries).
19. Do not request host root or the Docker socket to use TUN from a container.

The skill MUST include a decision tree and examples for at least:

- current/set default;
- direct one-shot;
- VPN one-shot;
- 2+ concurrent one-shot;
- domain probe across routes;
- provider update through DIRECT and through a working route;
- proxy->tun decision;
- diagnose `unsupported_in_environment`.

# 32. Installation and bootstrap

The project MUST provide separate backend and client installers.

At minimum:

```text
INSTALL.md
scripts/install-backend-host.sh
scripts/uninstall-backend-host.sh
scripts/verify-backend-host.sh
scripts/provision-client.sh
scripts/install-client.sh
scripts/verify-client.sh
scripts/bootstrap-dev-access.sh   # when using a narrow dev/test bridge
```

`INSTALL.md` MUST describe two independent steps:

### A. Backend deployment

- prerequisites;
- persistent paths and `findmnt`/`df`;
- Docker/Compose;
- secrets/provider subscriptions;
- firewall;
- Compose deployment;
- pinned Mihomo image/version;
- GEO data;
- loopback/docker-gateway bindings;
- client provisioning;
- host acceptance harness;
- rollback/uninstall.

### B. Client installation in each execution environment

For each client profile:

1. install the same `egressctl` + `egress-run` binaries/package;
2. install an environment-specific `/etc/egress/client.yaml`;
3. provide a separate API token and default-proxy credentials as read-only secrets;
4. install the skill if the environment runs an agent;
5. run `egressctl config validate`;
6. run `egressctl whoami`;
7. run `egressctl doctor`/smoke tests.

Production container installation MUST be reproducible: preferably bake the client package and skill into the image or mount them as deterministic read-only artifacts. Do not rely only on manual installation into a writable container layer that disappears on recreation.

The provisioning tool MUST generate separate client bundles/config templates for hosts and containers without provider secrets.

# 33. Optional constrained development environments

Contributors may develop or test the local client from inside an already-running container. Such an environment is untrusted and may not provide:

- host root;
- host `systemctl`;
- Docker socket;
- host network namespace;
- access to host `/etc`;
- Mihomo admin API.

## 33.1. Checks available to a client/container contributor

After initial bootstrap/provisioning, a contributor should be able to:

- edit the checked-out repository;
- run unit/parser/config-generation tests;
- install/update a development version of the local client package inside the container;
- validate the container's `client.yaml`;
- run `egressctl whoami/status`;
- call the public `egressd` API through `egressctl` and directly for API parity tests;
- access authenticated SOCKS endpoints;
- read/change only its own default route/mode;
- run local `egress-run` and verify the process remains inside the container;
- run concurrent one-shot proxy tests;
- update providers through the public API;
- run domain probes/benchmarks;
- check HARD DIRECT and operator control-plane connectivity;
- verify that its actions do not change another client's default (using a host acceptance report or a dedicated cross-client assertion);
- read safe host-test reports.

## 33.2. Host-side operations

The primary egress runtime runs in the `egress` Docker Compose stack.

Host privileges are required for:

- initial backend bootstrap;
- Docker Compose deploy/redeploy;
- firewall/persistent dirs;
- publishing loopback/Docker-gateway ports;
- Docker-CIDR synchronization;
- host client provisioning/install;
- host acceptance harness;
- local client TUN namespace and static packet engine.

Mihomo and `egressd` are not installed as persistent host daemons.

An administrator performs the initial bootstrap or provides a preinstalled narrow deployment/test interface.

Afterward, a container-based contributor may:

- validate staged backend/client bundle;
- request deploy/redeploy only the egress stack from its fixed deployment path;
- retrieve filtered status/logs;
- run the fixed host acceptance harness;
- sync Docker CIDRs;
- request deterministic installation/update of a client artifact in an approved target environment, if such a helper is implemented.

Prohibited operations:

- arbitrary shell commands on the host;
- arbitrary compose path/container management;
- arbitrary file copying into `/etc`;
- Docker socket passthrough;
- generic host execution of a container workload.

For a PoC, a manual administrator loop is acceptable: the contributor prepares artifacts/instructions, an administrator runs host-only commands and returns a structured report. Production acceptance requires a reproducible installer/harness.

# 34. Host acceptance harness

A fixed, root-owned test harness is required, for example:

```text
egress-host-test
```

It runs only predefined checks and returns a JSON report without secrets:

- socket binds/public exposure/firewall;
- Compose health;
- raw Mihomo API reachability policy;
- Docker CIDR bypass artifact freshness;
- persistent directories/permissions;
- image versions/digests;
- restart/recreate checks;
- host client config/token presence and permissions;
- host `egressctl whoami` identity;
- host current default route/mode;
- per-client isolation assertion: changing a test container's default does not change the host default;
- client-local namespace, TUN fd handoff, capability drop, and seccomp policy.

A container-based contributor may request only the fixed harness/report through a narrow dev/test mechanism; the harness does not accept arbitrary shell commands.

# 35. Docker/internal CIDR synchronization

The egress stack MUST NOT receive the Docker socket.

Host helper:

```text
egress-hostctl sync-docker-cidrs
```

collects Docker networks through the host Docker CLI/API and writes only a generated CIDR artifact.

Triggers:

- install;
- Docker restart;
- systemd timer;
- explicit narrow admin action.

# 36. Network exposure

Public client-facing services are available only through trusted local paths:

```text
host clients:      127.0.0.1:<ports>
container clients: docker gateway / explicitly allowed local interface:<ports>
```

Do not bind to a public IP or unrestricted `0.0.0.0` without explicit firewall/interface restrictions.

At minimum, expose:

- authenticated default SOCKS;
- authenticated one-shot SOCKS;
- public narrow `egressd` API.

The Mihomo admin API is accessible only on the private Compose network.

UFW/nftables MUST allow container clients only from the required local subnets/interfaces.

Network reachability does not replace application authentication: control API and proxy endpoints MUST require client credentials.

# 37. Secrets

Recommended backend layout:

```text
/etc/egress/secrets/
  mihomo_api_secret
  providers/
    blanc_subscription
    xeovo_subscription
    custom_vless
  clients/
    host-main.token
    container-main.token
```

Each client environment receives only its own API token and separate default-proxy credentials. One-shot credentials/capabilities are issued through the public API and are not provider secrets.

Client-side examples:

```text
host:
  /etc/egress/client.yaml
  /etc/egress/client.token
  /etc/egress/default-proxy.auth

container:
  /etc/egress/client.yaml
  /run/secrets/egress_client_token
  /run/secrets/egress_egress_default_proxy_auth
```

Use permissions `0700/0600` or equivalent read-only secret mounts.

Provider/Mihomo secrets MUST NOT appear in:

- client configurations;
- Git;
- tar/zip artifacts;
- ordinary logs;
- public API responses;
- `docker inspect` environment variables when mounted files can be used instead.

A client token inside a container is not secret from code in that container; its permissions MUST be safe for the container's entire trust domain.

# 38. Persistence

The persistent root is configurable and MUST be located on a filesystem with sufficient capacity.

Example:

```text
/var/lib/egress/
  mihomo/
  providers/
  generated/
  egressd/
    metrics.sqlite
  geo/
```

Before installation, check `findmnt -T` and `df -h`.

# 39. Metrics DB

SQLite is sufficient.

Tables/entities:

- client profile metadata without raw secrets;
- route metadata;
- health/latency samples;
- domain probe samples;
- benchmark samples;
- provider update events;
- per-client default selection events;
- one-shot jobs/sessions with `client_id`;
- GEO verification cache;
- schema version.

Do not copy raw provider/client secrets into the database when references/hashes are sufficient.

# 40. Observability

`egressd` logs — structured JSON.

Audit events MUST include the authenticated `client_id` for client-initiated operations:

- provider update;
- default route/mode change;
- probe-domain;
- benchmark;
- one-shot start/end;
- GEO update;
- reconciliation fallback to direct.

`egressctl doctor` checks the current client profile for:

1. local config parse/permissions;
2. API/auth;
3. `/v1/self` identity matches the expected `client_id`;
4. API/client version compatibility;
5. Mihomo/backend readiness;
6. current default route/mode;
7. default proxy data plane;
8. one-shot direct;
9. one-shot proxy route;
10. Docker/HARD DIRECT bypass where applicable;
11. provider inventory;
12. GEO data freshness;
13. optional domain probe;
14. local strict/TUN capability;
15. absence of silent fallback.

Doctor MUST NOT print API/proxy credentials.

# 41. GEO data update

Commands:

```bash
egressctl geo status
egressctl geo update
```

Native Mihomo auto-update and explicit API-triggered updates are both allowed.

`geo status` SHOULD show:

- source URLs;
- last update time;
- file/version/hash, when available;
- staleness;
- presence of GeoIP/GeoSite/ASN datasets.

# 42. Admin UI

MetaCubeXD/Zashboard may be used only as an administrative convenience.

It is not part of the agent API.

If installed:

- bind only on host loopback;
- keep the full Mihomo secret inaccessible to containers;
- allow disabling the UI without affecting agent workflows.

# 43. MCP

MCP is optional Phase 3 functionality and does not replace the local client package.

If implemented, it is a thin wrapper over the same public `egressd` API and acts as a separate client profile/credential or an explicitly selected local profile:

```text
egress_status
egress_routes
egress_get_default
egress_set_default
egress_update_provider
egress_test
egress_probe_domain
egress_best
egress_benchmark
```

MCP MUST:

- MUST NOT implement a networking engine;
- MUST NOT receive the Mihomo admin secret/provider secrets;
- MUST NOT provide capabilities beyond the public API/`egressctl`;
- MUST NOT run arbitrary shell commands;
- MUST NOT replace `egress-run` for local workload execution.

If the agent has a local shell and the skill, the preferred path remains `egressctl`/`egress-run`.

# 44. Repository layout

```text
egress/
  TECH_SPEC.md
  README.md
  INSTALL.md
  compose.yaml
  scripts/build-tun-engine.sh

  mihomo/
    config.template.yaml
    generated/
    providers/

  egressd/
    pyproject.toml
    src/egressd/
    tests/

  client/
    pyproject.toml
    src/egress_client/
      egressctl.py
      egress_run.py
      config.py
      api.py
    tests/
    examples/
      client.host.yaml
      client.container.yaml

  adapters/
    blanc/
    xeovo/

  host/
    egress-hostctl
    egress-host-test
    client/tun-engine/
    units/

  skills/
    egress/
      SKILL.md

  scripts/
    install-backend-host.sh
    uninstall-backend-host.sh
    verify-backend-host.sh
    provision-client.sh
    install-client.sh
    verify-client.sh
    bootstrap-dev-access.sh

  tests/
    fixtures/
    integration/
    security/
```

There MUST NOT be separate CLI implementations for hosts and containers; use one client package with different configurations/capabilities.

# 45. Phase 0 — required proof of concept

Before production implementation, verify:

1. A real subscription format from each provider under evaluation.
2. Native Mihomo provider compatibility.
3. A minimal adapter if one is needed.
4. Custom VLESS end-to-end.
5. A WireGuard route through Mihomo.
6. Manual provider update API.
7. Provider health/delay API.
8. At least two per-client selector groups.
9. Independent selector query/set for each client.
10. A `direct` route in every client selector.
11. Authenticated default-proxy identity maps to the correct client selector.
12. One-shot `IN-USER`/equivalent routing to a concrete proxy.
13. **Three concurrent one-shot sessions using different routes**, including `direct`.
14. One-shot routing does not change any client default.
15. Client×route mapping scales to several hundred routes or another concurrency-safe native mechanism is identified.
16. Hard Docker CIDR bypass applies to default and one-shot listeners.
17. GEO data download/update and GEOIP/ASN matching.
18. Domain probe through `direct` and at least two locations.
19. Mihomo admin API remains private.
20. When a provider route disappears, only the affected client falls back to `direct`.
21. The same client package works on a host and container with different configurations.
22. `/v1/self` correctly binds a token to a client identity.
23. Spoofing `client_id` cannot control another profile.
24. Public `egressctl` operations can be reproduced with direct API calls using the same permissions.
25. `egress-run` in a container starts the child inside that container, not on the backend/host.
26. Proxy mode works end-to-end from a container.
27. Proxy mode works end-to-end from a host client.
28. Provider update via an explicit working `--via ROUTE` avoids shared-selector races.
29. Verify local strict/TUN on a host and generic Docker container; record required runtime capabilities and fail-closed behavior.

Record the result in `POC_REPORT.md`, including versions, configuration, measured outcomes, and decisions.

This consolidated checklist retains the original acceptance intent while avoiding provider-specific deployment assumptions.

# 46. Historical coding-agent work plan

## A0 — Architecture/PoC Agent

**Starts first and gates production implementation.**

Tasks:

- complete Phase 0;
- verify the pinned/current Mihomo docs/version;
- check provider/protocol formats;
- demonstrate per-client selector/default isolation;
- demonstrate concurrency-safe, route-addressable one-shot sessions;
- measure client×route mapping scale;
- check GEO assets/domain probes;
- check host/container client configurations;
- demonstrate local-execution semantics for `egress-run`;
- verify client-local nested user/network namespace support on host and ordinary Docker containers.

Skills/competencies:

- Linux/Docker/container runtime networking;
- Mihomo/Clash Meta;
- WireGuard/VLESS/SOCKS;
- HTTP/TLS diagnostics;
- process/network namespace semantics.

A0 works primarily from a container; host-only assertions use an administrator/harness.

Deliverable: `POC_REPORT.md` and proposed corrections to this specification if the PoC disproves assumptions.

---

## A1 — Deployment/Bootstrap Agent

After A0.

Tasks:

- Compose backend stack;
- persistent dirs/secrets/firewall;
- host backend installer;
- client provisioning;
- install the same client package on hosts/containers;
- generate different `client.yaml` files/tokens;
- uninstall/rollback;
- Docker CIDR sync;
- narrow dev/test bridge;
- host acceptance harness.

Required checks:

- no public binds;
- no Docker socket mount;
- separate client tokens;
- backend secrets absent from clients;
- containers cannot issue arbitrary host commands;
- container recreation preserves the production client installation/skill.

---

## A2 — Mihomo/Providers/GEO Agent

After A0, in parallel with A1.

Tasks:

- config template;
- native providers/adapters;
- per-client selector groups;
- default listener + client identities;
- route-addressable one-shot listener;
- `IN-USER`/equivalent rules;
- DIRECT bypass;
- GeoIP/GeoSite/ASN;
- GEO auto-update.

Verify:

- config validation;
- node collisions;
- client selector isolation;
- direct route;
- concurrent one-shot;
- mapping scale;
- GEO update;
- Docker bypass precedence.

---

## A3 — egressd Agent

After A0; coordinate contracts with A1/A2.

Tasks:

- public narrow API;
- token -> client profile auth;
- `/self`/capabilities;
- Mihomo API client;
- stable route IDs/catalog;
- per-client default route/mode;
- provider reconcile;
- one-shot sessions/capabilities;
- update-via-route;
- domain probe/benchmark metadata;
- metrics DB/audit/GEO enrichment.

Required tests:

- client impersonation denied;
- one client cannot change another default;
- concurrent sessions;
- route deletion during job;
- provider update races;
- secret redaction;
- per-client fallback to direct;
- API has no remote exec/raw Mihomo passthrough;
- domain probe error classification.

---

## A4 — Local Client Package Agent

After A3 API stabilization.

Tasks:

- one package with `egressctl` and `egress-run`;
- config discovery/validation;
- host/container endpoint differences only through configuration;
- `/self` identity verification;
- human output + JSON;
- stable exit codes;
- local proxy execution;
- signal/exit-code handling;
- doctor;
- public API parity tests.

Verify on both hosts and from inside containers:

- the same release/package;
- different configurations;
- correct `whoami`;
- independent defaults;
- child processes remain in their expected execution environment;
- `direct`/VPN one-shot sessions;
- concurrent invocations;
- operator control-plane/internal connectivity.

---

## A5 — Probe / Benchmark / Ranking Agent

After A2/A3; may run in parallel with A4 after contracts are frozen.

Tasks:

- domain probe engine;
- bounded benchmark;
- `best` ranking;
- route update-via health workflows;
- GEO explanation metadata.

Required checks:

- direct baseline;
- parallel probes with cap;
- DNS/TCP/TLS/HTTP classification;
- bounded bytes/time;
- `best --domain` uses actual probe.

---

## A6 — Strict/TUN Agent

Implemented in the client-local per-invocation backend.

Tasks/contract:

- nested user/network namespace and a new TUN for every invocation;
- a separate packet engine receives the TUN fd and route-specific SOCKS credentials;
- `direct + tun` and different routes in concurrent invocations;
- workload remains on the host or in the original Docker container;
- clean up the engine, DNS forwarder, and namespace after completion/timeout;
- ordinary Docker profiles allow only a fixed pair of namespace flags;
- fail closed without proxy fallback.

Critical prohibitions:

- no container runtime disable/runc fallback;
- no Docker socket to container;
- no generic remote host exec;
- no shared mutable one-shot selector;
- no silent proxy fallback.

---

## A7 — Agent Skill / Documentation Agent

After A4/A5; the skill is required for v1.

Tasks:

- `skills/egress/SKILL.md`;
- README user + agent sections;
- host/container client install/config documentation;
- usage examples/decision tree;
- optional MCP only after CLI.

Verify with a real container-based agent:

- `whoami`/capabilities;
- current/set own default;
- direct/VPN one-shot;
- concurrent one-shot;
- domain probe across routes;
- provider update + `--via` recovery;
- proxy/TUN choice;
- the agent does not attempt to use the raw Mihomo API/host root.

---

## A8 — Independent Security/Integration Review Agent

This is the final mandatory gate. The reviewer MUST NOT have authored the key components.

Tasks:

- adversarial API review;
- direct API calls bypassing CLI;
- client impersonation/default isolation;
- port exposure/auth;
- secret scan;
- routing bypass verification;
- concurrency races;
- malformed provider input;
- bootstrap/helper privilege review;
- host acceptance report review.

Pay particular attention to:

- the public API does not expose more power than the documented `egressctl` surface;
- bypassing the CLI does not give a container additional privileges;
- containers receive no host shell, Docker socket, provider secrets, or Mihomo secrets;
- host/container defaults remain independent;
- internal Docker/HARD DIRECT traffic remains direct;
- concurrent one-shot sessions do not affect one another;
- `egress-run` executes commands locally;
- the operator control plane remains reachable;
- public network ports remain closed;
- real secrets are absent from Git/artifacts/logs.

# 47. Work sequence

```text
A0 Architecture/PoC
 |
 +--> architecture review / TECH_SPEC corrections
 |
 +--> A1 deployment + client provisioning -------+
 |                                               |
 +--> A2 Mihomo/providers/per-client routing ----+--> A3 egressd/API
                                                       |
                                                       +--> A4 local client package
                                                       |
                                                       +--> A5 probe/benchmark/ranking
                                                              |
                                                              +--> A7 Skill/docs

Proxy-mode acceptance (host + container)
 |
 +--> A6 strict/TUN implementation
 |
 +--> A8 independent security/integration gate
```

A1/A2 may run in parallel after A0.
A4/A5 may partially run in parallel after public API/session contracts are frozen.
The A7 skill is part of the v1 release, not optional documentation.
A8 reviews proxy and client-local TUN boundaries.

# 48. Testing strategy from a container

If the coding/test agent runs inside a container, run real integration tests from that environment.

Minimum set:

1. local client package installed and version reported;
2. `egressctl config validate`;
3. `egressctl whoami` -> `container-main`;
4. direct API `/v1/self` returns same identity/capabilities;
5. spoofing another `client_id` does not change identity;
6. `egressctl route current`;
7. `egressctl route set direct`;
8. `egressctl route set <VPN>`;
9. the host client's default remains unchanged (asserted by the host harness);
10. the default proxy exit IP matches the container default;
11. `egress-run direct -- ...` runs its child inside the Docker container;
12. `egress-run <VPN> -- ...` runs its child inside the Docker container;
13. three or more concurrent one-shot routes, including `direct`;
14. provider update;
15. update blocked provider via working `--via ROUTE` where fixture/environment permits;
16. route list/reconciliation;
17. health test;
18. `probe-domain` direct + multiple routes;
19. bounded benchmark shortlist;
20. Docker internal endpoint remains reachable DIRECT;
21. the operator control-plane service remains reachable during default changes, provider updates, and concurrent one-shot sessions;
22. raw Mihomo API fails from container;
23. direct public API gives no capability beyond egressctl;
24. container cannot operate on host-main default;
25. Generic Docker `--mode tun` acceptance proves child processes stay local and per-invocation namespace routing works through the TUN fd;
26. host acceptance harness report passes.

# 49. Acceptance criteria — v1 proxy mode

Accept a release only if:

1. Mihomo version/image pinned.
2. BlancVPN loads natively or via documented adapter.
3. Xeovo loads natively or via documented adapter.
4. Custom VLESS works end-to-end.
5. At least one WireGuard route works through Mihomo.
6. `direct` exists in route catalog and works as default/one-shot.
7. Hosts and containers use the same client package release.
8. Hosts and containers have separate configurations/tokens/endpoints where needed.
9. `egressctl whoami` returns authenticated profile, not blindly local `client_id`.
10. Defaults are per-client; changing container default does not change host default.
11. Each client can query actual current route/mode.
12. Disappearing selected route falls back to direct only for affected clients.
13. `egressctl` control operations use documented public API only.
14. Direct API calls with same token have same privileges as egressctl.
15. Public API has no raw Mihomo passthrough, arbitrary config patch, host exec or client provisioning.
16. Provider update works and failed update preserves last-known-good.
17. Update via explicit working route works without changing shared/client defaults.
18. Health/delay tests work.
19. GEO data update/status and exit country/ASN enrichment work where available.
20. `probe-domain DOMAIN --all` compares multiple routes including direct and distinguishes DNS/TCP/TLS/HTTP failures.
21. Default proxy uses authenticated client's selected default route.
22. One-shot explicit route does not change any default.
23. At least three simultaneous one-shot jobs through different routes work without interference.
24. One concurrent job may use `direct`.
25. `egress-run` target command executes locally in caller environment, not backend/host remote exec.
26. Exit status/signals are preserved adequately.
27. Docker-internal destination is DIRECT via default and one-shot proxy for container client.
28. Operator-defined HARD DIRECT destination remains direct.
29. the operator control-plane service remains reachable during route changes, provider updates, and concurrent sessions.
30. Raw Mihomo API unavailable from container/ordinary clients.
31. Agent API and proxy endpoints authenticated.
32. Public NIC cannot reach client-facing ports.
33. Docker socket absent from egress stack/container.
34. Provider secrets absent from client configs/Git/API/logs/docker inspect env/artifacts.
35. Benchmark bounded by bytes/time.
36. `best --domain` uses actual domain probe, not GEO heuristics only.
37. Skill installed and real agent completes documented workflows using local CLI.
38. Backend + separate client install instructions reproduce deployment.
39. container client install survives/reproduces container recreate by documented mechanism.
40. Coding agent inside container can run integration suite without host root.
41. Host-only assertions exposed through fixed harness, not generic host shell.
42. API/client version mismatch is detected with actionable error/capability negotiation.

# 50. Acceptance criteria — Phase 2 TUN

Additional criteria:

1. `egress-run --mode tun ROUTE -- command` works for proxy-unaware TCP client on host and generic Docker container.
2. Command remains in the calling environment with caller filesystem/environment and mapped caller identity.
3. UDP/QUIC test works when route/protocol/backend supports it.
4. Concurrent host and generic Docker TUN one-shot jobs through different
   routes work independently; the Docker harness holds both workloads active,
   compares their network namespaces and confirms their public exit IPs.
5. `direct + tun` works as isolated direct path.
6. No generic root escalation/remote exec.
7. stale netns/TUN resources cleaned.
8. HARD DIRECT rules remain enforced where applicable.
9. Client container processes remain in their isolated container for every invocation.
10. The packet engine receives only a TUN fd and bounded selected-route credentials; it never receives argv/env/cwd/stdio or launches a workload.
11. No Docker socket or host root is granted to the client container.
12. No silent fallback from `tun` to `proxy`.
13. `egressctl whoami/capabilities` accurately reports mode availability.

# 51. Upstream references

Before implementation, coding agents MUST check the current upstream documentation:

- Mihomo docs: `https://wiki.metacubex.one/`
- Mihomo API: `https://wiki.metacubex.one/en/api/`
- Proxy providers: `https://wiki.metacubex.one/en/config/proxy-providers/`
- Proxy groups: `https://wiki.metacubex.one/en/config/proxy-groups/`
- Routing rules (`IN-USER`, `IN-NAME`, `GEOIP`, ASN): `https://wiki.metacubex.one/en/config/rules/`
- Inbounds/listeners/TUN: `https://wiki.metacubex.one/en/config/inbound/`
- General authentication/listeners: `https://wiki.metacubex.one/en/config/general/`
- TUN details: `https://wiki.metacubex.one/en/config/inbound/tun/`
- Rule providers: `https://wiki.metacubex.one/en/config/rule-providers/`
- GEO data source: `https://github.com/MetaCubeX/meta-rules-dat`

This specification defines requirements and invariants. Verify exact Mihomo syntax against the pinned version and integration tests.

# 52. Personal profiles and sources

1. On a host, each local user, including a service account without a home directory, receives a `host-<username>` profile on first client use. Container setup defaults to `container-<username>`. Usernames are limited to `[A-Za-z0-9._-]`; if the name is unavailable/unsafe or the ID exceeds 64 characters, use `host-uid-<uid>` / `container-uid-<uid>`. Without `--client`, `setup-egress-client.sh` derives the same ID from `SUDO_UID` (or the current UID when run directly as root). An explicit `--client` selects an additional profile. All UIDs may access the local enrollment socket; the service derives UID from `SO_PEERCRED` and does not accept a UID/client ID from the request. The client receives credentials in memory without writing them to the user's home. `egressctl` and `egress-run` run as the original UID without sudo.
2. Global providers and installation remain administrator-managed. An ordinary profile can add a personal provider from a subscription URL, local file, or single proxy URI using `egressctl source add` (`--url`, `--file`, `--uri`). It can see/use global and owned routes, but cannot see other profiles' routes or sources. Server-side ACL applies to listing, default selection, tests, probes, benchmarks, provider updates, and one-shot sessions. Mihomo's persistent default group contains only global and client-owned providers.
3. For host profiles, the source subscription is stored and refreshed on the backend under the service account. For container profiles, the source URL or file is stored **inside the container** in a private user directory with modes `0700/0600`. The container fetches and normalizes the subscription itself and sends only a YAML snapshot of nodes to the backend. The backend does not read, refresh, or delete the container's source URL/files; `egressctl source refresh` runs inside the container. Container-side normalization uses an optional adapter and PyYAML; the base client remains standard-library-only.
4. Personal-provider API: `GET/POST /v1/personal/providers`, `PUT /v1/personal/providers/{alias}/snapshot`, `POST /v1/personal/providers/{alias}/refresh`, and `DELETE /v1/personal/providers/{alias}`. Hosts use POST/refresh; containers use snapshot. Repeating `source add` with the same alias, type, and URL/URI/file contents returns success with `skipped: true`, without refetch or regeneration. Host `source refresh` recreates the provider from the stored URL or URI. Alias and owner are derived from the authenticated profile. Source secrets are never returned by the API.
5. Personal proxy endpoints and subscription URLs MUST resolve only to public addresses. During import, nodes with a non-public or unresolvable server address are skipped; the API/CLI report the count. Reject the import if no usable nodes remain. For DNS names, pin the validated public IP in the node configuration so Mihomo does not resolve the name again when connecting. Source and normalized snapshot sizes are limited to 4 MiB; there is no separate maximum node count. Limit personal providers to 32 per profile. Changes/deletions regenerate configuration and roll back on reload failure.
6. A personal provider's canonical ID is `p-<client_id>-<alias>` (for example, `p-host-<username>-blanc`). The CLI also accepts `@<alias>:<node>` and resolves the alias against the personal-provider list visible to the current bearer token. The API continues to accept/return canonical route IDs.
