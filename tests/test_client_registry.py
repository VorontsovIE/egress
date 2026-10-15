"""ClientRegistry reload semantics (offline, no server, no mihomo).

Regression coverage for the in-place token rotation bug:
scripts/provision-client.sh rotates a client token by overwriting the existing
<id>.token file in place, which bumps the FILE mtime but not the DIR mtime.
The old dir-mtime-only watch kept the running egressd authenticating the OLD
token forever (rotated tokens answered "unknown token" until a container
restart). The registry now signs the dir with per-file (mtime, size) stats,
so rotation of an existing client is detected like adding/removing clients.

Run:  python -m pytest tests/test_client_registry.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "egressd" / "src"))


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """(ClientRegistry class, clients dir).

    egressd.main runs ``create_app()`` at import time; point its env at tmp
    paths first (same pattern as tests/integration/test_a3.py) so the import
    is safe without a /var/lib/egressd mount or root.
    """
    clients = tmp_path / "clients"
    clients.mkdir()
    monkeypatch.setenv("EGRESSD_CLIENTS_DIR", str(clients))
    monkeypatch.setenv("EGRESSD_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("EGRESSD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EGRESSD_METRICS_DB", str(tmp_path / "state" / "metrics.sqlite"))
    monkeypatch.setenv("EGRESSD_GEO_DIR", str(tmp_path / "geo"))
    from egressd.main import ClientRegistry

    return ClientRegistry, clients


def _write_client(clients_dir: Path, client_id: str, token: str) -> None:
    (clients_dir / f"{client_id}.token").write_text(token + "\n")
    (clients_dir / f"{client_id}.profile.json").write_text(
        "{\"client_id\": \"" + client_id + "\", \"environment\": \"host\", \"modes\": [\"proxy\"]}\n"
    )
    (clients_dir / f"{client_id}.proxy-creds").write_text("proxy-user:proxy-pass\n")


def test_unknown_token_resolves_to_none(registry) -> None:
    ClientRegistry, clients = registry
    reg = ClientRegistry(clients)
    assert reg.resolve("no-such-token") is None


def test_new_client_files_are_picked_up(registry) -> None:
    ClientRegistry, clients = registry
    reg = ClientRegistry(clients)
    assert reg.resolve("tok-new") is None
    _write_client(clients, "client-a", "tok-new")
    profile = reg.resolve("tok-new")
    assert profile is not None
    assert profile["client_id"] == "client-a"
    assert profile["environment"] == "host"
    assert reg.proxy_creds("client-a") == ("proxy-user", "proxy-pass")


def test_in_place_token_rotation_is_seen_without_restart(registry) -> None:
    """Regression: provision-client.sh rewrites <id>.token in place, so the
    dir mtime does not change. The rotated token must authenticate and the
    old one must stop working, without restarting egressd."""
    ClientRegistry, clients = registry
    _write_client(clients, "client-a", "tok-old")
    reg = ClientRegistry(clients)
    assert reg.resolve("tok-old") is not None

    tok_path = clients / "client-a.token"
    dir_mtime_before = clients.stat().st_mtime_ns
    tok_path.write_text("tok-new\n")  # in-place rewrite (O_TRUNC)
    # Deterministic mtime bump; sizes are equal so mtime is the only signal.
    st = tok_path.stat()
    os.utime(tok_path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**6))
    assert clients.stat().st_mtime_ns == dir_mtime_before  # sanity: trap intact

    assert reg.resolve("tok-old") is None
    profile = reg.resolve("tok-new")
    assert profile is not None
    assert profile["client_id"] == "client-a"
    assert reg.proxy_creds("client-a") == ("proxy-user", "proxy-pass")


def test_removed_client_stops_authenticating(registry) -> None:
    ClientRegistry, clients = registry
    _write_client(clients, "client-a", "tok-a")
    _write_client(clients, "client-b", "tok-b")
    reg = ClientRegistry(clients)
    assert reg.resolve("tok-b") is not None
    (clients / "client-b.token").unlink()
    (clients / "client-b.profile.json").unlink()
    (clients / "client-b.proxy-creds").unlink()
    assert reg.resolve("tok-b") is None
    assert reg.resolve("tok-a") is not None
