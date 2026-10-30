# Installation and configuration

The backend and clients are installed separately. Deploy one backend on a Linux Docker host, then provision a client profile for each Linux host or container that will run workloads. Backend installation requires root. Client setup scripts also require root because they install files and provision credentials.

## Backend

### Requirements

- Linux host with Docker Engine and Docker Compose v2.
- Python 3 for host-side utilities.
- Internet access to pull the pinned Mihomo image and (unless seeded locally) GEO data.
- Root access.
- The default installer provisions a TUN-enabled host client, so build the packet engine first with Go 1.26.3 or later. For a proxy-only deployment, install the backend with `--skip-client`, then provision each client with `--tun-backend none` (no Go build required).

From a checkout of this repository, build the engine and deploy the backend:

```sh
scripts/build-tun-engine.sh
sudo bash scripts/install-backend-host.sh
sudo bash scripts/verify-backend-host.sh
```

For a tagged GitHub release, the installer pulls the corresponding `egressd` image from `ghcr.io/vorontsovie/egress`. A checkout not at a release tag uses `latest`; set `EGRESSD_IMAGE` to pin a different published version. To install client code from a release archive, extract it and use it with a separately provisioned client bundle. Release archives never contain client credentials.

The installer creates backend state under `/var/lib/vibelab-egress` and protected secrets under `/etc/vibelab-egress/secrets`. Override the persistent root with `--root` and the secrets directory with `--secrets-dir`. Use the supported scripts' `--help` options for syntax. The installer deploys Mihomo and `egressd`, provisions and installs a host client by default, configures Docker network CIDRs and firewall rules where supported, and runs the host acceptance harness. Inspect `<root>/install-backend-host.log` if installation reports an error.

For proxy-only operation, provision the host profile without TUN before the backend install. The installer still runs its acceptance harness, so the expected `host-main` client must already be installed when using `--skip-client`:

```sh
sudo bash scripts/provision-client.sh --client host-main --environment host --tun-backend none
sudo bash scripts/install-client.sh --client host-main --target host \
  --bundle /var/lib/vibelab-egress/client-bundles/host-main
sudo bash scripts/install-backend-host.sh --skip-client
```

Mihomo's admin API is private to the Compose network. Published client/API and proxy ports are restricted to local and Docker networks. Review firewall requirements in `TECH_SPEC.md` before changing network exposure.

### Add provider subscriptions

Provider sources are managed on the backend host with `host/egress-providerctl`. Source URLs and credentials are stored in the protected secrets directory; do not commit them or paste them into public logs. Adding a URL prompts for it without echoing the input. A source file must be a regular file with mode `0600`.

The subscription URL is entered at a hidden prompt rather than on the command line. Before adding a source file, restrict its permissions to mode `0600`:

```sh
sudo host/egress-providerctl add --id myprovider --subscription-url
chmod 600 ./links.txt
sudo host/egress-providerctl add --id custom --source-file ./links.txt
sudo host/egress-providerctl list
sudo host/egress-providerctl update --id PROVIDER_ID
chmod 600 ./new-links.txt
sudo host/egress-providerctl update --id custom --source-file ./new-links.txt
sudo host/egress-providerctl remove --id custom
```

Supported formats include Clash/Mihomo YAML, base64-encoded Clash subscriptions, VLESS/VMess/Trojan/Shadowsocks/Hysteria2/TUIC URIs, and WireGuard configuration. Normalized provider data is written into the backend persistent root. Failed fetches or parsing retain the previous known-good provider data.

### Backend maintenance

```sh
sudo bash scripts/verify-backend-host.sh
sudo bash scripts/uninstall-backend-host.sh --help
```

Uninstall retains persistent data by default; `--remove-data` removes it. Review the script help and confirm the selected paths before removing data.

## Install a client

The core client package requires Python 3.11 or later and has no third-party Python dependencies. Client profiles are provisioned by the backend administrator; a bundle contains `client.yaml`, `client.token`, `default-proxy.auth`, and TUN engine binaries when TUN is enabled. Keep bundle and secret files private.

### Host client

The backend installer sets up the default host client. To provision or install another profile:

```sh
sudo scripts/setup-egress-client.sh --environment host --client CLIENT_ID
egressctl config validate
egressctl whoami
egressctl doctor
```

When no explicit ID is given, setup derives a stable ID from the invoking account. An explicitly selected profile can be used with `egressctl --config /etc/vibelab-egress/client.yaml ...` or `egress-run --config /etc/vibelab-egress/client.yaml ...`.

### Container client

Provision a client bundle on the backend host:

```sh
sudo scripts/provision-client.sh --client CLIENT_ID --environment container
```

Build the client package into the image, preserving the repository layout expected by `scripts/install-client.sh` (`client/src/` and `adapters/`). Mount the bundle read-only at runtime and run the installer as root during container initialization, before starting the workload:

```sh
bash /path/to/checkout/scripts/install-client.sh \
  --client CLIENT_ID --target container --bundle /run/egress-client-bundle
```

Do not bake the bundle into an image layer because it contains client credentials. Provide a read-only bundle mount using the container runtime's secret or bind-mount mechanism. Running the installer at startup copies credentials into the container's `/etc` and `/run/secrets`; those copies disappear when the writable container layer is recreated, so keep the bundle mount available at each start.

The client container must reach the backend's published API and proxy ports. On Linux Docker, `host.docker.internal` is not added automatically; add `--add-host=host.docker.internal:host-gateway` (or equivalent Compose `extra_hosts`) or configure `control_url` and `proxy_host` with a reachable address. Never copy backend provider secrets into a client bundle.

If container users will run `egressctl source add` or `source refresh`, install PyYAML in the image (for example, `python3 -m pip install 'PyYAML>=6.0'`). The container normalizes its private source locally before uploading the resulting snapshot. This optional feature is the only client operation that needs PyYAML.

For a disposable Docker test container, the backend host can run:

```sh
scripts/build-tun-engine.sh   # requires Go 1.26.3+ if the engine is not already built
sudo scripts/setup-egress-client.sh --environment container --docker-test \
  --route PROVIDER:ROUTE
```

`--docker-test` is a test harness, not a general installer. For a host-owned personal route, `--copy-personal-source` creates a separate normalized snapshot for the test identity; it does not grant that identity access to the owner's profile.

## TUN mode

TUN is opt-in at runtime and runs locally for each `egress-run` invocation. Build the pinned packet engine before provisioning TUN-enabled bundles:

```sh
scripts/build-tun-engine.sh
```

The target Linux environment needs `/dev/net/tun`, unprivileged user namespaces, `unshare` (util-linux), `ip` (iproute2), and the matching engine binary. For a Docker container, expose `/dev/net/tun` and use the repository's `client/seccomp-tun.json` profile as described by the container runtime. Check local support with `egressctl capabilities`. TUN setup fails closed; it does not silently fall back to proxy mode.

## Verify and troubleshoot

```sh
egressctl config validate
egressctl whoami
egressctl status
egressctl doctor
egressctl list
```

Confirm that the client identity matches the provisioned token, that its configured API URL is reachable, and that routes exist on the backend. Use `egressctl --help`, `egress-run --help`, and the supported script options (`scripts/install-backend-host.sh --help`, `scripts/setup-egress-client.sh --help`, `scripts/provision-client.sh --help`, `scripts/install-client.sh --help`, and `host/egress-providerctl --help`) for current syntax. The build script reports its Go requirement when run. See [USER_GUIDE.md](USER_GUIDE.md) for normal use and [CLI_REFERENCE.md](CLI_REFERENCE.md) for command summaries.
