# Live acceptance — host and generic Docker client

`host/live-acceptance.py` produces a JSON report for checks requiring the live
backend, Docker, or network access. `scripts/verify-live-report.py` independently
re-derives checks from report evidence and verifies its hash.

The TUN backend is client-local. Every `egress-run --mode tun` starts a fresh
user and network namespace; the workload and all its child processes
stay in the calling host or container. The packet engine receives only the TUN
file descriptor and the selected one-shot SOCKS route. The generic Docker test
uses a plain test image, `/dev/net/tun`, and `client/seccomp-tun.json`; it does
not use host networking, `--privileged`, or `CAP_NET_ADMIN`.

## Prerequisites

1. Docker and the egress backend are running.
2. Build the static TUN engine with `scripts/build-tun-engine.sh` before
   provisioning the client bundle.
3. Host client is installed and `egressctl status` succeeds.
4. The generic container test requires Docker and a route visible to the test
   profile. A host profile's personal route must first be copied into a
   snapshot owned by the test client. Replace `<host-user>` with the account
   that owns the host profile:

```bash
sudo scripts/setup-egress-client.sh --client egress-test-client \
  --environment container --docker-test \
  --route 'p-host-<host-user>-blanc:vless-49' --copy-personal-source
```

The harness builds and runs an ordinary Docker image, installs the bundled
client in the container, checks `whoami` and `doctor`, then runs proxy and TUN
workloads inside the same container. It checks the child process sees the
container marker and makes a real HTTPS request.

## Run host acceptance

```bash
bash scripts/run-live-acceptance.sh
sudo python3 scripts/verify-live-report.py reports/host_acceptance_report.json
```

TUN checks that cannot run because the host disallows unprivileged user
namespaces or lacks `/dev/net/tun` are reported as `skip` with their reason.
The generic container TUN end-to-end check is run separately by the Docker
harness above.

The report contains redacted command output, statuses, and a tamper hash. Do not
copy secrets or generated configs into the repository.
