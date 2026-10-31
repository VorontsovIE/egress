import importlib.util
from importlib.machinery import SourceFileLoader
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "host" / "egress-host-test"


def _load_harness(monkeypatch):
    monkeypatch.syspath_prepend(str(HARNESS.parent))
    loader = SourceFileLoader("egress_host_test", str(HARNESS))
    spec = importlib.util.spec_from_loader("egress_host_test", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_harness_imports_client_from_wheel_venv(tmp_path, monkeypatch):
    lib = tmp_path / "lib" / "egress"
    venv = lib / "current" / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                   check=True)
    site_packages = venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    package = site_packages / "egress_client"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "egressctl.py").write_text("VALUE = True\n")

    harness = _load_harness(monkeypatch)
    monkeypatch.setattr(harness, "CLIENT_LIB_DIR", str(lib))
    results = []
    monkeypatch.setattr(harness, "add", lambda *args, **kwargs: results.append(args))
    invoked = []
    real_run = subprocess.run

    def record_run(command, *args, **kwargs):
        invoked.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", record_run)

    harness.check_client_package_importable(live=True)

    assert results[0][0:2] == ("client_package_importable", "pass")
    assert str(venv / "bin" / "python") in results[0][2]
    assert invoked[0][0] == str(venv / "bin" / "python")


def test_live_harness_imports_explicit_source_layout(tmp_path, monkeypatch):
    lib = tmp_path / "source-lib"
    package = lib / "egress_client"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "egressctl.py").write_text("VALUE = True\n")

    harness = _load_harness(monkeypatch)
    monkeypatch.setattr(harness, "CLIENT_LIB_DIR", str(lib))
    results = []
    monkeypatch.setattr(harness, "add", lambda *args, **kwargs: results.append(args))
    invoked = []
    real_run = subprocess.run

    def record_run(command, *args, **kwargs):
        invoked.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", record_run)

    harness.check_client_package_importable(live=True)

    assert results[0][0:2] == ("client_package_importable", "pass")
    assert invoked[0][0] == "python3"
    assert str(lib) in invoked[0][2]


def test_live_harness_fails_when_prefix_has_no_client_package(tmp_path, monkeypatch):
    harness = _load_harness(monkeypatch)
    lib = tmp_path / "empty-lib"
    lib.mkdir()
    monkeypatch.setattr(harness, "CLIENT_LIB_DIR", str(lib))
    results = []
    monkeypatch.setattr(harness, "add", lambda *args, **kwargs: results.append(args))

    harness.check_client_package_importable(live=True)

    assert results[0][0:2] == ("client_package_importable", "fail")
    assert "not found" in results[0][2]
