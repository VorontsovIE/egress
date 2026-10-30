"""Both release updaters retain the previous client on post-switch failure."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_root_updater(monkeypatch, tmp_path):
    helper = ROOT / "host" / "egress-upgrade"
    loader = importlib.machinery.SourceFileLoader("egress_upgrade_test", str(helper))
    spec = importlib.util.spec_from_loader("egress_upgrade_test", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    real_path = Path
    prefix = "/usr/local/lib/egress"
    destination = tmp_path / "usr" / "local" / "lib" / "egress"

    def mapped_path(value):
        raw = str(value)
        if raw == prefix or raw.startswith(prefix + "/"):
            return real_path(str(destination) + raw[len(prefix):])
        return real_path(value)

    monkeypatch.setattr(module, "Path", mapped_path)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    return module, destination


def test_system_updater_rolls_client_pointer_back_if_backend_deploy_fails(
        monkeypatch, tmp_path):
    module, install_root = load_root_updater(monkeypatch, tmp_path)
    current = install_root / "current"
    (install_root / "versions" / "1.0.0").mkdir(parents=True)
    (install_root / "versions" / "2.0.0").mkdir()
    current.symlink_to(Path("versions") / "1.0.0")

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        data = b"services: {}\n"
        info = tarfile.TarInfo("egress-2.0.0/compose.yaml")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    bundle = output.getvalue()
    monkeypatch.setattr(module, "release", lambda _tag: {"tag_name": "v2.0.0"})
    monkeypatch.setattr(module, "asset",
                        lambda _release, name: bundle if name == "egress-2.0.0.tar.gz" else b"asset")
    monkeypatch.setattr(module, "verify", lambda *_args: None)
    monkeypatch.setattr(module, "verify_attestations", lambda _assets: None)

    def install_client(version, _wheel, _engine):
        temp = current.with_name(".current-new")
        temp.symlink_to(Path("versions") / version)
        os.replace(temp, current)

    monkeypatch.setattr(module, "install_system_client", install_client)
    monkeypatch.setattr(module, "deploy_backend",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("backend health failure")))
    monkeypatch.setattr(module.sys, "argv", ["egress-upgrade", "--version", "v2.0.0"])

    with pytest.raises(RuntimeError, match="backend health failure"):
        module.main()

    assert current.is_symlink()
    assert os.readlink(current) == "versions/1.0.0"


def test_user_updater_rolls_back_if_wrapper_install_fails_after_switch(
        monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "client" / "src"))
    from egress_client import updater

    home = tmp_path / "home" / "alice"
    base = home / ".local" / "share" / "egress"
    previous = base / "versions" / "1.0.0"
    previous.mkdir(parents=True)
    current = base / "current"
    current.symlink_to(Path("versions") / "1.0.0")
    monkeypatch.setattr(updater.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(updater.sys, "platform", "linux")
    monkeypatch.setattr(updater.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(updater, "_asset",
                        lambda _release, name: f"asset:{name}".encode())
    monkeypatch.setattr(updater, "_verify", lambda *_args: None)
    monkeypatch.setattr(updater, "_verify_attestation", lambda _path: None)

    class FakeVenvBuilder:
        def __init__(self, **_kwargs):
            pass

        def create(self, env):
            python = env / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)

    monkeypatch.setattr(updater.venv, "EnvBuilder", FakeVenvBuilder)
    monkeypatch.setattr(updater.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "_install_wrappers",
                        lambda _home: (_ for _ in ()).throw(OSError("wrapper write failure")))

    with pytest.raises(OSError, match="wrapper write failure"):
        updater._install({"tag_name": "v2.0.0"})

    assert current.is_symlink()
    assert os.readlink(current) == "versions/1.0.0"
