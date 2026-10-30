# Development

Run development commands from the repository root. The repository can be checked out at any path; scripts resolve their own locations relative to the checkout.

## Offline tests

The backend runtime dependencies are declared in `egressd/pyproject.toml`; the `dev` optional dependency set adds `pytest`, `pytest-asyncio`, and `httpx2` for Starlette's test client. Install them into the project environment once with [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked --project egressd --extra dev
```

This installs the same dependency set required by the backend and tests; `egressd/uv.lock` pins the resolved versions. No separate, duplicated requirements file is needed. The test app writes temporary state, so point its state paths to a writable temp directory, then run the suites from the repository root:

```sh
export EGRESSD_STATE_DIR="$(mktemp -d)"
export EGRESSD_METRICS_DB="$EGRESSD_STATE_DIR/metrics.sqlite"
export EGRESSD_GEO_DIR="$EGRESSD_STATE_DIR/geo"
uv run --locked --project egressd python -m pytest tests/ client/tests/ -q
uv run --locked --project egressd python -m pytest tests/security/ -q
uv run --locked --project egressd python -m pytest tests/install/ -q
```

These tests do not require a live Mihomo backend. Python client runtime dependencies remain standard-library-only; the backend dependencies are needed to collect and run backend integration tests.

## Live checks

The PoC orchestrators require the corresponding locally built binary and GEO data:

```sh
python3 poc/run_poc.py
python3 poc/run_a2.py
python3 poc/run_a3.py
```

Fetch PoC vendor sources with `sh scripts/fetch-poc-vendor.sh`; fetch GEO data with `sh scripts/fetch-geo-data.sh`. The backend deployment uses the pinned Mihomo container image and does not require PoC binaries.

Live backend operations require a Linux host with Docker Engine/Compose and root access:

```sh
sudo bash scripts/install-backend-host.sh
sudo bash scripts/verify-backend-host.sh
sudo bash scripts/run-live-acceptance.sh
sudo bash scripts/uninstall-backend-host.sh
```

## Build PoC binaries

Vendor sources and generated binaries are not tracked. Fetch the pinned sources, then build:

```sh
sh scripts/fetch-poc-vendor.sh
cd poc/mihomo-src && CGO_ENABLED=0 go mod download
cd poc/mihomo-src && CGO_ENABLED=0 go build -o ../mihomo .
cd poc/mihomo-src && CGO_ENABLED=0 go build -tags with_gvisor -o ../mihomo-gvisor .
cd poc/wgserver && go mod download && go build -o wgserver .
```

## GitHub releases

GitHub Actions runs the offline, security, and install suites on pull requests and pushes to `main`. Push a semantic version tag such as `v1.2.3` to publish a multi-architecture `ghcr.io/vorontsovie/egress` image and a source archive containing the client TUN engines for Linux amd64 and arm64. The archive includes `SHA256SUMS`; verify it with `sha256sum -c SHA256SUMS`.

The backend installer selects the exact GHCR version when run from a checkout at a release tag. Other checkouts use `latest`; set `EGRESSD_IMAGE` to pin another image explicitly. Client credential bundles remain provisioned separately on the backend and are never part of a release artifact.

Build the client TUN packet engine separately with `scripts/build-tun-engine.sh`; it requires Go 1.26.3 or later.

## Repository map

- `egressd/`: authenticated control API and backend state.
- `client/`: standard-library Python package with `egressctl` and `egress-run`.
- `adapters/`: provider parsing and normalization.
- `host/`, `scripts/`: host-side provisioning, maintenance, and acceptance tools.
- `tests/`: offline tests and security checks.
- `poc/`: PoC sources, generated binaries, and run artifacts.
- `reports/`: acceptance reports.

Persistent host data defaults to `/var/lib/vibelab-egress`; protected secrets default to `/etc/vibelab-egress/secrets`. Override supported paths with the documented script options or environment variables.
