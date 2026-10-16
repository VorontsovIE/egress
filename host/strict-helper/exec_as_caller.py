#!/usr/bin/env python3
"""Fixed launcher used only after the privileged helper dropped UID/GID.

The privileged process must enter the netns before setpriv. This process runs
as the caller already, so checking/changing cwd cannot grant access to a
directory the caller could not ordinarily enter.
"""
import os
import sys


def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(2)
    cwd, mask, *command = sys.argv[1:]
    os.chdir(cwd)
    os.umask(int(mask))
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
