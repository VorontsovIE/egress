"""Live config generation + hot reload (spec A3: egressd owns config gen).

In A1 the bootstrap path (``bootstrap_config.py``) generated the Mihomo config
once at install time. In A3 egressd owns this responsibility at runtime: when a
provider's inventory changes (a subscription update), egressd regenerates the
full Mihomo config and hot-reloads it via the mihomo admin API
(``PUT /configs``), so the new node set is live without a process restart.

Hot reload is safe for per-client defaults because the generated config sets
``profile.store-selected: true`` (validated in A2): selector selections are
persisted in the bbolt cache and restored on reload. After a reload egressd
re-asserts every client's selector from its persisted default (see the
reconcile path), so a deleted route cannot leave a selector dangling.

The inputs are the same sources the bootstrap path reads, addressed by env so
egressd is layout-agnostic:

  * MIHOMO_SECRET_FILE        -- the mihomo admin secret
  * EGRESSD_CLIENTS_DIR       -- <id>.token / <id>.profile.json / <id>.proxy-creds
  * EGRESSD_ONESHOT_SLOTS_FILE-- {os_i: password}
  * EGRESSD_PROVIDERS_DIR     -- provider *.yaml files (inside the mihomo home)
  * EGRESSD_CONFIG_OUT        -- where the generated config.yaml is written
  * EGRESSD_MIHO_CONFIG_PATH  -- path to that file inside the mihomo container
  * EGRESSD_MIHO_CONTROLLER_LISTEN -- Mihomo's own admin API bind address
  * EGRESSD_DEFAULT_PROXY_PORT / EGRESSD_ONESHOT_PROXY_PORT / MIHOMO_API_URL
  * EGRESSD_LISTENER_LISTEN   -- SOCKS bind address (127.0.0.1 or 0.0.0.0)
  * EGRESSD_DOCKER_CIDRS_FILE -- optional hard-direct CIDRs (spec 35)
"""
from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .configgen import (
    DEFAULT_HARD_DIRECT_CIDRS,
    ClientDef,
    EgressConfig,
    OneShotSlot,
    ProviderDef,
)
from .personal import load_metadata


def load_mihomo_secret() -> str | None:
    p = Path(os.environ.get("MIHOMO_SECRET_FILE", "/etc/egress/secrets/mihomo_api_secret"))
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


def load_clients(clients_dir: str) -> list[ClientDef]:
    d = Path(clients_dir)
    clients: list[ClientDef] = []
    # egressd runs non-root and may not be able to read the root-provisioned
    # client secrets (PermissionError on is_dir/glob/read_text). Degrade to an
    # empty list (egressd then runs in offline/no-config-controller mode)
    # instead of crashing the control plane.
    try:
        if not d.is_dir():
            return clients
        for tok in sorted(d.glob("*.token")):
            cid = tok.name[: -len(".token")]
            creds = d / f"{cid}.proxy-creds"
            if not creds.is_file():
                continue
            user, _, password = creds.read_text().strip().partition(":")
            if user and password:
                clients.append(ClientDef(client_id=cid, proxy_user=user, proxy_password=password))
    except OSError as e:
        import logging
        logging.getLogger("egressd").error(
            "load_clients: cannot read clients dir %s (%s); continuing with "
            "no clients (config generation disabled until ownership is fixed)",
            clients_dir, e)
    return clients


def load_oneshot_slots(slots_file: str | None, count: int) -> list[OneShotSlot]:
    from .probe import load_slot_passwords

    passwords = load_slot_passwords(count, slots_file)
    slots: list[OneShotSlot] = []
    for i in range(count):
        user = f"os_{i}"
        slots.append(OneShotSlot(index=i, user=user, password=passwords[user]))
    return slots


def load_providers(providers_dir: str) -> list[ProviderDef]:
    d = Path(providers_dir)
    providers: list[ProviderDef] = []
    if not d.is_dir():
        return providers
    for p in sorted(d.glob("*.yaml")):
        # the path is the absolute file path (inside the mihomo home), which is
        # what mihomo's file provider reads and what egressd re-reads on update.
        mihomo_dir = os.environ.get("EGRESSD_MIHO_PROVIDER_DIR", providers_dir)
        providers.append(ProviderDef(
            name=p.stem, type="file", path=str(Path(mihomo_dir) / p.name), prefix=f"{p.stem}-",
            owner=(load_metadata(p.stem) or {}).get("client_id"),
        ))
    return providers


def load_docker_cidrs(path: str | None) -> list[str]:
    if not path:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    cidrs: list[str] = []
    for m in re.finditer(r"^\s+subnet:\s*'?([\d./]+)'?", p.read_text(), re.MULTILINE):
        if m.group(1) not in cidrs:
            cidrs.append(m.group(1))
    return cidrs


def build_egress_config() -> EgressConfig | None:
    """Build the EgressConfig from env. Returns None if the required inputs
    (mihomo secret + at least one client) are not present -- egressd then runs
    in a degraded mode where provider updates write the file but cannot
    hot-reload mihomo (the offline/test path)."""
    secret = load_mihomo_secret()
    clients_dir = os.environ.get("EGRESSD_CLIENTS_DIR", "/etc/egress/secrets/clients")
    clients = load_clients(clients_dir)
    if not secret or not clients:
        return None
    providers_dir = os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers")
    slots_count = int(os.environ.get("EGRESSD_ONESHOT_SLOTS", "8"))
    slots_file = os.environ.get("EGRESSD_ONESHOT_SLOTS_FILE")
    cfg = EgressConfig(
        mihomo_secret=secret,
        clients=clients,
        providers=load_providers(providers_dir),
        oneshot_slots=load_oneshot_slots(slots_file, slots_count),
        default_port=int(os.environ.get("EGRESSD_DEFAULT_PROXY_PORT", "18080")),
        oneshot_port=int(os.environ.get("EGRESSD_ONESHOT_PROXY_PORT", "18081")),
        controller=os.environ.get("EGRESSD_MIHO_CONTROLLER_LISTEN", "0.0.0.0:9090"),
        dns_listen=os.environ.get("EGRESSD_DNS_LISTEN", "127.0.0.1:1053"),
        listener_listen=os.environ.get("EGRESSD_LISTENER_LISTEN", "127.0.0.1"),
    )
    cfg.hard_direct_cidrs = list(DEFAULT_HARD_DIRECT_CIDRS) + load_docker_cidrs(
        os.environ.get("EGRESSD_DOCKER_CIDRS_FILE"))
    return cfg


class ConfigController:
    """Regenerates the Mihomo config and hot-reloads it (spec A3)."""

    def __init__(self, egress_config: EgressConfig, config_out: str | Path,
                 mihomo: Any, mihomo_config_path: str | Path | None = None):
        self.cfg = egress_config
        self.config_out = Path(config_out)
        self.mihomo_config_path = Path(mihomo_config_path or config_out)
        self.mihomo = mihomo

    def refresh(self) -> None:
        """Re-read the provider/client/slot inputs from disk so the generated
        config reflects the current inventory (a provider file may have been
        added, removed, or its node set changed by an update)."""
        providers_dir = os.environ.get("EGRESSD_PROVIDERS_DIR", "/root/.config/mihomo/providers")
        clients_dir = os.environ.get("EGRESSD_CLIENTS_DIR", "/etc/egress/secrets/clients")
        slots_file = os.environ.get("EGRESSD_ONESHOT_SLOTS_FILE")
        slots_count = int(os.environ.get("EGRESSD_ONESHOT_SLOTS", "8"))
        self.cfg.providers = load_providers(providers_dir)
        self.cfg.clients = load_clients(clients_dir)
        self.cfg.oneshot_slots = load_oneshot_slots(slots_file, slots_count)

    def regenerate(self) -> Path:
        """Write the current config to disk (atomic)."""
        import yaml

        self.refresh()
        data = self.cfg.generate()
        text = yaml.safe_dump(data, sort_keys=False)
        self.config_out.parent.mkdir(parents=True, exist_ok=True)
        # atomic write
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=str(self.config_out.parent),
                                   prefix=f".{self.config_out.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.config_out)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return self.config_out

    def hot_reload(self, wait_s: float = 20.0) -> tuple[bool, str]:
        """PUT /configs to mihomo and wait for it to come back up."""
        st, _ = self.mihomo.put("/configs", {"path": str(self.mihomo_config_path)})
        if st not in (200, 204):
            return False, f"mihomo /configs returned {st}"
        # mihomo reloads asynchronously; poll /version until it is back
        deadline = time.time() + wait_s
        while time.time() < deadline:
            st2, _ = self.mihomo.get("/version", timeout=3.0)
            if st2 == 200:
                return True, "reloaded"
            time.sleep(0.4)
        return False, "mihomo did not come back up after reload"
