"""Provider subscription adapters (spec sections 11, 12).

A provider adapter converts a raw provider subscription into the normalized
Mihomo/Clash provider YAML that the ``file`` proxy-provider reads:

    provider source -> provider-specific adapter -> Mihomo provider YAML

Adapter obligations (spec 11.2), enforced here:

* fixtures without secrets        -- see tests/fixtures/
* atomic replace                  -- :func:`atomic_write` (tmp + rename)
* keep last-known-good            -- :func:`atomic_write` writes ``<out>.lkg``
* never log private keys          -- :func:`redact` masks secret fields
* separate tests                  -- tests/integration/test_adapters.py

Formats handled (detection order in :func:`detect_format`):

* ``clash``   -- Mihomo/Clash-compatible YAML (``proxies: [...]``)
* ``base64``  -- base64-encoded Clash YAML (common subscription encoding)
* ``wg-conf`` -- a single WireGuard ``.conf`` (``[Interface]`` / ``[Peer]``)
* ``vless``   -- a ``vless://`` URI (one or more, one per line)

The normalized output is always ``{"proxies": [...]}`` so it can be dropped
straight into a Mihomo ``file`` provider. Node names are sanitized and, when a
``prefix`` is given, prefixed (e.g. ``[blanc] `` or ``blanc-``) so names are
unique across providers.

This module is importable from egressd (``from adapters.common import ...``);
the repo root is on ``sys.path`` inside the egressd container.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

try:  # PyYAML is available in egressd (pyproject dependency).
    import yaml
except ImportError:  # pragma: no cover - egressd always has pyyaml
    yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# secret handling
# ---------------------------------------------------------------------------
# Field names whose values must never appear in logs / reports.
SECRET_FIELDS = {
    "private-key", "privatekey", "preshared-key", "presharedkey",
    "psk", "uuid", "password", "secret", "token", "api-key",
    "apikey", "client-id", "client_id",
}
_MASK = "***REDACTED***"


def redact(obj: Any) -> Any:
    """Return a copy of ``obj`` with every secret field value masked.

    Works recursively on dicts/lists. Used before any log line or report is
    emitted so a private key / uuid / password can never leak.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in SECRET_FIELDS and isinstance(v, str) and v:
                out[k] = _MASK
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


# ---------------------------------------------------------------------------
# name handling
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r"[^A-Za-z0-9._\-]")


def _safe_name(name: str) -> str:
    name = _NAME_RE.sub("-", str(name).strip())
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name or "node"


def _apply_prefix(name: str, prefix: str | None) -> str:
    if not prefix:
        return name
    return f"{prefix}{name}"


def stable_slug(display: str) -> str:
    """Map a mutable display name to a stable, provider-unique id.

    "FI - Helsinki 1" -> "FI-Helsinki-1"; "US – New York 2" -> "US-New-York-2".
    The result is what a route ID depends on (spec section 13), so it must be
    deterministic and independent of punctuation/case quirks in the display
    name.
    """
    s = str(display).strip()
    # normalize dashes / separators
    s = re.sub(r"[\s\-–—_/]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "node"


# ---------------------------------------------------------------------------
# format detection
# ---------------------------------------------------------------------------
def detect_format(text: str) -> str:
    s = text.strip()
    if not s:
        raise ValueError("empty subscription source")
    if s.startswith("vless://"):
        return "vless"
    if re.search(r"^\[Interface\]", s, re.MULTILINE):
        return "wg-conf"
    # base64: a single line of base64 that decodes to Clash YAML
    if "\n" not in s and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
        try:
            dec = base64.b64decode(s, validate=True).decode("utf-8")
        except Exception:
            dec = ""
        if "proxies" in dec:
            return "base64"
    if "proxies" in s:
        return "clash"
    raise ValueError(
        "unrecognized subscription format (expected Clash YAML, base64 Clash, "
        "WireGuard .conf, or vless:// URI)"
    )


# ---------------------------------------------------------------------------
# per-format parsers -> list of proxy dicts
# ---------------------------------------------------------------------------
def _parse_clash_yaml(text: str) -> list[dict[str, Any]]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to parse Clash YAML")
    data = yaml.safe_load(text)
    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        return [p for p in data["proxies"] if isinstance(p, dict)]
    if isinstance(data, dict) and {"name", "type"}.issubset(data):
        return [data]
    raise ValueError("Clash YAML must contain a 'proxies' list")


def _parse_base64(text: str) -> list[dict[str, Any]]:
    dec = base64.b64decode(text.strip(), validate=True).decode("utf-8")
    return _parse_clash_yaml(dec)


def _parse_wg_conf(text: str) -> list[dict[str, Any]]:
    """Parse a single WireGuard .conf into one Mihomo wireguard proxy.

    Mihomo wireguard proxy fields (validated in PoC P21): name, type, server,
    port, ip, private-key, public-key, allowed-ips, persistent-keepalive.
    """
    if re.search(r"\[Interface\]", text) is None:
        raise ValueError("WireGuard .conf missing [Interface]")
    section = "none"
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"\[(.+)\]$", line)
        if m:
            section = m.group(1).strip().lower()
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            fields[f"{section}.{k.strip().lower()}"] = v.strip()

    def get(sec: str, key: str) -> str:
        return fields.get(f"{sec}.{key}", "")

    priv = get("interface", "privatekey")
    ip = get("interface", "address")
    pub = get("peer", "publickey")
    endpoint = get("peer", "endpoint")
    allowed = get("peer", "allowedips")
    keepalive = get("peer", "persistentkeepalive")

    if not (priv and pub and endpoint and ip):
        raise ValueError(
            "WireGuard .conf missing required fields "
            "(PrivateKey, Address, PublicKey, Endpoint)"
        )
    host, _, port = endpoint.rpartition(":")
    proxy: dict[str, Any] = {
        "name": f"wireguard-{host}" if host else "wireguard",
        "type": "wireguard",
        "server": host,
        "port": int(port) if port.isdigit() else 51820,
        "ip": ip.split("/")[0],
        "private-key": priv,
        "public-key": pub,
        "allowed-ips": [a.strip() for a in allowed.split(",") if a.strip()]
        or ["0.0.0.0/0"],
    }
    if keepalive.isdigit():
        proxy["persistent-keepalive"] = int(keepalive)
    return [proxy]


def _parse_vless(text: str) -> list[dict[str, Any]]:
    """Parse one or more vless:// URIs into Mihomo vless proxies."""
    proxies: list[dict[str, Any]] = []
    for i, line in enumerate(l for l in (x.strip() for x in text.splitlines()) if l):
        if not line.startswith("vless://"):
            continue
        from urllib.parse import urlparse, parse_qs

        u = urlparse(line)
        server = u.hostname
        port = u.port or 443
        uuid = u.username or ""
        q = parse_qs(u.query)
        first = lambda k, d="": q.get(k, [d])[0]  # noqa: E731
        p: dict[str, Any] = {
            "name": f"vless-{i + 1}",
            "type": "vless",
            "server": server,
            "port": port,
            "uuid": uuid,
        }
        if first("encryption", "none") != "none":
            p["encryption"] = first("encryption")
        if first("flow"):
            p["flow"] = first("flow")
        sec = first("security", "tls")
        p["tls"] = sec in ("tls", "reality")
        if sec == "reality":
            p["reality-opts"] = {
                "public-key": first("pbk"),
                "fingerprint": first("fp", "chrome"),
                "server-name": first("sni") or server,
                "spider-x": "1",
            }
        elif p["tls"]:
            p["servername"] = first("sni") or server
            if first("allow-insecure") == "1":
                p["skip-cert-verify"] = True
        if first("network") and first("network") != "tcp":
            p["network"] = first("network")
        proxies.append(p)
    if not proxies:
        raise ValueError("no vless:// URIs found")
    return proxies


_PARSERS: dict[str, Callable[[str], list[dict[str, Any]]]] = {
    "clash": _parse_clash_yaml,
    "base64": _parse_base64,
    "wg-conf": _parse_wg_conf,
    "vless": _parse_vless,
}


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------
def normalize_proxies(
    proxies: list[dict[str, Any]],
    prefix: str | None = None,
    name_map: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Sanitize + prefix proxy names. ``name_map`` is a provider-specific
    display-name -> stable-id transform (optional).

    Raises ``ValueError`` on a duplicate normalized node name. Mihomo does NOT
    refuse to start on duplicate node names -- it silently loads them, which is
    a routing hazard (two nodes, one selectable name). The adapter therefore
    rejects the collision up front (spec A2 "node collisions").
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in proxies:
        p = dict(p)
        raw = str(p.get("name", "node"))
        base = name_map(raw) if name_map else raw
        name = _apply_prefix(_safe_name(base), prefix)
        if name in seen:
            raise ValueError(f"duplicate node name {name!r} in provider subscription")
        seen.add(name)
        p["name"] = name
        out.append(p)
    return out


def normalize_source(
    text: str,
    prefix: str | None = None,
    name_map: Callable[[str], str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Detect format, parse, normalize, and emit the provider YAML text.

    Returns ``(yaml_text, meta)`` where meta has ``format`` and ``nodes``
    (redacted -- never contains secret values).
    """
    fmt = detect_format(text)
    proxies = _PARSERS[fmt](text)
    proxies = normalize_proxies(proxies, prefix, name_map)
    if not proxies:
        raise ValueError("subscription produced zero proxies")
    yaml_text = yaml.safe_dump({"proxies": proxies}, sort_keys=False)
    meta = {
        "format": fmt,
        "count": len(proxies),
        "nodes": [p["name"] for p in proxies],
    }
    return yaml_text, meta


# ---------------------------------------------------------------------------
# atomic write + last-known-good
# ---------------------------------------------------------------------------
def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write raw ``data`` bytes to ``path`` (tmp + rename).

    On success, also write ``<path>.lkg`` (last-known-good) so a failed future
    update can be rolled back. The LKG copy is only refreshed after a
    successful write, so it always holds the last working artifact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        lkg = path.with_name(path.name + ".lkg")
        with open(lkg, "wb") as f:
            f.write(data)
        os.chmod(lkg, 0o600)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def atomic_write(path: Path, data: str) -> None:
    """Atomically write UTF-8 text to ``path`` (see :func:`atomic_write_bytes`)."""
    _atomic_write_bytes(Path(path), data.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write binary ``data`` to ``path`` (GEO datasets are binary)."""
    _atomic_write_bytes(Path(path), data)


# ---------------------------------------------------------------------------
# provider registry
# ---------------------------------------------------------------------------
class ProviderAdapter:
    """Base adapter. Subclasses set ``provider`` and may override
    ``name_map`` for provider-specific display-name normalization.

    The adapter produces **clean, stable node names** (no provider tag). The
    provider tag is applied by Mihomo's ``override.additional-prefix`` in the
    provider config (spec 11.1), which is uniform for native (http) and
    adapter (file) providers. ``default_prefix`` documents the recommended
    ``additional-prefix`` value for this provider; it is NOT baked into the
    node names (doing so would double-prefix when combined with
    ``additional-prefix``).
    """

    provider = "generic"
    # Recommended ``additional-prefix`` for the provider config (documentation
    # only; the adapter does not apply it to node names).
    default_prefix: str | None = None

    def name_map(self, display: str) -> str:
        return display

    def normalize(self, text: str, prefix: str | None = None) -> tuple[str, dict]:
        # ``prefix`` is an explicit opt-in (standalone use). In the stack the
        # tag comes from additional-prefix, so this stays None -> clean names.
        return normalize_source(text, prefix=prefix, name_map=self.name_map)


def _registry() -> dict[str, type[ProviderAdapter]]:
    from . import blanc, xeovo  # local import: avoids a module-load cycle
    return {"blanc": blanc.BlancAdapter, "xeovo": xeovo.XeovoAdapter}


def get_adapter(provider: str) -> ProviderAdapter:
    reg = _registry()
    if provider not in reg:
        raise ValueError(f"unknown provider {provider!r} (known: {sorted(reg)})")
    return reg[provider]()


def run_adapter(provider: str, source_text: str, prefix: str | None = None) -> tuple[str, dict]:
    """Normalize ``source_text`` for ``provider``. Returns (yaml_text, meta).

    ``prefix`` is applied to node names only when explicitly given. In the
    stack the provider tag is supplied by the provider config's
    ``override.additional-prefix`` (spec 11.1), so the default is to produce
    clean names -- passing ``prefix=adapter.default_prefix`` is a standalone
    convenience, not the stack path.
    """
    adapter = get_adapter(provider)
    return adapter.normalize(source_text, prefix=prefix)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="egress-adapter",
        description="Normalize a provider subscription into Mihomo provider YAML.",
    )
    ap.add_argument("--provider", required=True, help="provider name (blanc, xeovo)")
    ap.add_argument("--source", required=True, help="raw subscription file (or - for stdin)")
    ap.add_argument("--out", required=True, help="output provider YAML path")
    ap.add_argument("--prefix", default=None, help="node name prefix (default per provider)")
    args = ap.parse_args(argv)

    if args.source == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.source).read_text()
    yaml_text, meta = run_adapter(args.provider, text, prefix=args.prefix)
    atomic_write(Path(args.out), yaml_text)
    # meta is already redacted (only names, no secret values)
    print(json.dumps({"ok": True, "provider": args.provider,
                      "out": str(args.out), "meta": meta}, default=str))


if __name__ == "__main__":
    main()
