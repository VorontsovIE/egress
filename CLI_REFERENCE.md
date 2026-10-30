# CLI reference

Use `egressctl --help` or `egressctl COMMAND --help` for live syntax. Global options are `--config PATH` and `--json` (where supported); put them before the command. Personal route aliases use `@alias:node`.

## Commands

| Command | Purpose |
|---|---|
| `status`, `whoami`, `doctor`, `capabilities` | Inspect backend, identity, local capabilities, and health |
| `route current|set ROUTE` | Read or set this client's default route |
| `mode current|set proxy\|tun` | Read or set this client's default execution mode |
| `providers`, `list` | Inspect providers and route catalog; `list` accepts country/provider/healthy filters |
| `test [ROUTE]` | Test one route, or catalog routes up to the backend limit when omitted |
| `probe-domain DOMAIN` | Probe a domain over selected routes; see options below |
| `benchmark [ROUTE]` | Measure a route or benchmark a latency shortlist |
| `best` | Rank routes, optionally filtering by country, provider, domain, or latency |
| `update` | Trigger provider updates; supports `--provider`, `--via`, `--if-older-than` |
| `upgrade` | Update client/backend from a release; supports version and component selection |
| `source list|add|refresh|remove` | Manage client-owned private sources |
| `oneshot active` | List active one-shot sessions |
| `geo status|update` | Inspect or update GEO datasets |
| `config show|validate` | Display masked configuration or validate config and secret files |

## `egress-run`

```text
egress-run [--config PATH] [--timeout SECONDS] [--mode proxy|tun] [--default | ROUTE] -- COMMAND [ARG ...]
egress-run direct -- COMMAND [ARG ...]
```

Choose one explicit route or `--default`. `--default` uses the saved per-client route and mode; `--mode` overrides the saved mode. An explicit route defaults to proxy mode. `direct` bypasses the proxy. The command executes locally; its exit code is returned. An explicit route uses an isolated one-shot session and does not modify the persistent default. TUN mode runs locally and fails closed when prerequisites are unavailable. `--timeout` applies to TUN bootstrap operations.

## Configuration and exit codes

### Options

```text
egressctl list [--country CODE] [--provider ID] [--healthy]
egressctl test [ROUTE]
egressctl update [--provider ID] [--via ROUTE] [--if-older-than DURATION]
egressctl benchmark [ROUTE] [--country CODE] [--provider ID] [--top-latency N] [--bytes SIZE]
egressctl best [--country CODE] [--provider ID] [--domain DOMAIN]
                 [--max-latency MS] [--speed-aware] [--limit N]
egressctl config show [--secrets]
egressctl probe-domain DOMAIN [--all] [--country CODE] [--provider ID]
    [--routes ID[,ID...]] [--parallel N] [--timeout SECONDS]
    [--scheme http|https] [--port PORT] [--path PATH] [--method HEAD|GET]
    [--expected-status CODE[,CODE...]] [--check dns,tcp,tls,http]
    [--max-routes N]
```

`benchmark` without a route first selects up to `--top-latency` routes (default 5) and benchmarks that shortlist. `--bytes` defaults to `8M`. `best --speed-aware` includes recent throughput in ranking; `--limit` changes how many candidates are printed (default 10). Probe defaults and backend limits are configurable server-side; `--all` requests catalog routes up to the per-request backend limit. Provider updates without `--provider` request an update of all providers. Durations accepted by `--if-older-than` use values such as `6h`.

`egressctl upgrade [--version vX.Y.Z] [--client-only | --backend-only]` selects the release and components. `--client-only` works without administrator rights; full/backend upgrades require explicit `sudo`. Without the required rights, the command prints the supported alternatives and exits without changing files. Privileged updates require a root-owned updater and reject user-writable Python installations.

`source add NAME` requires exactly one of `--url URL`, `--file PATH`, or `--uri URI`; for URL/URI, `-` reads one line from standard input. `source refresh NAME` and `source list` manage client-owned sources. Remove a provider profile with `provider remove --personal ALIAS`; managed profiles require `sudo egressctl provider remove --managed ID`. Container-side `source add` and `source refresh` require PyYAML for local normalization; the core CLI remains standard-library-only.

`--config` overrides `EGRESS_CONFIG`; otherwise the client uses `/etc/egress/client.yaml`. Secret values are read from separate files referenced by the config. `egressctl config show` masks secrets by default; `--secrets` displays them and should be used with care.

`egressctl` exit codes: 0 success, 1 runtime/API error, 2 usage error, 3 configuration error, 4 authentication error, 5 identity mismatch. `egress-run` also uses 6 for unsupported local capability. For the precise syntax and accepted option values, prefer each command's `--help` output.
