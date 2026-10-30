"""Container-owned personal subscription sources.

This optional feature uses the project's provider adapter and PyYAML in the
container. The core egressctl/egress-run package stays stdlib-only. Raw source
URLs and files never cross the API; only normalized provider snapshots do.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
MAX_SOURCE_BYTES = 4 * 1024 * 1024


def source_dir() -> Path:
    override = os.environ.get("EGRESS_SOURCE_DIR")
    if override:
        return Path(override)
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        return Path(state) / "egress" / "sources"
    home = Path.home()
    if not str(home) or str(home) == "/":
        raise ValueError("set EGRESS_SOURCE_DIR for this container user")
    return home / ".local" / "state" / "egress" / "sources"


def path_for(name: str) -> Path:
    if not isinstance(name, str) or not ALIAS.fullmatch(name):
        raise ValueError("invalid source name")
    return source_dir() / f"{name}.json"


def save(name: str, kind: str, source: str) -> None:
    path = path_for(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temp = tempfile.mkstemp(prefix=".source-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump({"kind": kind, "source": source}, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(name: str) -> dict:
    data = json.loads(path_for(name).read_text())
    if not isinstance(data, dict) or data.get("kind") not in ("url", "file", "uri") or \
            not isinstance(data.get("source"), str):
        raise ValueError("invalid local source")
    return data


def remove(name: str) -> None:
    path_for(name).unlink(missing_ok=True)


def read_source(name: str) -> str:
    source = load(name)
    if source["kind"] in ("file", "uri"):
        return source["source"]
    with urllib.request.urlopen(source["source"], timeout=30) as response:
        raw = response.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("subscription too large")
    return raw.decode("utf-8")


def normalize(name: str) -> str:
    try:
        from adapters.common import normalize_source
    except ImportError as exc:
        raise RuntimeError("container source adapter requires PyYAML in the container") from exc
    source = load(name)
    normalized, parsed = normalize_source(read_source(name))
    if source["kind"] == "uri" and ("\n" in source["source"] or "\r" in source["source"] or
                                    parsed["format"] not in ("vless", "uris") or
                                    parsed["count"] != 1):
        raise ValueError("personal URI source needs one proxy")
    return normalized
