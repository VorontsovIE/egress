"""Parser contract tests for the egress lifecycle commands."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client" / "src"))

from egress_client.egressctl import build_parser  # noqa: E402


def test_provider_remove_requires_one_explicit_scope():
    parser = build_parser()
    personal = parser.parse_args(
        ["provider", "remove", "--personal", "work"])
    assert personal.command == "provider"
    assert personal.provider_cmd == "remove"
    assert personal.personal == "work"
    assert personal.managed is None

    managed = parser.parse_args(
        ["provider", "remove", "--managed", "alpha"])
    assert managed.managed == "alpha"
    assert managed.personal is None

    with pytest.raises(SystemExit):
        parser.parse_args(["provider", "remove"])
    with pytest.raises(SystemExit):
        parser.parse_args(["provider", "remove", "--personal", "work",
                           "--managed", "alpha"])


def test_upgrade_options_select_version_and_component():
    parser = build_parser()
    args = parser.parse_args(
        ["upgrade", "--client-only", "--version", "v1.2.3"])
    assert args.client_only is True
    assert args.backend_only is False
    assert args.version == "v1.2.3"

    args = parser.parse_args(["upgrade", "--backend-only"])
    assert args.client_only is False
    assert args.backend_only is True
    assert args.version is None

    with pytest.raises(SystemExit):
        parser.parse_args(["upgrade", "--client-only", "--backend-only"])


def test_uninstall_options_distinguish_client_only_and_purge():
    parser = build_parser()
    client = parser.parse_args(["uninstall", "--client-only"])
    assert client.client_only is True
    assert client.purge is False

    purge = parser.parse_args(["uninstall", "--purge"])
    assert purge.client_only is False
    assert purge.purge is True

    with pytest.raises(SystemExit):
        parser.parse_args(["uninstall", "--client-only", "--purge"])


def test_old_provider_removal_command_is_not_an_alias():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["source", "remove", "work"])
