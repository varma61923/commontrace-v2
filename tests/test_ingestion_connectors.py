"""Tests for multimodal ingestion pipeline (M2)."""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture()
def store(tmp_path):
    """Initialise a minimal commontrace store."""
    store = tmp_path / "store"
    store.mkdir()
    from commontrace import paths
    os.makedirs(paths.lessons_dir(str(store)), exist_ok=True)
    os.makedirs(paths.traces_dir(str(store)), exist_ok=True)
    return str(store)


class TestCodeIngestion:
    def test_ingest_python_file(self, store, tmp_path):
        from commontrace.ingest import ingest_code_repository

        src = tmp_path / "mylib"
        src.mkdir()
        (src / "utils.py").write_text(
            '"""Utility functions."""\n\ndef add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n',
            encoding="utf-8",
        )

        result = ingest_code_repository(store, str(src), scope="myproject")
        assert result.chunks_extracted > 0
        assert result.graph_nodes_written > 0
        assert result.source_type == "code"

    def test_ingest_redacts_secrets(self, store, tmp_path):
        from commontrace.ingest import ingest_code_repository

        src = tmp_path / "secrets_proj"
        src.mkdir()
        (src / "config.py").write_text(
            'API_KEY = "sk-ant-api03-SECRETSECRETSECRETSECRET1234567890"\n',
            encoding="utf-8",
        )

        result = ingest_code_repository(store, str(src))
        # Verify no raw secret appears in any lesson file
        from commontrace import paths
        for fname in os.listdir(paths.lessons_dir(store)):
            if fname.endswith(".md"):
                content = open(os.path.join(paths.lessons_dir(store), fname)).read()
                assert "sk-ant-api03-" not in content, f"Secret found in {fname}"

    def test_ingest_invalid_python_does_not_crash(self, store, tmp_path):
        from commontrace.ingest import ingest_code_repository

        src = tmp_path / "bad_syntax"
        src.mkdir()
        (src / "broken.py").write_text("def foo(:\n    pass\n", encoding="utf-8")

        result = ingest_code_repository(store, str(src))
        # Should not raise, should handle gracefully
        assert result.source_type == "code"


class TestMarkdownIngestion:
    def test_ingest_markdown_extracts_facts(self, store, tmp_path):
        from commontrace import hierarchical
        from commontrace.ingest import ingest_markdown_documentation

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "constraints.md").write_text(
            "# Requirements\n\n## Constraint: max 100 connections\n\nThe database pool must not exceed 100 simultaneous connections.\n",
            encoding="utf-8",
        )

        result = ingest_markdown_documentation(store, str(docs), scope="infra")
        assert result.facts_written > 0
        assert result.source_type == "markdown"

        facts = hierarchical.list_facts(store)
        assert any("Constraint" in f.statement for f in facts), \
            f"Expected constraint fact, got: {[f.statement for f in facts]}"


class TestJsonLogIngestion:
    def test_ingest_json_logs_clusters_errors(self, store, tmp_path):
        from commontrace import paths
        from commontrace.ingest import ingest_json_logs

        log_file = tmp_path / "service.jsonl"
        log_file.write_text(
            json.dumps({"level": "ERROR", "message": "Connection timeout to payments service", "ts": 1}) + "\n" +
            json.dumps({"level": "ERROR", "message": "Connection timeout to payments service", "ts": 2}) + "\n" +
            json.dumps({"level": "INFO", "message": "Request completed", "ts": 3}) + "\n",
            encoding="utf-8",
        )

        result = ingest_json_logs(store, str(log_file), scope="payments", service_name="api-gateway")
        assert result.source_type == "json_logs"
        assert result.graph_nodes_written >= 2  # service + error nodes
        assert result.graph_edges_written >= 1
        # Recurring error (2 occurrences) should generate a trace
        traces = [f for f in os.listdir(paths.traces_dir(store)) if f.endswith(".md")]
        assert len(traces) >= 1


class TestTranscriptIngestion:
    def test_ingest_failure_transcript_drafts_lessons(self, store, tmp_path):
        from commontrace import paths
        from commontrace.ingest import ingest_failure_transcript

        transcript = tmp_path / "run.jsonl"
        transcript.write_text(
            json.dumps({"step_index": 1, "status": "ERROR", "content": "Tool execution failed: database is unreachable"}) + "\n" +
            json.dumps({"step_index": 2, "status": "DONE", "content": "Task complete"}) + "\n",
            encoding="utf-8",
        )

        result = ingest_failure_transcript(store, str(transcript), scope="db")
        assert result.source_type == "transcript"
        assert result.lessons_drafted >= 1

        lessons = [f for f in os.listdir(paths.lessons_dir(store)) if f.endswith(".md")]
        assert len(lessons) >= 1


class TestIngestionPipeline:
    def test_pipeline_unknown_type_returns_error(self, store):
        from commontrace.ingest import IngestionPipeline
        pipeline = IngestionPipeline()
        result = pipeline.ingest_source("/nonexistent", "unknown_type", store)
        assert len(result.errors) > 0
        assert "unknown_type" in result.errors[0]

    def test_pipeline_dispatches_markdown(self, store, tmp_path):
        from commontrace.ingest import IngestionPipeline
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Architecture\n\nPrefer async IO for all service calls.\n", encoding="utf-8")

        pipeline = IngestionPipeline()
        result = pipeline.ingest_source(str(docs), "markdown", store, scope="arch")
        assert result.source_type == "markdown"
