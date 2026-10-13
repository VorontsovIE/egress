"""Bootstrap Mihomo config generation (A1).

One-shot generator used by ``install-backend-host.sh`` so the compose
stack can start: reads backend secrets + provider files + the Docker CIDR
artifact and writes ``<root>/generated/config.yaml``.

In A3 egressd owns this responsibility at runtime (regeneration + hot
reload on provider/client changes); this module is the deterministic
bootstrap path and stays the reference for the inputs the live generator
consumes.

Inputs (all backend-side; nothing client-facing is read):
  * ``<secrets>/mihomo_api_secret``
  * ``<secrets>/clients/<id>.token`` + ``<id>.proxy-creds`` (user:password)
  * ``<secrets>/clients/oneshot-slots.json`` (created here if absent)
  * ``<root>/providers/*.yaml`` (file providers, mounted into mihomo)
  * ``<root>/generated/docker-cidrs.yaml`` (hard-direct CIDRs, spec 35)

Usage:
  python3 -m egressd.bootstrap_config --root <root> --secrets-dir <dir> \
      [--oneshot-slots N] [--out <path>]

Stdlib-only (no PyYAML): emits the config with a small deterministic
YAML writer.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

from .configgen import (
    ClientDef,
    EgressConfig,
    OneShotSlot,
    ProviderDef,
)

# In-container path of the mounted providers dir (see compose.yaml).
PROVIDERS_MOUNT = "/root/.config/mihomo/providers"


# ---------------------------------------------------------------------------
# minimal deterministic YAML emitter (covers the generated config shape:
# nested dicts, lists of scalars, lists of dicts)
# ---------------------------------------------------------------------------
def _fmt_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return "''"
    needs_quote = (
        any(c in s for c in ":#{}[]&*!|>'\"%@`,")
        or s != s.strip()
        or s.lower() in ("true", "false", "null", "yes", "no", "on", "off")
        or re.fullmatch(r"[-+]?\d+(\.\d+)?", s) is not None
    )
    if needs_quote:
        return "'" + s.replace("'", "''") + "'"
    return s


def _dump_list(items, indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for i in items:
        if isinstance(i, dict) and i:
            sub = _dump_map(i, indent + 1)
            first = sub[0][len(pad) + 2:]
            lines.append(f"{pad}- {first}")
            lines.extend(sub[1:])
        elif isinstance(i, (dict, list)):
            lines.append(f"{pad}- {{}}" if isinstance(i, dict) else f"{pad}- []")
        else:
            lines.append(f"{pad}- {_fmt_scalar(i)}")
    return lines


def _dump_map(obj: dict, indent: int) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    for k, v in obj.items():
        key = _fmt_scalar(str(k))
        if isinstance(v, dict) and v:
            lines.append(f"{pad}{key}:")
            lines.extend(_dump_map(v, indent + 1))
        elif isinstance(v, list) and v:
            lines.append(f"{pad}{key}:")
            lines.extend(_dump_list(v, indent + 1))
        elif isinstance(v, dict):
            lines.append(f"{pad}{key}: {{}}")
        elif isinstance(v, list):
            lines.append(f"{pad}{key}: []")
        else:
            lines.append(f"{pad}{key}: {_fmt_scalar(v)}")
    return lines


def dump_yaml(obj: dict) -> str:
    return "\n".join(_dump_map(obj, 0)) + "\n"


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def load_mihomo_secret(secrets_dir: Path) -> str:
    p = secrets_dir / "mihomo_api_secret"
    if not p.is_file():
        raise SystemExit(f"bootstrap-config: missing {p}")
    return p.read_text().strip()


def load_clients(secrets_dir: Path) -> list[ClientDef]:
    clients_dir = secrets_dir / "clients"
    clients: list[ClientDef] = []
    if not clients_dir.is_dir():
        return clients
    for tok in sorted(clients_dir.glob("*.token")):
        client_id = tok.name[: -len(".token")]
        creds = clients_dir / f"{client_id}.proxy-creds"
        if not creds.is_file():
            print(f"bootstrap-config: WARNING: no proxy-creds for {client_id}; "
                  "skipping (run provision-client.sh)", file=sys.stderr)
            continue
        user, _, password = creds.read_text().strip().partition(":")
        if not user or not password:
            raise SystemExit(f"bootstrap-config: bad proxy-creds for {client_id}")
        clients.append(ClientDef(
            client_id=client_id,
            proxy_user=user,
            proxy_password=password,
        ))
    return clients


def load_oneshot_slots(secrets_dir: Path, count: int) -> list[OneShotSlot]:
    slots_file = secrets_dir / "clients" / "oneshot-slots.json"
    passwords: dict[str, str] = {}
    if slots_file.is_file():
        try:
            passwords = json.loads(slots_file.read_text())
        except json.JSONDecodeError:
            print("bootstrap-config: WARNING: unreadable oneshot-slots.json; "
                  "regenerating", file=sys.stderr)
    missing = [f"os_{i}" for i in range(count) if f"os_{i}" not in passwords]
    if missing:
        for name in missing:
            passwords[name] = secrets.token_urlsafe(24)
        slots_file.parent.mkdir(parents=True, exist_ok=True)
        slots_file.write_text(json.dumps(passwords, indent=2, sort_keys=True))
        os.chmod(slots_file, 0o600)
    return [
        OneShotSlot(index=i, user=f"os_{i}", password=passwords[f"os_{i}"])
        for i in range(count)
    ]


def load_providers(root: Path) -> list[ProviderDef]:
    providers_dir = root / "providers"
    providers: list[ProviderDef] = []
    if not providers_dir.is_dir():
        return providers
    for p in sorted(providers_dir.glob("*.yaml")):
        providers.append(ProviderDef(
            name=p.stem,
            type="file",
            path=f"{PROVIDERS_MOUNT}/{p.stem}.yaml",
            prefix=f"{p.stem}-",
        ))
    return providers


def load_docker_cidrs(root: Path) -> list[str]:
    artifact = root / "generated" / "docker-cidrs.yaml"
    if not artifact.is_file():
        return []
    cidrs: list[str] = []
    for m in re.finditer(r"^\s+subnet:\s*'?([\d./]+)'?",
                         artifact.read_text(), re.MULTILINE):
        cidr = m.group(1)
        if cidr not in cidrs:
            cidrs.append(cidr)
    return cidrs


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def generate(root: Path, secrets_dir: Path, oneshot_slots: int = 8,
             out: Path | None = None) -> Path:
    cfg = EgressConfig(
        mihomo_secret=load_mihomo_secret(secrets_dir),
        clients=load_clients(secrets_dir),
        providers=load_providers(root),
        oneshot_slots=load_oneshot_slots(secrets_dir, oneshot_slots),
        default_port=18080,
        oneshot_port=18081,
        controller="127.0.0.1:9090",
        listener_listen="0.0.0.0",  # container mode; compose publish binds
    )
    # extend hard-direct with Docker CIDRs (spec 35)
    from .configgen import DEFAULT_HARD_DIRECT_CIDRS
    cfg.hard_direct_cidrs = list(DEFAULT_HARD_DIRECT_CIDRS) + load_docker_cidrs(root)
    if not cfg.clients:
        raise SystemExit("bootstrap-config: no provisioned clients found "
                         f"in {secrets_dir / 'clients'} (run provision-client.sh)")
    data = cfg.generate()
    out = out or (root / "generated" / "config.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump_yaml(data))
    os.chmod(out, 0o600)
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="egressd.bootstrap_config")
    ap.add_argument("--root", default="/home/vibelab/openscience/egress")
    ap.add_argument("--secrets-dir", default="/etc/vibelab-egress/secrets")
    ap.add_argument("--oneshot-slots", type=int,
                    default=int(os.environ.get("EGRESSD_ONESHOT_SLOTS", "8")))
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out = generate(Path(args.root), Path(args.secrets_dir), args.oneshot_slots,
                   Path(args.out) if args.out else None)
    n_rules = out.read_text().count("\n  - ")  # rough; report exact below
    text = out.read_text()
    rules = len(re.findall(r"^\s+- (?:GEOIP|IP-CIDR|IN-USER|MATCH)", text, re.M))
    print(json.dumps({"ok": True, "config": str(out), "rules": rules}))


if __name__ == "__main__":
    main()
