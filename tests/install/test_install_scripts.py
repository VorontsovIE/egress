"""Offline install suite — thin pytest wrapper.

Runs tests/install/test_install_scripts.sh (stubbed docker/id/..., no
Docker daemon, no root required) and asserts a clean exit. Scenario list
and environment notes: tests/install/README.md.
"""

import os
import subprocess

HERE = os.path.dirname(os.path.realpath(__file__))
SCRIPT = os.path.join(HERE, "test_install_scripts.sh")


def test_offline_install_suite():
    p = subprocess.run(
        ["bash", SCRIPT],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert p.returncode == 0, (
        "offline install suite failed (rc=%d)\n"
        "--- stdout (tail) ---\n%s\n"
        "--- stderr (tail) ---\n%s"
        % (p.returncode, p.stdout[-8000:], p.stderr[-4000:])
    )
