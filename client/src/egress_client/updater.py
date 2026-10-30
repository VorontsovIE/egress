"""Install the current user's client and TUN engine from a signed release set.

This module never performs privileged operations. The backend updater is a
separate root-owned executable installed by the backend installer.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import venv
from pathlib import Path

REPO = "VorontsovIE/egress"
API = f"https://api.github.com/repos/{REPO}/releases"


def _read(url: str, limit: int = 64 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                "User-Agent": "egress-updater"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("release asset exceeds size limit")
    return data


def _release(version: str | None) -> dict:
    url = f"{API}/latest" if version is None else f"{API}/tags/{version}"
    data = json.loads(_read(url, 2 * 1024 * 1024))
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v") or data.get("draft"):
        raise RuntimeError("no published stable egress release found")
    return data


def _asset(release: dict, filename: str) -> bytes:
    for asset in release.get("assets", []):
        if asset.get("name") == filename and isinstance(asset.get("browser_download_url"), str):
            return _read(asset["browser_download_url"])
    raise RuntimeError(f"release asset missing: {filename}")


def _verify(data: bytes, name: str, checksums: bytes) -> None:
    expected = None
    for line in checksums.decode("ascii").splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[1].lstrip("* ") == name:
            expected = fields[0]
            break
    if expected is None or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f"checksum verification failed: {name}")


def _install(release: dict) -> None:
    tag = release["tag_name"]
    version = tag[1:]
    arch = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)
    if arch not in ("amd64", "arm64") or sys.platform != "linux":
        raise RuntimeError("client release supports Linux amd64 and arm64 only")
    wheel_name = f"egress_client-{version}-py3-none-any.whl"
    engine_name = f"egress-tun-engine-linux-{arch}"
    checksum_data = _asset(release, "SHA256SUMS")
    wheel_data, engine_data = _asset(release, wheel_name), _asset(release, engine_name)
    _verify(wheel_data, wheel_name, checksum_data)
    _verify(engine_data, engine_name, checksum_data)

    home = Path.home()
    base = home / ".local" / "share" / "egress"
    versions = base / "versions"
    versions.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = versions / version
    if target.exists():
        raise RuntimeError(f"version already installed: {tag}")
    staging = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=versions))
    try:
        wheel_path = staging / wheel_name
        wheel_path.write_bytes(wheel_data)
        engine_path = staging / "egress-tun-engine"
        engine_path.write_bytes(engine_data)
        engine_path.chmod(0o755)
        env = staging / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(env)
        subprocess.run([str(env / "bin/python"), "-m", "pip", "install",
                        "--no-deps", "--no-index", str(wheel_path)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        target.mkdir(mode=0o700)
        shutil.move(str(env), target / "venv")
        shutil.move(str(engine_path), target / "egress-tun-engine")
        current_tmp = base / ".current-new"
        current_tmp.unlink(missing_ok=True)
        current_tmp.symlink_to(Path("versions") / version)
        os.replace(current_tmp, base / "current")
        _install_wrappers(home)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    print(f"client and TUN engine updated to {tag}; restart your shell if PATH needs updating")


def _install_wrappers(home: Path) -> None:
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for command, module in (("egressctl", "egressctl"), ("egress-run", "egress_run")):
        path = bindir / command
        body = ("#!/bin/sh\n"
                "EGRESS_HOME=\"${HOME}/.local/share/egress/current\"\n"
                "export EGRESS_TUN_ENGINE=\"$EGRESS_HOME/egress-tun-engine\"\n"
                f"exec \"$EGRESS_HOME/venv/bin/python\" -m egress_client.{module} \"$@\"\n")
        tmp = path.with_name(f".{command}.new")
        tmp.write_text(body)
        tmp.chmod(0o755)
        os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="egress-client-upgrade")
    parser.add_argument("--version")
    args = parser.parse_args(argv)
    try:
        _install(_release(args.version))
    except (OSError, ValueError, urllib.error.URLError, RuntimeError,
            subprocess.CalledProcessError) as exc:
        print(f"egress upgrade failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
