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
