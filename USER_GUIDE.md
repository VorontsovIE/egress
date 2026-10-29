# User guide

This guide covers routine use after an administrator has deployed the backend and provisioned a client profile. Run commands in the same host or container where the workload runs.

## Check setup

```sh
egressctl config validate
egressctl whoami
egressctl status
egressctl doctor
egressctl providers
egressctl list
```

`egressctl list --healthy` filters to healthy routes. Add `--country FI` or `--provider NAME` to narrow the catalog. Use `egressctl best` to rank candidates, `egressctl test provider:route` to test one route, or `egressctl test` to test catalog routes up to the backend limit. `egressctl benchmark provider:route` measures one route; `egressctl benchmark` benchmarks a latency shortlist.

## Run commands

```sh
# Use an explicit route for this invocation
egress-run provider:route -- curl https://example.com/

# Use the configured default route
egress-run --default -- command arg1

# Run without a proxy
egress-run direct -- command arg1

# Select TUN mode for this invocation
egress-run --mode tun provider:route -- command arg1
```

Replace `provider:route` with an ID shown by `egressctl list`. An explicit route does not change the saved default. `egress-run` preserves the command's exit status and does not reparse its arguments through a shell. See `egress-run --help` for options including `--config` and `--timeout`.

## Change defaults

```sh
egressctl route current
egressctl route set provider:route
egressctl mode current
egressctl mode set proxy
```

Modes are `proxy` and `tun`. `egress-run --default` uses the saved mode; an explicit `--mode` overrides it. An explicit route uses proxy mode unless you pass `--mode tun`. TUN availability depends on the local machine; inspect it with `egressctl capabilities`.

## Personal sources

Add a private subscription URL, file, or single proxy URI:

```sh
egressctl source add work --url https://subscription.example/sub
egressctl source add lab --file ./proxies.txt
egressctl source add node --uri 'vless://…'
egressctl source list
egressctl source refresh work
egressctl source remove lab
```

The source is private to the client identity. On a host, the backend receives and stores it in its protected source area. In a container, the source file stays in that container and the client uploads normalized nodes. Container-side `source add` and `source refresh` need PyYAML installed in the image; other client commands do not. Do not put credentials in shell history; use stdin (`--url -` or `--uri -`) when appropriate.

For a personal source, route aliases may use `@alias:node`; canonical route IDs are also accepted. `egressctl --help` lists additional tools for domain probes, GEO status, and active one-shot sessions.
