from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import memory_blocks

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def test_block_lifecycle(store):
    # Set persona block
    b = memory_blocks.set_block(
        store, "persona", "You are an autonomous staff software architect.",
        max_chars=500, actor="test", reason="init persona",
    )
    assert b.name == "persona"
    assert b.char_count == len("You are an autonomous staff software architect.")
    assert len(b.revision) == 16

    # Append
    b2 = memory_blocks.append_block(
        store, "persona", "Always verify diffs before committing.",
        actor="test", reason="add rule",
    )
    assert "verify diffs" in b2.content
    assert b2.revision != b.revision

    # Replace
    b3 = memory_blocks.replace_block(
        store, "persona", "Always verify diffs", "Thoroughly verify diffs",
        actor="test", reason="refine wording",
    )
    assert "Thoroughly verify diffs" in b3.content

    # List
    all_blocks = memory_blocks.list_blocks(store)
    assert len(all_blocks) == 1
    assert all_blocks[0].name == "persona"

    # History
    history = memory_blocks.block_history(store, "persona")
    assert len(history) == 3
    assert history[0]["action"] == "set"
    assert history[1]["action"] == "set"  # append uses set_block internally
    assert history[2]["action"] == "set"  # replace uses set_block internally


def test_block_quota_enforced(store):
    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.set_block(
            store, "strict", "x" * 150, max_chars=100,
        )


def test_block_cli_commands(store):
    res = cli("block", "set", "human", "User prefers TypeScript and strict typing.", "--dest", store)
    assert res.returncode == 0, res.stderr
    assert "Saved block 'human'" in res.stdout

    res = cli("block", "get", "human", "--dest", store)
    assert res.returncode == 0
    assert "TypeScript" in res.stdout

    res = cli("block", "append", "human", "Prefers functional programming.", "--dest", store)
    assert res.returncode == 0
    assert "Appended to block 'human'" in res.stdout

    res = cli("block", "list", "--dest", store)
    assert res.returncode == 0
    assert "human" in res.stdout

    res = cli("block", "history", "human", "--dest", store)
    assert res.returncode == 0
    assert "human" in res.stdout


def test_block_delete_lifecycle_and_history(store):
    b = memory_blocks.set_block(store, "scratchpad", "Initial thoughts.")
    orig_rev = b.revision
    assert memory_blocks.delete_block(store, "scratchpad", actor="test-worker", reason="cleanup") is True
    with pytest.raises(memory_blocks.BlockNotFoundError):
        memory_blocks.get_block(store, "scratchpad")
    assert not any(block.name == "scratchpad" for block in memory_blocks.list_blocks(store))
    assert memory_blocks.delete_block(store, "scratchpad") is False

    hist = memory_blocks.block_history(store, "scratchpad")
    assert len(hist) == 2
    assert hist[0]["action"] == "set"
    assert hist[1]["action"] == "delete"
    assert hist[1]["actor"] == "test-worker"
    assert hist[1]["reason"] == "cleanup"
    assert hist[1]["prev_revision"] == orig_rev
    assert len(hist[1]["revision"]) == 16
    assert hist[1]["char_count"] == 0


def test_block_atomic_file_writes(store, monkeypatch):
    memory_blocks.set_block(store, "atomic_test", "Safe content.")
    blocks_dir = os.path.join(store, "memory", "blocks")
    assert not any(f.endswith(".tmp") for f in os.listdir(blocks_dir))

    # Test failure during update leaves original uncorrupted
    original_replace = os.replace

    def broken_replace(src, dst):
        if "atomic_test.meta.json" in dst:
            raise OSError("Simulated disk error during metadata rename")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        memory_blocks.set_block(store, "atomic_test", "New unsafe content.")

    # Original content should still be intact
    monkeypatch.setattr(os, "replace", original_replace)
    block_restored = memory_blocks.get_block(store, "atomic_test")
    assert block_restored.content == "Safe content."
    assert not any(f.endswith(".tmp") for f in os.listdir(blocks_dir))


def test_block_cli_delete(store):
    res = cli("block", "set", "temporary", "To be removed", "--dest", store)
    assert res.returncode == 0
    assert "Saved block 'temporary'" in res.stdout

    res = cli("block", "delete", "temporary", "--dest", store)
    assert res.returncode == 0
    assert "Deleted block 'temporary'" in res.stdout

    res = cli("block", "get", "temporary", "--dest", store)
    assert res.returncode == 1
    assert "does not exist" in res.stderr

    res = cli("block", "history", "temporary", "--dest", store)
    assert res.returncode == 0
    assert "delete" in res.stdout

    res = cli("block", "delete", "temporary", "--dest", store)
    assert res.returncode == 1
    assert "Block 'temporary' not found" in res.stderr


def test_memory_block_schema_validation(store):
    from commontrace import validate

    schema = validate.load_schema("memory_block.schema.json")
    validate.assert_supported_schema(schema)

    b = memory_blocks.set_block(store, "schemablock", "Valid markdown content.")
    errs = validate.validate(b.to_dict(), schema)
    assert errs == []

    bad_dict = b.to_dict()
    bad_dict["char_count"] = -1
    assert len(validate.validate(bad_dict, schema)) > 0
