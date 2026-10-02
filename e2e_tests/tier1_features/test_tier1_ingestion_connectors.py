from __future__ import annotations

import json
from pathlib import Path

from commontrace import failure_import
from e2e_tests.harness.cli_runner import require_milestone, run_cli
from e2e_tests.harness.store_fixtures import (
    create_sample_code_repo,
    create_sample_logs,
    create_sample_markdown_docs,
    create_sample_transcripts,
)


def test_t1_ingest_code_ast_chunking(tmp_path: Path, isolated_store: str):
    """E2E-T1-ING-1: Ingest code repository into graph nodes and candidate lessons (M2)."""
    require_milestone("M2")
    repo_dir = create_sample_code_repo(tmp_path)
    res = run_cli("ingest", str(repo_dir), "--format", "code", dest=isolated_store)
    res.assert_success()


def test_t1_ingest_markdown_hierarchical_headers(tmp_path: Path, isolated_store: str):
    """E2E-T1-ING-2: Ingest Markdown documentation with hierarchical header chunking (M2)."""
    require_milestone("M2")
    docs_dir = create_sample_markdown_docs(tmp_path)
    res = run_cli("ingest", str(docs_dir), "--format", "markdown", dest=isolated_store)
    res.assert_success()


def test_t1_ingest_json_logs_clustering(tmp_path: Path, isolated_store: str):
    """E2E-T1-ING-3: Ingest JSON structured logs and cluster error fingerprints (M2)."""
    require_milestone("M2")
    log_file = create_sample_logs(tmp_path)
    res = run_cli("ingest", str(log_file), "--format", "logs", dest=isolated_store)
    res.assert_success()


def test_t1_ingest_failure_transcripts(tmp_path: Path, isolated_store: str):
    """E2E-T1-ING-4: Ingest multi-turn failure transcripts into traces and review lessons (M2)."""
    require_milestone("M2")
    t_file = create_sample_transcripts(tmp_path)
    res = run_cli("ingest", str(t_file), "--format", "transcript", dest=isolated_store)
    res.assert_success()


def test_t1_ingest_failure_records_existing(tmp_path: Path):
    """E2E-T1-ING-5: Ingest failure records via failure_import module (progressive baseline)."""
    fail_file = tmp_path / "failures.jsonl"
    records = [
        {"text": "Postgres connection timeout after 5000ms", "label": "db_timeout"},
        {"text": "Redis connection refused on port 6379", "label": "cache_err"},
    ]
    with open(fail_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    imported, report = failure_import.read_failures(str(fail_file))
    assert len(imported) == 2
    assert "Postgres" in imported[0]["label"] or "Postgres" in imported[0]["text"]
