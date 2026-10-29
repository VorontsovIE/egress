#!/usr/bin/env python3
"""Migrate UID-derived client IDs and hashed personal provider IDs.

Run only while egressd and Mihomo are stopped. The operation preflights all
destinations, refuses active one-shot sessions, takes a restricted backup,
and restores that backup if any write fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "host"))
from client_identity import default_client_id  # noqa: E402

CLIENT_OLD_RE = re.compile(r"^(host|container)-uid-([0-9]+)$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,98}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
MIGRATABLE_COLUMNS = {
    "clients": {"client_id"},
    "routes": {"route_id", "provider"},
    "route_identities": {"route_id"},
    "health_samples": {"route_id", "client_id"},
    "probe_samples": {"route_id", "client_id"},
    "benchmark_samples": {"route_id", "client_id"},
    "provider_update_events": {"provider", "client_id"},
    "default_selection_events": {"client_id", "from_route", "to_route"},
    "oneshot_sessions": {"client_id", "route"},
    "geo_cache": {"route_id"},
}


class MigrationError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise MigrationError(f"cannot read migration input {path}: {exc}") from None
    if not isinstance(value, dict):
        raise MigrationError(f"invalid migration input: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.migration-tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def file_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def ensure_regular_or_missing(paths: list[Path]) -> None:
    for path in paths:
        if path.is_symlink():
            raise MigrationError(f"refusing symlink in migration path: {path}")


def make_backup(backup: Path, locations: dict[str, Path]) -> None:
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(backup, 0o700)
    manifest = {}
    for label, source in locations.items():
        dest = backup / label
        if source.exists():
            if source.is_dir():
                shutil.copytree(source, dest, symlinks=True)
            else:
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, dest, follow_symlinks=False)
            manifest[label] = True
        else:
            manifest[label] = False
    (backup / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    os.chmod(backup / "manifest.json", 0o600)


def restore_backup(backup: Path, locations: dict[str, Path]) -> None:
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for label, target in locations.items():
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        source = backup / label
        if manifest.get(label) and source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target, follow_symlinks=False)


def migrate_file(source: Path, target: Path) -> None:
    if not file_present(source):
        return
    if source.is_symlink() or target.is_symlink():
        raise MigrationError(f"refusing symlink in migration path: {source} or {target}")
    if target.exists():
        raise MigrationError(f"migration target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError:
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
            shutil.rmtree(source)
        else:
            shutil.copy2(source, target)
            source.unlink()


def personal_provider_id(client_id: str, alias: str) -> str:
    if not CLIENT_ID_RE.fullmatch(client_id) or not ALIAS_RE.fullmatch(alias):
        raise MigrationError("personal provider metadata has an invalid owner or alias")
    value = f"p-{client_id}-{alias}"
    if not SAFE_ID_RE.fullmatch(value):
        raise MigrationError(f"new personal provider ID is invalid: {value!r}")
    return value


def update_route(route: object, provider_map: dict[str, str]) -> object:
    if not isinstance(route, str) or ":" not in route:
        return route
    provider, node = route.split(":", 1)
    return f"{provider_map.get(provider, provider)}:{node}"


def get_migration_maps(secrets: Path, root: Path) -> tuple[dict[str, str], dict[str, str], dict[str, dict]]:
    clients = secrets / "clients"
    client_map: dict[str, str] = {}
    if clients.exists():
        for token_path in sorted(clients.glob("*.token")):
            if token_path.is_symlink():
                raise MigrationError(f"refusing symlink: {token_path}")
            old_id = token_path.name[:-len(".token")]
            profile_path = clients / f"{old_id}.profile.json"
            if profile_path.is_symlink():
                raise MigrationError(f"refusing symlink: {profile_path}")
            profile = read_json(profile_path) if profile_path.exists() else {}
            match = CLIENT_OLD_RE.fullmatch(old_id)
            if not match or (profile and profile.get("client_id") != old_id):
                continue
            env, uid_text = match.groups()
            if profile and profile.get("environment") not in (None, env):
                raise MigrationError(f"profile environment disagrees with ID: {profile_path}")
            new_id = default_client_id(int(uid_text), env)
            if new_id != old_id:
                client_map[old_id] = new_id

    personal_dir = root / "egressd" / "personal"
    provider_map: dict[str, str] = {}
    metadata: dict[str, dict] = {}
    if personal_dir.exists():
        for meta_path in sorted(personal_dir.glob("*.json")):
            if meta_path.is_symlink():
                raise MigrationError(f"refusing symlink: {meta_path}")
            meta = read_json(meta_path)
            old_provider = meta.get("provider_id", meta_path.stem)
            owner = meta.get("client_id")
            alias = meta.get("alias")
            if not isinstance(old_provider, str) or not isinstance(owner, str) or \
                    not isinstance(alias, str):
                raise MigrationError(f"personal provider metadata is incomplete: {meta_path}")
            if old_provider != meta_path.stem or not SAFE_ID_RE.fullmatch(old_provider):
                raise MigrationError(f"personal provider ID does not match its file: {meta_path}")
            new_owner = client_map.get(owner, owner)
            new_provider = personal_provider_id(new_owner, alias)
            if old_provider != new_provider:
                provider_map[old_provider] = new_provider
            metadata[old_provider] = {**meta, "provider_id": new_provider,
                                      "client_id": new_owner}
    return client_map, provider_map, metadata


def preflight(root: Path, secrets: Path, etc_client: Path,
              client_map: dict[str, str], provider_map: dict[str, str]) -> None:
    providers = root / "providers"
    personal = root / "egressd" / "personal"
    status = root / "egressd" / "provider-status"
    client_dir = secrets / "clients"
    bundles = root / "client-bundles"
    sources = secrets / "providers"
    candidates: list[Path] = []
    candidates.extend(etc_client / name for name in
                     ("client.yaml", "client.token", "default-proxy.auth"))
    for old, new in client_map.items():
        candidates += [client_dir / f"{old}{suffix}" for suffix in
                       (".token", ".profile.json", ".proxy-creds")]
        candidates.append(bundles / old)
        for suffix in (".token", ".profile.json", ".proxy-creds"):
            target = client_dir / f"{new}{suffix}"
            candidates.append(target)
            if target.exists():
                raise MigrationError(f"client migration target already exists: {target}")
            if target.is_symlink():
                raise MigrationError(f"refusing symlink: {target}")
        candidates.append(bundles / new)
        if (bundles / new).exists():
            raise MigrationError(f"client bundle migration target already exists: {bundles / new}")
    for old, new in provider_map.items():
        for directory, suffixes in ((providers, (".yaml", ".yaml.lkg")),
                                    (personal, (".json",)),
                                    (status, (".json",)),
                                    (sources, (".json", ".source"))):
            for suffix in suffixes:
                candidates.append(directory / f"{old}{suffix}")
                target = directory / f"{new}{suffix}"
                candidates.append(target)
                if target.exists():
                    raise MigrationError(f"provider migration target already exists: {target}")
                if target.is_symlink():
                    raise MigrationError(f"refusing symlink: {target}")
    ensure_regular_or_missing(candidates)

    db = root / "egressd" / "state" / "metrics.sqlite"
    if db.exists():
        if db.is_symlink():
            raise MigrationError(f"refusing symlink: {db}")
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "oneshot_sessions" in tables:
                active = conn.execute(
                    "SELECT COUNT(*) FROM oneshot_sessions WHERE ended_at IS NULL").fetchone()[0]
                if active:
                    raise MigrationError(
                        f"{active} one-shot session(s) are active; stop workloads before migration")
            conn.close()
        except sqlite3.Error as exc:
            raise MigrationError(f"cannot inspect metrics DB: {exc}") from None


def migrate_sqlite(db: Path, client_map: dict[str, str],
                   provider_map: dict[str, str]) -> None:
    if not db.exists():
        return
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        temp_client = {old: f"__egress_migrate_client_{i}__"
                       for i, old in enumerate(client_map)}
        tables &= MIGRATABLE_COLUMNS.keys()
        route_ids: set[str] = set()
        for table in tables:
            cols = MIGRATABLE_COLUMNS[table]
            for column in ("route_id", "route", "from_route", "to_route"):
                if column in cols:
                    for (route_id,) in conn.execute(
                            f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"):
                        if isinstance(route_id, str):
                            route_ids.add(route_id)
        route_map: dict[str, str] = {}
        for route_id in route_ids:
            old_provider, separator, node = route_id.partition(":")
            if separator and old_provider in provider_map:
                route_map[route_id] = f"{provider_map[old_provider]}:{node}"
        # Update all known string-reference columns in two passes so mappings
        # cannot collide while rows are being rewritten.
        for table in sorted(tables):
            cols = MIGRATABLE_COLUMNS[table]
            if "client_id" in cols:
                for old, temp in temp_client.items():
                    conn.execute(f"UPDATE {table} SET client_id=? WHERE client_id=?", (temp, old))
            for column in ("route_id", "route", "from_route", "to_route"):
                if column not in cols:
                    continue
                for old_route, new_route in route_map.items():
                    # Avoid primary-key collisions; the preflight's target IDs
                    # are checked below and intermediate IDs preserve rows.
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?",
                                 ("__egress_migrate_route__" + old_route, old_route))
        for table in sorted(tables):
            cols = MIGRATABLE_COLUMNS[table]
            if "client_id" in cols:
                for old, temp in temp_client.items():
                    conn.execute(f"UPDATE {table} SET client_id=? WHERE client_id=?",
                                 (client_map[old], temp))
            if "provider" in cols:
                for old, new in provider_map.items():
                    conn.execute(f"UPDATE {table} SET provider=? WHERE provider=?", (new, old))
            for column in ("route_id", "route", "from_route", "to_route"):
                if column not in cols:
                    continue
                for old_route, new_route in route_map.items():
                    conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?",
                                 (new_route, "__egress_migrate_route__" + old_route))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_migration(root: Path, secrets: Path, etc_client: Path,
                    client_map: dict[str, str], provider_map: dict[str, str],
                    metadata: dict[str, dict], backup: Path) -> None:
    clients = secrets / "clients"
    bundles = root / "client-bundles"
    for old, new in client_map.items():
        for suffix in (".token", ".profile.json", ".proxy-creds"):
            migrate_file(clients / f"{old}{suffix}", clients / f"{new}{suffix}")
        profile_path = clients / f"{new}.profile.json"
        if profile_path.exists():
            profile = read_json(profile_path)
        else:
            env = "host" if old.startswith("host-") else "container"
            profile = {"client_id": new, "environment": env, "modes": ["proxy"]}
        profile["client_id"] = new
        write_json(profile_path, profile)
        creds = clients / f"{new}.proxy-creds"
        if creds.exists():
            raw = creds.read_text(encoding="utf-8").strip()
            _, sep, password = raw.partition(":")
            if not sep:
                raise MigrationError(f"invalid proxy credential file: {creds}")
            creds.write_text(f"{new}:{password}\n", encoding="utf-8")
            os.chmod(creds, 0o600)
        old_bundle, new_bundle = bundles / old, bundles / new
        if old_bundle.exists():
            if old_bundle.is_symlink() or new_bundle.exists():
                raise MigrationError(f"unsafe or colliding client bundle: {new_bundle}")
            migrate_file(old_bundle, new_bundle)
            yaml_path = new_bundle / "client.yaml"
            if yaml_path.exists():
                text = yaml_path.read_text(encoding="utf-8")
                text, count = re.subn(r"(?m)^client_id:[ \t]*" + re.escape(old) + r"[ \t]*$",
                                      f"client_id: {new}", text, count=1)
                if count != 1:
                    raise MigrationError(f"client bundle identity mismatch: {yaml_path}")
                yaml_path.write_text(text, encoding="utf-8")
            auth = new_bundle / "default-proxy.auth"
            if auth.exists():
                _, sep, password = auth.read_text(encoding="utf-8").strip().partition(":")
                if not sep:
                    raise MigrationError(f"invalid proxy auth file: {auth}")
                auth.write_text(f"{new}:{password}\n", encoding="utf-8")
                os.chmod(auth, 0o600)

    for old, new in provider_map.items():
        for directory, suffixes in ((root / "providers", (".yaml", ".yaml.lkg")),
                                    (root / "egressd" / "personal", (".json",)),
                                    (root / "egressd" / "provider-status", (".json",)),
                                    (secrets / "providers", (".json", ".source"))):
            for suffix in suffixes:
                migrate_file(directory / f"{old}{suffix}", directory / f"{new}{suffix}")
        meta_path = root / "egressd" / "personal" / f"{new}.json"
        meta = metadata[old]
        legacy = list(meta.get("legacy_provider_ids", []))
        if old not in legacy:
            legacy.append(old)
        meta["legacy_provider_ids"] = sorted(set(x for x in legacy if isinstance(x, str)))
        meta["legacy_provider_ids_version"] = 1
        write_json(meta_path, meta)

    defaults = root / "egressd" / "state" / "defaults.json"
    if defaults.exists():
        data = read_json(defaults)
        clients_data = data.get("clients", {})
        if isinstance(clients_data, dict):
            for old, new in client_map.items():
                if old in clients_data:
                    if new in clients_data:
                        raise MigrationError(f"default state target already exists for {new}")
                    clients_data[new] = clients_data.pop(old)
            for state in clients_data.values():
                if isinstance(state, dict) and "route" in state:
                    state["route"] = update_route(state["route"], provider_map)
        write_json(defaults, data)

    migrate_sqlite(root / "egressd" / "state" / "metrics.sqlite",
                   client_map, provider_map)

    # Refresh the already-installed host client when it points at a migrated
    # UID profile. Its token stays the same; only proxy username changes.
    client_yaml = etc_client / "client.yaml"
    if client_yaml.exists() and not client_yaml.is_symlink():
        text = client_yaml.read_text(encoding="utf-8")
        for old, new in client_map.items():
            if re.search(r"(?m)^client_id:[ \t]*" + re.escape(old) + r"[ \t]*$", text):
                text = re.sub(r"(?m)^client_id:[ \t]*" + re.escape(old) + r"[ \t]*$",
                              f"client_id: {new}", text, count=1)
                token = clients / f"{new}.token"
                creds = clients / f"{new}.proxy-creds"
                if token.exists():
                    shutil.copy2(token, etc_client / "client.token")
                    os.chmod(etc_client / "client.token", 0o600)
                if creds.exists():
                    shutil.copy2(creds, etc_client / "default-proxy.auth")
                    os.chmod(etc_client / "default-proxy.auth", 0o600)
                break
        client_yaml.write_text(text, encoding="utf-8")
        os.chmod(client_yaml, 0o644)

    write_json(root / "egressd" / "state" / "identity-migration.json", {
        "version": 1,
        "backup": str(backup),
        "clients": client_map,
        "providers": provider_map,
        "migrated_at": int(time.time()),
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=os.environ.get("VIBELAB_EGRESS_ROOT",
                                                           "/var/lib/vibelab-egress"))
    parser.add_argument("--secrets-dir", default=os.environ.get("VIBELAB_EGRESS_SECRETS",
                                                                  "/etc/vibelab-egress/secrets"))
    parser.add_argument("--etc-client-dir", default="/etc/vibelab-egress")
    parser.add_argument("--check", action="store_true", help="preflight without changing files")
    parser.add_argument("--restore", metavar="BACKUP", help="restore a migration backup")
    args = parser.parse_args()
    root, secrets, etc_client = map(Path, (args.root, args.secrets_dir, args.etc_client_dir))
    locations = {
        "providers": root / "providers",
        "personal": root / "egressd" / "personal",
        "provider-status": root / "egressd" / "provider-status",
        "state": root / "egressd" / "state",
        "client-bundles": root / "client-bundles",
        "client-secrets": secrets / "clients",
        "provider-secrets": secrets / "providers",
        "host-client-yaml": etc_client / "client.yaml",
        "host-client-token": etc_client / "client.token",
        "host-client-auth": etc_client / "default-proxy.auth",
    }
    try:
        if args.restore:
            restore_backup(Path(args.restore), locations)
            print(f"restored identity migration backup: {args.restore}")
            return 0
        client_map, provider_map, metadata = get_migration_maps(secrets, root)
        if not client_map and not provider_map:
            print("identity migration: no UID-derived profiles or hashed personal providers found")
            return 0
        preflight(root, secrets, etc_client, client_map, provider_map)
        print(f"identity migration preflight: {len(client_map)} client(s), "
              f"{len(provider_map)} personal provider(s)")
        if args.check:
            return 0
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup = root / "identity-migrations" / f"backup-{stamp}"
        backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if backup.parent.is_symlink():
            raise MigrationError(f"refusing symlink migration backup directory: {backup.parent}")
        os.chmod(backup.parent, 0o700)
        make_backup(backup, locations)
        try:
            apply_migration(root, secrets, etc_client, client_map, provider_map,
                            metadata, backup)
        except Exception:
            restore_backup(backup, locations)
            raise
        print(f"identity migration complete; backup: {backup}")
        for old, new in client_map.items():
            print(f"  client {old} -> {new}")
        for old, new in provider_map.items():
            print(f"  provider {old} -> {new}")
        return 0
    except (MigrationError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"identity migration: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
