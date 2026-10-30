# egress

**egress** routes locally running commands through selected proxy routes. A Docker Compose backend (`mihomo` + `egressd`) serves multiple Linux hosts and containers. Install the small Python client (`egressctl` and `egress-run`), whose core uses only the standard library, wherever your workload runs.

## Quick start

1. Install from the latest signed release with the [one-command installer](INSTALL.md#backend), or build the TUN packet engine from a checkout with Go 1.26.3+ and deploy the backend on a Linux machine with Docker Engine and Compose v2. See [INSTALL.md](INSTALL.md) for exact commands and proxy-only alternatives.
2. Provision and install a client profile for each host or container.
3. Check the connection and available routes:

   ```sh
   egressctl config validate
   egressctl whoami
   egressctl list
   ```

4. Run a command through a route:

   ```sh
   egress-run provider:route -- curl https://example.com/
   ```

   Replace `provider:route` with a route ID shown by `egressctl list`.

`egress-run` executes the command locally. It sends route/session requests to the backend, never the command itself. Use `egress-run --help` and `egressctl --help` for command-line help.

## Documentation

- [Install and configure](INSTALL.md)
- [Everyday usage](USER_GUIDE.md)
- [CLI reference](CLI_REFERENCE.md)
- [Technical specification and implementation status](TECH_SPEC.md)
- [Development](DEVELOPMENT.md)
- [Agent usage skill](skills/egress/SKILL.md)

## Requirements

The backend installer requires Linux, root access, Docker Engine with Compose v2, and Python 3. The core Python client requires Python 3.11+ and uses only the standard library. Container-side personal-source normalization additionally requires PyYAML; other client commands do not. TUN mode has additional Linux namespace, `/dev/net/tun`, `unshare`, `ip`, and packet-engine requirements; proxy mode is the simplest way to start.
