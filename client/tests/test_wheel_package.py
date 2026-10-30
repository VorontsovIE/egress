"""Packaging acceptance checks for the client wheel.

The wheel must be self-contained: client commands and the adapters imported
by personal subscription normalization must work outside the source tree.
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "client"


def test_wheel_contains_client_adapters_and_console_scripts(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", str(CLIENT), "--out-dir", str(wheelhouse)],
        check=True,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wheels = list(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as package:
        names = set(package.namelist())
    assert any(name.startswith("egress_client/") for name in names)
    assert any(name.startswith("adapters/") for name in names)
    entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
    with zipfile.ZipFile(wheels[0]) as package:
        scripts = package.read(entry_points).decode()
    assert "egressctl = egress_client.egressctl:main" in scripts
    assert "egress-run = egress_client.egress_run:main" in scripts


def test_installed_wheel_imports_without_checkout(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    subprocess.run(
        ["uv", "build", "--wheel", str(CLIENT), "--out-dir", str(wheelhouse)],
        check=True,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    wheels = list(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1
    env = os.environ.copy()
    env["PYTHONPATH"] = str(wheels[0])
    env.pop("PYTHONHOME", None)
    result = subprocess.run(
        [sys.executable, "-c",
         "import egress_client, adapters.common; "
         "from egress_client.egressctl import build_parser; "
         "assert build_parser().prog == 'egressctl'; "
         "print(egress_client.__version__)"],
        check=True,
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.stdout.strip()
