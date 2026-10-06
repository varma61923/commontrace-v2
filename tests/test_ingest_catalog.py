"""Tests for ingest job lifecycle and document catalog."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace.ingest import catalog

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


def test_ingest_job_lifecycle_stages(store):
    job = catalog.create_ingest_job(store, "data/corpus.json")
    assert job.stage == "queued"

    # Progress through stages
    for stage in ("extracting", "transforming", "embedding", "submitting", "done"):
        job = catalog.update_ingest_job(store, job.id, stage, message=f"At {stage}")
        assert job.stage == stage

    jobs = catalog.list_ingest_jobs(store)
    assert len(jobs) == 1
    assert jobs[0].id == job.id
    with pytest.raises(ValueError, match="terminal"):
        catalog.update_ingest_job(store, job.id, "extracting")


def test_document_catalog_summary_vs_get(store):
    content = "Quick summary.\n\n" + ("Long body text details. " * 200)
    doc = catalog.record_document(
        store, "/docs/runbook.md", content, title="Operations Runbook",
        chunks=["chunk0", "chunk1", "chunk2"],
    )

    # list_documents returns summary list, never dumping content
    summaries = catalog.list_documents(store)
    assert len(summaries) == 1
    assert summaries[0]["id"] == doc.id
    assert summaries[0]["title"] == "Operations Runbook"
    assert len(summaries[0]["summary"]) <= 200
    assert "content" not in summaries[0]

    # get_document returns full content
    full = catalog.get_document(store, doc.id)
    assert full is not None
    assert full["content"] == content
    assert len(full["chunks"]) == 3

    # chunk retrieval
    chunk = catalog.get_document(store, doc.id, chunk_index=1)
    assert chunk["requested_chunk"] == "chunk1"


def test_ingest_cli_catalog_and_status(store):
    catalog.record_document(store, "/docs/cli_test.md", "CLI Document Body", title="CLI Doc")
    job = catalog.create_ingest_job(store, "/docs/cli_test.md")
    catalog.update_ingest_job(store, job.id, "done", message="CLI Ingest Complete")

    # list docs
    res_list = cli("ingest", "--list-docs", "--dest", store)
    assert res_list.returncode == 0
    assert "CLI Doc" in res_list.stdout

    # get doc
    res_get = cli("ingest", "--get-doc", "/docs/cli_test.md", "--dest", store)
    assert res_get.returncode == 0
    assert "CLI Document Body" in res_get.stdout

    # job status
    res_stat = cli("ingest", "--job-status", job.id, "--dest", store)
    assert res_stat.returncode == 0
    assert "DONE" in res_stat.stdout
    assert "CLI Ingest Complete" in res_stat.stdout
