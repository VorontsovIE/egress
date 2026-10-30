"""Provider deletion stages backups on the source filesystem."""
from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
TOOL = ROOT_DIR / "host" / "egress-providerctl"


def setup_tool(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader("providerctl_exdev_test", str(TOOL))
    spec = importlib.util.spec_from_loader("providerctl_exdev_test", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "var" / "lib" / "egress"
    secrets = tmp_path / "etc" / "egress" / "secrets"
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "SECRETS", secrets)
    paths = [
        secrets / "providers" / "alpha.json",
        secrets / "providers" / "alpha.source",
        root / "providers" / "alpha.yaml",
        root / "providers" / "alpha.yaml.lkg",
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"item-{index}")
    real_replace = module.os.replace
    calls = []
    etc_root = tmp_path / "etc" / "egress"
    var_root = tmp_path / "var" / "lib" / "egress"

    def same_filesystem_replace(source, destination):
        source, destination = Path(source), Path(destination)
        source_in_etc = source == etc_root or etc_root in source.parents
        destination_in_var = destination == var_root or var_root in destination.parents
        source_in_var = source == var_root or var_root in source.parents
        destination_in_etc = destination == etc_root or etc_root in destination.parents
        if (source_in_etc and destination_in_var) or (source_in_var and destination_in_etc):
            raise OSError(errno.EXDEV, "simulated cross-filesystem rename")
        calls.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", same_filesystem_replace)
    return module, paths, calls


@pytest.mark.parametrize("reconcile_fails", [False, True])
def test_provider_delete_never_renames_between_etc_and_var_lib(
        tmp_path, monkeypatch, reconcile_fails):
    module, paths, replaces = setup_tool(tmp_path, monkeypatch)
    if reconcile_fails:
        monkeypatch.setattr(module, "api",
                            lambda *_args: (_ for _ in ()).throw(RuntimeError("reconcile failed")))
    else:
        monkeypatch.setattr(module, "api", lambda *_args: {})

    if reconcile_fails:
        with pytest.raises(RuntimeError, match="reconcile failed"):
            module.delete_source(module.argparse.Namespace(id="alpha"))
        assert all(path.read_text() == f"item-{index}" for index, path in enumerate(paths))
    else:
        module.delete_source(module.argparse.Namespace(id="alpha"))
        assert all(not path.exists() for path in paths)

    assert replaces
    etc_root = tmp_path / "etc" / "egress"
    var_root = tmp_path / "var" / "lib" / "egress"
    assert all(not ((src == etc_root or etc_root in src.parents) and
                    (dst == var_root or var_root in dst.parents))
               and not ((src == var_root or var_root in src.parents) and
                        (dst == etc_root or etc_root in dst.parents))
               for src, dst in replaces)
