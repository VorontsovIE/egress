"""A user-writable client must never invoke the privileged provider helper."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from egress_client import egressctl  # noqa: E402


def test_managed_remove_refuses_user_writable_cli_before_helper(monkeypatch):
    monkeypatch.setattr(egressctl, "_root_owned_cli", lambda: False)
    monkeypatch.setattr(egressctl.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(
        egressctl.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("privileged helper must not run"),
    )
    args = egressctl.argparse.Namespace(personal=None, managed="managed-provider")

    with pytest.raises(SystemExit, match="user-writable egressctl install"):
        egressctl.cmd_provider_remove(args)
