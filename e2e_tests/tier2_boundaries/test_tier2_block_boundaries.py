from __future__ import annotations

import pytest

from commontrace import memory_blocks
from e2e_tests.harness.cli_runner import run_cli


def test_t2_block_quota_ceiling_overflow(isolated_store: str):
    """E2E-T2-MB-1: Attempting to write content exceeding max_chars quota must be rejected."""
    # Attempt via API
    with pytest.raises(memory_blocks.QuotaExceededError):
        memory_blocks.set_block(
            isolated_store,
            "strict_quota",
            "A" * 501,
            max_chars=500,
        )

    # Attempt via CLI
    res = run_cli(
        "block", "set", "strict_quota", "B" * 600,
        "--max-chars", "500",
        dest=isolated_store,
    )
    res.assert_failure()


def test_t2_block_ambiguous_substring_replacement(isolated_store: str):
    """E2E-T2-MB-2: Substring replacement fails when target string occurs multiple times."""
    content = "The service retry count is 3. Later retry count is 3."
    memory_blocks.set_block(isolated_store, "ambiguous", content)

    with pytest.raises(memory_blocks.MemoryBlockError, match="Ambiguous"):
        memory_blocks.replace_block(
            isolated_store, "ambiguous",
            "retry count is 3", "retry count is 5",
        )

    # CLI should also exit non-zero
    res = run_cli(
        "block", "replace", "ambiguous",
        "--old", "retry count is 3",
        "--new", "retry count is 5",
        dest=isolated_store,
    )
    res.assert_failure()


def test_t2_block_nonexistent_substring_replacement(isolated_store: str):
    """E2E-T2-MB-3: Substring replacement fails when target string is not in the block."""
    memory_blocks.set_block(isolated_store, "missing_target", "Only alpha and beta.")

    with pytest.raises(memory_blocks.SubstringNotFoundError):
        memory_blocks.replace_block(
            isolated_store, "missing_target",
            "gamma", "delta",
        )

    res = run_cli(
        "block", "replace", "missing_target",
        "--old", "gamma",
        "--new", "delta",
        dest=isolated_store,
    )
    res.assert_failure()


def test_t2_block_name_sanitization_path_traversal(isolated_store: str):
    """E2E-T2-MB-4: Names with path traversal patterns (e.g. ../../etc/passwd) must be sanitized."""
    malicious_name = "../../etc/passwd"
    b = memory_blocks.set_block(isolated_store, malicious_name, "Root user test")
    # Sanitized name must not contain directory separators
    assert "/" not in b.name
    assert "\\" not in b.name
    assert ".." not in b.name


def test_t2_block_empty_name_rejection(isolated_store: str):
    """E2E-T2-MB-5: Empty or whitespace-only block names must be rejected."""
    with pytest.raises((ValueError, memory_blocks.MemoryBlockError)):
        memory_blocks.set_block(isolated_store, "   ", "Invalid empty block name")
