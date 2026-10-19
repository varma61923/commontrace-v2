from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("transport", ["tar", "rsync"])
def test_installer_copies_source_without_generated_caches_or_deployment_state(tmp_path, transport):
    if transport == "rsync" and not shutil.which("rsync"):
        pytest.skip("rsync transport requires the native executable")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("installer requires bash")
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "install.sh", source / "install.sh")
    generated = (
        "deploy/terraform/aws/.terraform/providers/binary", "deploy/terraform/aws/private.tfstate",
        "deploy/terraform/aws/private.tfstate.backup", "deploy/terraform/aws/private.tfvars",
        "deploy/terraform/aws/private.tfplan", "distribution/jetbrains/build/artifact.jar",
        "distribution/jetbrains/.gradle/cache", "distribution/jetbrains/.intellijPlatform/cache",
        ".mypy_cache/cache", ".pytest_cache/cache", ".ruff_cache/cache", "node_modules/package/index.js",
        "sdk/generated/typescript/client.ts", "sdk/rust/target/binary",
    )
    retained = ("README.md", "commontrace/runtime.py", "deploy/terraform/aws/main.tf",
                "deploy/terraform/aws/example.tfvars.example", "deploy/terraform/aws/.terraform.lock.hcl",
                "distribution/jetbrains/src/source.kt")
    for name in generated + retained:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture data")
    # Force each real copy implementation without invoking a fake rsync.
    tools = tmp_path / "tools"
    tools.mkdir()
    commands = ("dirname", "mkdir", "tar", "rm", "find", "wc", "tr", "cat")
    for name in commands + (("rsync",) if transport == "rsync" else ()):
        executable = shutil.which(name)
        if executable is None:
            pytest.skip("installer contract requires " + name)
        (tools / name).symlink_to(executable)
    dest = tmp_path / "installed"
    process = subprocess.run([bash, str(source / "install.sh"), "--dest", str(dest),
                              "--python", sys.executable, "--no-deps", "--no-index"],
                             env={**os.environ, "PATH": str(tools)}, text=True, capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert all((dest / name).is_file() for name in retained)
    assert all(not (dest / name).exists() for name in generated)


def test_tar_installer_preserves_existing_destination_data_and_handles_no_lessons(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("installer requires bash")
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "install.sh", source / "install.sh")
    (source / "README.md").write_text("source")
    (source / ".env").write_text("source-secret")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("dirname", "mkdir", "tar", "find", "wc", "tr", "cat", "rm"):
        executable = shutil.which(name)
        if executable is None:
            pytest.skip("installer contract requires " + name)
        (tools / name).symlink_to(executable)
    dest = tmp_path / "installed"
    preserved = {".git/HEAD": "existing git", ".env": "destination secret", ".env.production": "production secret",
                 "memory/attention/index.npz": "existing index", "cache/__pycache__/cache.pyc": "existing cache"}
    for name, text in preserved.items():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    process = subprocess.run([bash, str(source / "install.sh"), "--dest", str(dest), "--python", sys.executable,
                              "--no-deps"], env={**os.environ, "PATH": str(tools)},
                             text=True, capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert "No active lessons" in process.stdout
    assert all((dest / name).read_text() == text for name, text in preserved.items())
    assert (dest / "README.md").read_text() == "source"


@pytest.mark.parametrize("destination", [".", "nested"])
def test_installer_rejects_copying_into_its_own_source_tree(tmp_path, destination):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("installer requires bash")
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "install.sh", source / "install.sh")
    process = subprocess.run([bash, str(source / "install.sh"), "--dest", str(source / destination),
                              "--no-deps", "--no-index"], text=True, capture_output=True, timeout=30)
    assert process.returncode != 0
    assert "--in-place" in process.stderr
