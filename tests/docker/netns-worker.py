#!/usr/bin/env python3
"""Hold a TUN workload open while the Docker harness compares its netns."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    own_marker, peer_marker, release_marker = map(Path, sys.argv[1:])
    own_marker.write_text(os.readlink("/proc/self/ns/net") + "\n", encoding="ascii")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if peer_marker.is_file() and release_marker.is_file():
            return 0
        time.sleep(0.05)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
