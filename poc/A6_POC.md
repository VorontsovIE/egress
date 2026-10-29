# A6: Intermediate host-native strict/TUN PoC

Date: 2026-10-16. The PoC ran in a disposable Ubuntu 24.04 container with `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, and `/dev/net/tun`, without a Docker socket or host mounts. It validates network mechanisms but is not live acceptance on a production host.

## Confirmed

- `ip netns add` creates an isolated network namespace with the required capabilities; `ip netns exec` brings up loopback.
- Mihomo v1.19.32 from the pinned image can use a pre-created TUN owned by a separate UID without `CAP_NET_ADMIN` in the Mihomo process.
- Direct TUN networking used a separate namespace, veth, local NAT, and nftables filter. A process running as UID/GID 65534 completed HTTPS and exited successfully.
- Proxy traffic traversed TUN and SOCKS5 to a fake upstream. The upstream observed a CONNECT request and the client received the expected response. An explicit SOCKS outbound `interface-name` was required to prevent a routing loop.

## Not confirmed by this PoC

- Running from a real host systemd service with the caller's filesystem, working directory, environment, UID/GID, and groups.
- The published one-shot listener, SOCKS5 UDP associate, QUIC, DNS, concurrent jobs, or cleanup after abnormal termination.
- Compatibility with the production host firewall/UFW and actual systemd capabilities. A6/live acceptance must not be considered complete until these checks pass.

## Follow-up implementation run

After building `native_netns.py`, an isolated Docker PoC exercised the backend methods `create` → `pin_direct` → `exec_in` → `destroy`; a process with UID/GID 65534 ran in `/tmp` and returned code 0. A proxy run started Mihomo under a worker UID with capabilities dropped; the fake SOCKS5 server received CONNECT to `1.1.1.1:80`, and the HTTP client received the expected proxied response. These runs were in a disposable container, so host systemd, one-shot, DNS/UDP, and product acceptance remained unverified.
