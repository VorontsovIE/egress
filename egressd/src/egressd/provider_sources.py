"""Private provider source inventory and public, redacted update status."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,98}$")
MIHOMO_PROVIDER_DIR = "/root/.config/mihomo/providers"
REFRESH_INTERVAL = 600


def source_dir() -> Path:
    return Path(os.environ.get("EGRESSD_PROVIDER_SOURCES_DIR", "/etc/vibelab-egress/secrets/providers"))


def status_dir() -> Path:
    return Path(os.environ.get("EGRESSD_PROVIDER_STATUS_DIR", "/var/lib/egressd/provider-status"))


def load_source(provider_id: str) -> dict | None:
    if not ID_RE.fullmatch(provider_id):
        return None
    try:
        data = json.loads((source_dir() / f"{provider_id}.json").read_text())
        if not isinstance(data, dict) or data.get("type") not in ("url", "file"):
            return None
        if data["type"] == "url" and not isinstance(data.get("url"), str):
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def read_status(provider_id: str) -> dict:
    if not ID_RE.fullmatch(provider_id):
        return {}
    try:
        data = json.loads((status_dir() / f"{provider_id}.json").read_text())
        if not isinstance(data, dict):
            return {}
        return {k: data.get(k) for k in ("last_success", "last_error")}
    except (OSError, ValueError, TypeError):
        return {}


def write_status(provider_id: str, *, ok: bool, error: str | None = None) -> None:
    if not ID_RE.fullmatch(provider_id):
        return
    path = status_dir() / f"{provider_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = read_status(provider_id)
    if ok:
        status = {"last_success": int(time.time()), "last_error": None}
    else:
        # Fixed category only; exceptions can contain URL and credentials.
        status["last_error"] = error or "update_failed"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".status-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(status, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
