"""Backend-managed provider deletion must be recoverable on reconcile errors."""
from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
TOOL = ROOT_DIR / "host" / "egress-providerctl"


def load_providerctl(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("egress_providerctl_test", str(TOOL))
    spec = importlib.util.spec_from_loader("egress_providerctl_test", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "var" / "lib" / "egress"
    secrets = tmp_path / "etc" / "egress" / "secrets"
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "SECRETS", secrets)
    return module, root, secrets


def create_provider_tree(module, root, secrets, provider="demo"):
    paths = (
        secrets / "providers" / f"{provider}.json",
        secrets / "providers" / f"{provider}.source",
        root / "providers" / f"{provider}.yaml",
        root / "providers" / f"{provider}.yaml.lkg",
        root / "egressd" / "provider-status" / f"{provider}.json",
    )
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"provider-file-{index}")
    return paths


@pytest.mark.parametrize("failure", [RuntimeError("reload failed"), SystemExit("reconcile failed")])
def test_delete_source_restores_every_file_when_reconcile_fails(
        tmp_path, monkeypatch, failure):
    module, root, secrets = load_providerctl(tmp_path, monkeypatch)
    paths = create_provider_tree(module, root, secrets)
    monkeypatch.setattr(module, "api", lambda *_args: (_ for _ in ()).throw(failure))
    args = module.argparse.Namespace(id="demo")

    with pytest.raises(type(failure)):
        module.delete_source(args)

    assert [path.read_text() for path in paths] == [
        "provider-file-0", "provider-file-1", "provider-file-2",
        "provider-file-3", "provider-file-4",
    ]


def test_delete_source_removes_all_provider_files_after_success(
        tmp_path, monkeypatch, capsys):
    module, root, secrets = load_providerctl(tmp_path, monkeypatch)
    paths = create_provider_tree(module, root, secrets)
    calls = []
    monkeypatch.setattr(module, "api", lambda method, path: calls.append((method, path)) or {})

    module.delete_source(module.argparse.Namespace(id="demo"))

    assert calls == [("POST", "/v1/reconcile")]
    assert all(not path.exists() for path in paths)
    assert '"removed": true' in capsys.readouterr().out
