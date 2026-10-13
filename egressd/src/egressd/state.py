"""Per-client default route / execution mode state (spec sections 15, 16).

The default route and default execution mode are **per-client** and isolated:
changing one client's default never touches another client's selector or mode
(spec 15/16). The state is persisted to a JSON file (atomic write) so it
survives an egressd restart; the actual Mihomo selector is reconciled from
this state by the caller (egressd) via the mihomo admin API.

Fallback rule (spec 15): if a client's selected route disappears after a
provider update, **only the affected client profiles** fall back to ``direct``
and emit a warning/audit event.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .catalog import DIRECT

VALID_MODES = ("proxy", "tun")


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class ClientState:
    """Holds per-client default route + mode; persists atomically.

    ``catalog`` is the live :class:`~egressd.catalog.RouteCatalog` used to
    validate route IDs and to drive the fallback rule.
    """

    def __init__(self, path: str | Path, catalog: Any):
        self.path = Path(path)
        self.catalog = catalog
        self._lock = threading.RLock()
        # client_id -> {"route": route_id, "mode": "proxy"|"tun"}
        self._defaults: dict[str, dict[str, str]] = {}
        self._load()

    # ---- persistence ------------------------------------------------------
    def _load(self) -> None:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                for cid, st in data.get("clients", {}).items():
                    self._defaults[cid] = {
                        "route": st.get("route", DIRECT),
                        "mode": st.get("mode", "proxy"),
                    }
            except (json.JSONDecodeError, OSError):
                self._defaults = {}
        else:
            self._defaults = {}

    def _save(self) -> None:
        _atomic_write_json(self.path, {"clients": self._defaults})

    # ---- accessors --------------------------------------------------------
    def get(self, client_id: str) -> dict[str, str]:
        with self._lock:
            st = self._defaults.get(client_id)
            if st is None:
                return {"route": DIRECT, "mode": "proxy"}
            return dict(st)

    def all(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {cid: dict(st) for cid, st in self._defaults.items()}

    # ---- mutations --------------------------------------------------------
    def set_route(self, client_id: str, route_id: str,
                  reason: str = "set") -> tuple[str, bool]:
        """Set a client's default route.

        Returns ``(previous_route, changed)``. Raises ``ValueError`` if the
        route ID is not in the catalog (a client can only select a route that
        exists; ``direct`` always does).
        """
        if not self.catalog.exists(route_id):
            raise ValueError(f"unknown route {route_id!r}")
        with self._lock:
            prev = self._defaults.get(client_id, {}).get("route", DIRECT)
            if prev != route_id:
                self._defaults.setdefault(client_id, {"route": DIRECT, "mode": "proxy"})
                self._defaults[client_id]["route"] = route_id
                self._save()
                return prev, True
            return prev, False

    def set_mode(self, client_id: str, mode: str) -> tuple[str, bool]:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode {mode!r} (want one of {VALID_MODES})")
        with self._lock:
            prev = self._defaults.get(client_id, {}).get("mode", "proxy")
            if prev != mode:
                self._defaults.setdefault(client_id, {"route": DIRECT, "mode": "proxy"})
                self._defaults[client_id]["mode"] = mode
                self._save()
                return prev, True
            return prev, False

    def reconcile(self, catalog: Any) -> list[dict[str, str]]:
        """Apply the fallback rule after a provider update.

        For every client whose selected route is no longer in the catalog,
        fall back to ``direct`` and return a record for the audit log. Only the
        affected clients are touched (spec 15).
        """
        self.catalog = catalog
        affected: list[dict[str, str]] = []
        with self._lock:
            for cid, st in list(self._defaults.items()):
                if st["route"] != DIRECT and not catalog.exists(st["route"]):
                    affected.append({
                        "client_id": cid,
                        "from_route": st["route"],
                        "to_route": DIRECT,
                        "reason": "route_missing_after_provider_update",
                    })
                    st["route"] = DIRECT
            if affected:
                self._save()
        return affected
