#!/usr/bin/env python3
"""Hold a route-specific TUN workload until its peer is ready, then probe IP."""
from __future__ import annotations

import ipaddress
import os
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    own_marker, peer_marker, release_marker, result_file = map(Path, sys.argv[1:])
    own_marker.write_text(os.readlink("/proc/self/ns/net") + "\n", encoding="ascii")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if peer_marker.is_file() and release_marker.is_file():
            break
        time.sleep(0.05)
    else:
        return 1

    opener = build_opener(ProxyHandler({}))
    with opener.open("https://api.ipify.org", timeout=30) as response:
        value = response.read(128).decode("ascii").strip()
    address = ipaddress.ip_address(value)
    if not address.is_global:
        raise RuntimeError("route returned a non-public exit address")
    result_file.write_text(value + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
