# Handoff: client-local TUN

Updated 2026-10-20. The earlier systemd TUN service design is obsolete and is not an instruction for the current implementation.

## Current design

- `egress-run --mode tun ROUTE -- command` creates a separate user and network namespace for each invocation.
- The workload and child processes remain in the calling environment, including an ordinary Docker container.
- A static packet engine receives the TUN file descriptor and selected one-shot SOCKS credentials; it does not receive workload arguments, working directory, environment, or stdio.
- A local loopback DNS proxy sends DNS through TUN and the same selected route.
- Docker receives `/dev/net/tun` and `client/seccomp-tun.json`. The outer container does not receive `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, privileged mode, host networking, or the Docker socket.
- `scripts/remove-legacy-tun-helper.sh` migrates old host/container TUN services. It refuses to stop the environment if it cannot confirm there are no active jobs.

## Build and install

Build the static engine using Go 1.26.3 on a trusted build machine:

```sh
scripts/build-tun-engine.sh
```

Provisioning includes amd64 and arm64 engines in the client bundle. Installation verifies checksums and selects the binary for the target architecture. See `INSTALL.md` for current instructions.

## Previously verified

- Python and Bash syntax checks, offline Python suite, and install-script harness.
- Linux amd64 and arm64 static engine builds.
- User/network namespace and TUN setup in an ordinary Docker container without `CAP_NET_ADMIN`.
- Direct HTTPS and DNS through TUN in a container; concurrent invocations received different network namespace IDs.
- SOCKS5 authentication/TCP forwarding through a local test endpoint and SOCKS UDP DNS through a separate test endpoint.

Acceptance through real backend provider routes requires a configured backend and valid client bundle. See `host/LIVE_ACCEPTANCE.md` and `INSTALL.md`.
