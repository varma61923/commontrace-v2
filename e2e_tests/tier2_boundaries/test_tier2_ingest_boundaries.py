from __future__ import annotations

from pathlib import Path

import pytest

from commontrace import failure_import, memory_guard


def test_t2_ingest_empty_file_rejection(tmp_path: Path):
    """E2E-T2-ING-1: Ingesting an empty file raises FailureImportError."""
    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("", encoding="utf-8")

    with pytest.raises(failure_import.FailureImportError, match="empty"):
        failure_import.read_failures(str(empty_file))


def test_t2_ingest_corrupt_jsonl_error_handling(tmp_path: Path):
    """E2E-T2-ING-2: Corrupt non-JSON lines in JSONL file raise clear parse errors."""
    corrupt_file = tmp_path / "corrupt.jsonl"
    corrupt_file.write_text("{\"title\": \"Valid line\"}\nNOT_VALID_JSON{{{\n", encoding="utf-8")

    with pytest.raises(failure_import.FailureImportError, match="not valid JSON"):
        failure_import.read_failures(str(corrupt_file), fmt_override="jsonl")


def test_t2_ingest_secret_key_redaction():
    """E2E-T2-ING-3: Ingested traces containing API keys and bearer tokens must be redacted."""
    raw_text = (
        "Encountered 401 when calling Anthropic using API key "
        "sk-ant-api03-abcdef1234567890abcdef1234567890 and AWS AKIAIOSFODNN7EXAMPLE"
    )
    redacted, found = memory_guard.redact_secrets(raw_text)

    assert "sk-ant-" not in redacted, "Anthropic API key must be scrubbed"
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted, "AWS key must be scrubbed"
    assert "[REDACTED" in redacted
    assert len(found) >= 2


def test_t2_ingest_unsupported_format_rejection(tmp_path: Path):
    """E2E-T2-ING-4: Ingestion with unsupported format override raises FailureImportError."""
    test_file = tmp_path / "test.data"
    test_file.write_text("some content", encoding="utf-8")

    with pytest.raises(failure_import.FailureImportError, match="unknown format"):
        failure_import.read_failures(str(test_file), fmt_override="unsupported_format_xyz")


def test_t2_ingest_duplicate_records_deduplication(tmp_path: Path):
    """E2E-T2-ING-5: Repeated identical failure traces are automatically deduplicated in stats."""
    dup_file = tmp_path / "duplicates.jsonl"
    record = "{\"title\": \"Database lock timeout\", \"text\": \"Lock timeout on accounts\"}\n"
    dup_file.write_text(record * 5, encoding="utf-8")

    failures, stats = failure_import.read_failures(str(dup_file))
    assert len(failures) == 1, "Must deduplicate exact identical records"
    assert stats["deduplicated"] == 4
