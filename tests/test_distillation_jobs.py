"""Durable grounded extraction retries keep review gates and source evidence."""
from __future__ import annotations

import glob
from pathlib import Path
from typing import Any

import pytest

from commontrace import frontmatter, jobs, paths, trace_io
from commontrace.cli import main
from commontrace.distillation_worker import distill_job


@pytest.mark.parametrize("payload", [
    {"status": "active"}, {"failed_only": "false"}, {"semantic_dedup": 1},
    {"min_validation_score": float("nan")}, {"similarity": float("inf")},
    {"min_cluster": True}, {"min_cluster": 1001}, {"agent_type": []},
])
def test_worker_rejects_invalid_or_privilege_expanding_configuration(tmp_path: Path, payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        distill_job(str(tmp_path), payload)
    assert not (tmp_path / "memory").exists()


def test_real_durable_distillation_is_review_only_and_retry_safe(tmp_path: Path) -> None:
    root = str(tmp_path)
    assert main(["init", "--dest", root, "--agent-type", "support"]) == 0
    for number in range(3):
        trace_io.write_new(
            root, title=f"Postgres connection timeout {number}",
            context="A postgres database transaction timed out because all pooled connections were busy.",
            solution="Bound concurrent requests to the database pool size and retry only transient connection failures.",
            tags=["postgres", "connection", "timeout"], agent_type="support", outcome={"resolved": False},
        )
    first = jobs.enqueue(root, "distill", {"min_validation_score": 0.7}, dedupe_key="failure-lessons")
    assert jobs.enqueue(root, "distill", {"min_validation_score": 0.7}, dedupe_key="failure-lessons").id == first.id
    assert jobs.run_pending(root, kinds=["distill"]) == {"done": 1, "failed": 0, "pending": 0}
    lessons = glob.glob(str(Path(paths.lessons_dir(root)) / "lesson_candidate_*.md"))
    assert len(lessons) == 1
    fm, body = frontmatter.read(lessons[0])
    assert fm["status"] == "review" and fm["distillation"]["method"] == "evidence-consensus-v1"
    assert len(fm["source_traces"]) == 3 and "TODO" not in body
    second = jobs.enqueue(root, "distill", {}, dedupe_key="failure-lessons")
    assert second.id != first.id
    assert jobs.run_pending(root, kinds=["distill"])["done"] == 1
    assert glob.glob(str(Path(paths.lessons_dir(root)) / "lesson_candidate_*.md")) == lessons
