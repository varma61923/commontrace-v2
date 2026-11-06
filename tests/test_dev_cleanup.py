"""Cleanup must never widen a cache match into removal of local user state."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.mypy_cache/\n.pytest_cache/\n.ruff_cache/\nbuild/\ndist/\n"
        ".coverage*\ncoverage.xml\n.venv/\nnode_modules/\n*.egg-info/\n.env\nmemory/\n",
        encoding="utf-8",
    )
    tracked = tmp_path / "dist" / "source.txt"
    tracked.parent.mkdir()
    tracked.write_text("tracked source", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".gitignore", "dist/source.txt"], cwd=tmp_path, check=True)
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    return tmp_path


def populate(root: Path, names: tuple[str, ...]) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve or regenerate", encoding="utf-8")


def test_clean_removes_only_ignored_outputs_and_keeps_local_state(checkout):
    generated = (
        ".mypy_cache/cache",
        ".pytest_cache/cache",
        ".ruff_cache/cache",
        "build/temp.o",
        "dist/wheel.whl",
        "sdk/typescript/dist/client.js",
        "__pycache__/conftest.pyc",
        "unimem/nested/__pycache__/module.pyc",
        "unimem/old.pyc",
        ".coverage.worker",
        "coverage.xml",
    )
    retained = (
        "dist/source.txt",  # Even an ignored output directory may contain tracked files.
        ".env",
        ".venv/lib/__pycache__/dependency.pyc",
        "sdk/typescript/node_modules/pkg/__pycache__/dependency.pyc",
        "unimem.egg-info/PKG-INFO",
        "memory/.cache/index",
        "memory/facts.jsonl",
        "memory/__pycache__/private.pyc",
        "unimem/untracked_source.py",
    )
    populate(checkout, generated + retained)
    assert dev.clean() == 0
    assert all(not (checkout / name).exists() for name in generated)
    assert all((checkout / name).is_file() for name in retained)
    assert dev.clean() == 0
    assert all((checkout / name).is_file() for name in retained)


def test_clean_preview_and_symlinks_do_not_modify_external_data(checkout, tmp_path_factory):
    generated = (".pytest_cache/cache", "unimem/__pycache__/module.pyc")
    populate(checkout, generated)
    external = tmp_path_factory.mktemp("outside-cleanup")
    (external / "keep.txt").write_text("external data", encoding="utf-8")
    (checkout / ".ruff_cache").symlink_to(external, target_is_directory=True)
    assert dev.clean(dry_run=True) == 0
    assert all((checkout / name).is_file() for name in generated)
    assert dev.clean() == 0
    assert (external / "keep.txt").read_text(encoding="utf-8") == "external data"


def test_clean_cli_rejects_extra_paths(checkout, monkeypatch):
    populate(checkout, (".env",))
    monkeypatch.setattr(sys, "argv", ["dev.py", "clean", ".env"])
    with pytest.raises(SystemExit) as raised:
        dev.main()
    assert raised.value.code == 2
    assert (checkout / ".env").is_file()
