#!/usr/bin/env python3
"""Stable default client IDs derived from the invoking Linux account."""
from __future__ import annotations

import argparse
import pwd
import re

SAFE_USERNAME = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_CLIENT_ID = 64


def default_client_id(uid: int, environment: str) -> str:
    if environment not in ("host", "container"):
        raise ValueError("environment must be host or container")
    prefix = environment + "-"
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = ""
    if (username and SAFE_USERNAME.fullmatch(username) and
            len(prefix + username) <= MAX_CLIENT_ID):
        return prefix + username
    return f"{environment}-uid-{uid}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("uid", type=int)
    parser.add_argument("environment", choices=("host", "container"))
    args = parser.parse_args()
    print(default_client_id(args.uid, args.environment))


if __name__ == "__main__":
    main()
