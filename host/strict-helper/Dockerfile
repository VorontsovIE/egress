# Dockerfile for the local privileged strict/TUN helper (A6, spec section 21).
#
# The helper is a single stdlib-only Python script; it needs `ip` (iproute2)
# to manage per-run network namespaces and, optionally, `tun2socks` to forward
# the TUN device to the route-specific one-shot SOCKS upstream. It runs with
# the minimal reviewed capability grant from compose.tun.yaml (cap_add:
# NET_ADMIN only; cap_drop: ALL; no-new-privileges).
FROM python:3.11-slim

# iproute2 provides `ip netns`; tun2socks is the userspace TUN->SOCKS forwarder.
# (If a bundled forwarder is preferred, replace this with a static binary.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/egress-tun-helper
COPY host/strict-helper/egress-tun-helper.py /opt/egress-tun-helper/egress-tun-helper.py
RUN chmod 0755 /opt/egress-tun-helper/egress-tun-helper.py

# The helper creates a 0700 state dir + 0600 socket at runtime.
RUN mkdir -p /run/vibelab-egress && chmod 0700 /run/vibelab-egress

ENTRYPOINT ["python", "/opt/egress-tun-helper/egress-tun-helper.py"]
