"""Offline coverage for profile/provider ID migration and rollback."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migrate-egress-identities.py"
LOADER = importlib.machinery.SourceFileLoader("egress_identity_migration", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
MIGRATION = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(MIGRATION)


def _layout(tmp_path: Path):
    root = tmp_path / "root"
    secrets = tmp_path / "secrets"
    etc = tmp_path / "etc-client"
    old_client = "host-uid-998877"
    alias = "blanc"
    old_provider = f"p-{hashlib.sha256(old_client.encode()).hexdigest()[:24]}-{alias}"
    clients = secrets / "clients"
    bundle = root / "client-bundles" / old_client
    providers = root / "providers"
    personal = root / "egressd" / "personal"
    status = root / "egressd" / "provider-status"
    state = root / "egressd" / "state"
    source_secrets = secrets / "providers"
    for directory in (clients, bundle, providers, personal, status, state,
                      source_secrets, etc):
        directory.mkdir(parents=True, exist_ok=True)
    (clients / f"{old_client}.token").write_text("fake-token\n")
    (clients / f"{old_client}.profile.json").write_text(json.dumps({
        "client_id": old_client, "environment": "host", "modes": ["proxy", "tun"]
    }))
    (clients / f"{old_client}.proxy-creds").write_text(f"{old_client}:fake-pass\n")
    (bundle / "client.yaml").write_text(f"client_id: {old_client}\n")
    (bundle / "client.token").write_text("fake-token\n")
    (bundle / "default-proxy.auth").write_text(f"{old_client}:fake-pass\n")
    (providers / f"{old_provider}.yaml").write_text("proxies: []\n")
    (personal / f"{old_provider}.json").write_text(json.dumps({
        "provider_id": old_provider, "client_id": old_client, "alias": alias,
        "kind": "url", "source": "https://subscription.example/list"
    }))
    (status / f"{old_provider}.json").write_text('{"last_success": 1}\n')
    (source_secrets / f"{old_provider}.json").write_text('{"type":"url"}\n')
    (source_secrets / f"{old_provider}.source").write_text("opaque source\n")
    (state / "defaults.json").write_text(json.dumps({"clients": {
        old_client: {"route": f"{old_provider}:FI-node", "mode": "tun"}
    }}))
    (etc / "client.yaml").write_text(f"client_id: {old_client}\n")
    (etc / "client.token").write_text("fake-token\n")
    (etc / "default-proxy.auth").write_text(f"{old_client}:fake-pass\n")
    db = sqlite3.connect(state / "metrics.sqlite")
    db.executescript("""
      CREATE TABLE clients(client_id TEXT PRIMARY KEY, environment TEXT);
      CREATE TABLE routes(route_id TEXT PRIMARY KEY, provider TEXT, display_name TEXT);
      CREATE TABLE route_identities(route_id TEXT PRIMARY KEY, fingerprint TEXT);
      CREATE TABLE health_samples(route_id TEXT, client_id TEXT);
      CREATE TABLE oneshot_sessions(id TEXT PRIMARY KEY, client_id TEXT, route TEXT,
                                    ended_at REAL);
      CREATE TABLE default_selection_events(client_id TEXT, from_route TEXT, to_route TEXT);
    """)
    route = f"{old_provider}:FI-node"
    db.execute("INSERT INTO clients VALUES(?, 'host')", (old_client,))
    db.execute("INSERT INTO routes VALUES(?, ?, 'FI-node')", (route, old_provider))
    db.execute("INSERT INTO route_identities VALUES(?, 'fingerprint')", (route,))
    db.execute("INSERT INTO health_samples VALUES(?, ?)", (route, old_client))
    db.execute("INSERT INTO oneshot_sessions VALUES('s1', ?, ?, 1)", (old_client, route))
    db.execute("INSERT INTO default_selection_events VALUES(?, ?, ?)",
                (old_client, route, route))
    db.commit()
    db.close()
    return root, secrets, etc, old_client, old_provider, route


def _locations(root: Path, secrets: Path, etc: Path):
    return {
        "providers": root / "providers",
        "personal": root / "egressd" / "personal",
        "provider-status": root / "egressd" / "provider-status",
        "state": root / "egressd" / "state",
        "client-bundles": root / "client-bundles",
        "client-secrets": secrets / "clients",
        "provider-secrets": secrets / "providers",
        "host-client-yaml": etc / "client.yaml",
        "host-client-token": etc / "client.token",
        "host-client-auth": etc / "default-proxy.auth",
    }


def test_migrate_identity_data_and_restore_backup(tmp_path, monkeypatch):
    root, secrets, etc, old_client, old_provider, old_route = _layout(tmp_path)
    monkeypatch.setattr(MIGRATION, "default_client_id",
                        lambda _uid, environment: f"{environment}-alice")
    client_map, provider_map, metadata = MIGRATION.get_migration_maps(secrets, root)
    new_client = "host-alice"
    new_provider = "p-host-alice-blanc"
    new_route = f"{new_provider}:FI-node"
    assert client_map == {old_client: new_client}
    assert provider_map == {old_provider: new_provider}
    MIGRATION.preflight(root, secrets, etc, client_map, provider_map)
    backup = tmp_path / "backup"
    locations = _locations(root, secrets, etc)
    MIGRATION.make_backup(backup, locations)

    MIGRATION.apply_migration(root, secrets, etc, client_map, provider_map,
                              metadata, backup)

    assert (secrets / "clients" / f"{new_client}.token").read_text() == "fake-token\n"
    assert (secrets / "clients" / f"{new_client}.proxy-creds").read_text() == \
        "host-alice:fake-pass\n"
    assert (root / "client-bundles" / new_client / "client.yaml").read_text() == \
        "client_id: host-alice\n"
    migrated_meta = json.loads((root / "egressd" / "personal" /
                                f"{new_provider}.json").read_text())
    assert migrated_meta["provider_id"] == new_provider
    assert migrated_meta["client_id"] == new_client
    assert migrated_meta["legacy_provider_ids"] == [old_provider]
    assert migrated_meta["legacy_provider_ids_version"] == 1
    defaults = json.loads((root / "egressd" / "state" / "defaults.json").read_text())
    assert defaults["clients"][new_client]["route"] == new_route
    assert old_client not in defaults["clients"]
    db = sqlite3.connect(root / "egressd" / "state" / "metrics.sqlite")
    assert db.execute("SELECT client_id FROM clients").fetchone()[0] == new_client
    assert db.execute("SELECT route_id, provider FROM routes").fetchone() == \
        (new_route, new_provider)
    assert db.execute("SELECT route_id, client_id FROM health_samples").fetchone() == \
        (new_route, new_client)
    db.close()

    MIGRATION.restore_backup(backup, locations)
    assert (secrets / "clients" / f"{old_client}.token").read_text() == "fake-token\n"
    assert (root / "egressd" / "personal" / f"{old_provider}.json").exists()
    assert not (secrets / "clients" / f"{new_client}.token").exists()
    assert json.loads((root / "egressd" / "state" / "defaults.json").read_text())[
        "clients"][old_client]["route"] == old_route


def test_migration_refuses_active_oneshot_session(tmp_path, monkeypatch):
    root, secrets, etc, old_client, old_provider, _ = _layout(tmp_path)
    monkeypatch.setattr(MIGRATION, "default_client_id",
                        lambda _uid, environment: f"{environment}-alice")
    db = sqlite3.connect(root / "egressd" / "state" / "metrics.sqlite")
    db.execute("INSERT INTO oneshot_sessions VALUES('active', ?, 'route', NULL)",
               (old_client,))
    db.commit()
    db.close()
    client_map, provider_map, _ = MIGRATION.get_migration_maps(secrets, root)
    with pytest.raises(MIGRATION.MigrationError, match="active"):
        MIGRATION.preflight(root, secrets, etc, client_map, provider_map)
