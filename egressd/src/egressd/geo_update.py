"""GEO data update (spec sections 14, 41).

Mihomo is configured with ``geo-auto-update: true`` + ``geo-update-interval``
(native path, spec 14.1). This module is the **explicit, API-triggered** path
(spec 41): egressd downloads the datasets from the configured ``geox-url``
sources, validates them, writes them atomically, and records metadata so
``geo status`` can report source URL, last update time, hash, size and
staleness.

Design notes
------------
* egressd is the single GEO writer in the compose stack (the GEO dir is
  mounted read-only into mihomo, read-write into egressd). This avoids two
  writers racing on the same files.
* The fetcher is injectable (``fetcher(url) -> bytes``) so tests run offline
  with a local / canned fetcher; production uses :func:`http_fetch`.
* A failed update never destroys the previous working files: the download is
  validated (non-empty, sane size) before the atomic replace, and
  :func:`adapters.common.atomic_write` keeps a ``.lkg`` last-known-good copy.
* After a successful write, an optional ``reload_hook`` is called so mihomo
  can pick up the new datasets (best-effort; a reload failure does not undo
  the on-disk update).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any

# The provider adapters live at the repo root (``adapters/``). In the egressd
# container /app is on PYTHONPATH; locally/tests we add the repo root so the
# import resolves either way.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adapters.common import atomic_write_bytes  # noqa: E402

# Dataset registry: logical name -> (default filename, default source URL).
# Source URLs default to the MetaCubeX meta-rules-dat release (spec 14.1).
# NOTE: the upstream release tag is `latest` (a moving release updated in
# place); the old `release` tag no longer exists (404). These are the default
# sources for egressd's enrichment store; mihomo's own geox-url (in the
# generated config) is separate and manages the proxy's GEO data.
DEFAULT_SOURCES = {
    "geoip": (
        "GeoIP.dat",
        "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat",
    ),
    "geosite": (
        "GeoSite.dat",
        "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat",
    ),
    "asn": (
        "GeoLite2-ASN.mmdb",
        "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/GeoLite2-ASN.mmdb",
    ),
}
DATASET_NAMES = ("geoip", "geosite", "asn")
DEFAULT_INTERVAL_HOURS = 24
# A dataset smaller than this is considered corrupt/empty and rejected.
MIN_SIZE = 1024


def http_fetch(url: str, timeout: float = 60.0) -> bytes:
    """Production fetcher: download ``url`` and return the bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "vibelab-egress/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@dataclass
class GeoConfig:
    geo_dir: Path
    sources: dict[str, str] = field(default_factory=dict)
    filenames: dict[str, str] = field(default_factory=dict)
    interval_hours: int = DEFAULT_INTERVAL_HOURS
    fetcher: Callable[[str], bytes] = http_fetch
    reload_hook: Callable[[list[str]], None] | None = None

    def __post_init__(self) -> None:
        self.geo_dir = Path(self.geo_dir)
        self.geo_dir.mkdir(parents=True, exist_ok=True)
        for name in DATASET_NAMES:
            fn, url = DEFAULT_SOURCES[name]
            self.filenames.setdefault(name, fn)
            self.sources.setdefault(name, url)
        self._meta_path = self.geo_dir / "geo-metadata.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GeoUpdateError(Exception):
    pass


class GeoManager:
    def __init__(self, cfg: GeoConfig):
        self.cfg = cfg

    # ---- metadata ----
    def _read_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.cfg._meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        self.cfg._meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        os.chmod(self.cfg._meta_path, 0o600)

    # ---- update ----
    def update(self, names: tuple[str, ...] = DATASET_NAMES, force: bool = False,
               now: float | None = None) -> dict[str, Any]:
        """Download + atomically write the named datasets.

        Returns a per-dataset result: ``{"name": {"updated": bool, "reason":
        str, "sha256": str, "size": int, "source": str, ...}}``. A failed
        download leaves the previous file untouched (validated before replace).
        """
        now = now if now is not None else time.time()
        meta = self._read_meta()
        results: dict[str, Any] = {}
        updated_names: list[str] = []
        for name in names:
            if name not in DATASET_NAMES:
                raise GeoUpdateError(f"unknown dataset {name!r}")
            source = self.cfg.sources[name]
            filename = self.cfg.filenames[name]
            path = self.cfg.geo_dir / filename
            prev = meta.get(name, {})
            try:
                data = self.cfg.fetcher(source)
            except Exception as e:  # network / HTTP error
                results[name] = {"updated": False, "reason": f"fetch failed: {e}",
                                 "source": source, "previous_sha256": prev.get("sha256")}
                continue
            if not data or len(data) < MIN_SIZE:
                results[name] = {"updated": False,
                                 "reason": f"download too small ({len(data)} bytes)",
                                 "source": source, "previous_sha256": prev.get("sha256")}
                continue
            sha = _sha256(data)
            if not force and prev.get("sha256") == sha:
                results[name] = {"updated": False, "reason": "unchanged",
                                 "sha256": sha, "size": len(data), "source": source}
                continue
            atomic_write_bytes(path, data)
            on_disk = path.read_bytes()
            disk_sha = _sha256(on_disk)
            meta[name] = {
                "sha256": disk_sha,
                "size": len(on_disk),
                "source": source,
                "updated_at": now,
                "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            }
            updated_names.append(name)
            results[name] = {"updated": True, "reason": "ok", "sha256": disk_sha,
                             "size": len(on_disk), "source": source}
        if updated_names:
            self._write_meta(meta)
            if self.cfg.reload_hook:
                try:
                    self.cfg.reload_hook(updated_names)
                except Exception as e:  # reload is best-effort
                    for n in updated_names:
                        results.setdefault(n, {})["reload_error"] = str(e)
        return results

    # ---- status ----
    def status(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        meta = self._read_meta()
        datasets: dict[str, Any] = {}
        for name in DATASET_NAMES:
            filename = self.cfg.filenames[name]
            path = self.cfg.geo_dir / filename
            present = path.is_file()
            info: dict[str, Any] = {
                "present": present,
                "file": filename,
                "source": self.cfg.sources[name],
            }
            if present:
                data = path.read_bytes()
                m = meta.get(name, {})
                last = m.get("updated_at")
                if last is not None:
                    age_h = (now - last) / 3600.0
                    info["last_update"] = m.get("updated_at_iso")
                    info["last_update_ts"] = last
                    info["age_hours"] = round(age_h, 3)
                    info["stale"] = age_h > self.cfg.interval_hours
                else:
                    info["last_update"] = None
                    info["stale"] = None  # unknown age
                # hash the file on disk (authoritative)
                info["sha256"] = _sha256(data)
                info["size"] = len(data)
            datasets[name] = info
        return {
            "geo_dir": str(self.cfg.geo_dir),
            "interval_hours": self.cfg.interval_hours,
            "datasets": datasets,
            "all_present": all(d["present"] for d in datasets.values()),
        }
