"""Durable writes stay authoritative when optional acceleration fails."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from commontrace import _jsonl, fact_index, hierarchical


def test_serialized_writer_preserves_exact_unicode_and_escaped_newlines(tmp_path: Path) -> None:
    path = tmp_path / "facts.jsonl"
    rows = (json.dumps({"statement": "部署\nquorum 🌍", "id": "example"}, ensure_ascii=False),)
    checksum = _jsonl.write_serialized_rows(str(path), rows)
    expected = (rows[0] + "\n").encode("utf-8")
    assert path.read_bytes() == expected
    assert checksum == hashlib.sha256(expected).hexdigest()
    assert _jsonl.read_rows(str(path)) == [{"statement": "部署\nquorum 🌍", "id": "example"}]


@pytest.mark.parametrize("stage", ["capture_for_write", "publish_committed"])
def test_cache_failure_does_not_report_successful_canonical_write_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Old orbital calibration.")
    old_view = fact_index.snapshot_facts(root)

    def unavailable(*args: object) -> None:
        raise RuntimeError("synthetic acceleration failure")

    monkeypatch.setattr(fact_index, stage, unavailable)
    changed = hierarchical.update_fact(root, fact.id, statement="New orbital calibration.")
    assert changed.statement == "New orbital calibration."
    with pytest.raises(fact_index.FactSnapshotChanged):
        old_view.ensure_current()
    assert hierarchical.load_facts(root)[fact.id].statement == changed.statement
    assert hierarchical.search_facts(root, "new orbital")[0][0].statement == changed.statement


def test_failed_atomic_replacement_preserves_current_disk_and_warm_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Original calibration.")
    view = fact_index.snapshot_facts(root)
    path = Path(hierarchical._facts_file(root))
    before = path.read_bytes()

    def failed_replace(*args: object) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(_jsonl.os, "replace", failed_replace)
    with pytest.raises(OSError, match="synthetic disk failure"):
        hierarchical.update_fact(root, fact.id, statement="Uncommitted calibration.")
    assert path.read_bytes() == before
    view.ensure_current()
    assert view[fact.id].statement == "Original calibration."
    assert not list(path.parent.glob(".facts.jsonl.*.tmp"))


def test_failed_mutation_body_never_commits_or_revises_warm_generation(tmp_path: Path) -> None:
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Original configuration.")
    view = fact_index.snapshot_facts(root)
    with pytest.raises(ValueError, match="abort"):
        with hierarchical.mutate_facts(root) as facts:
            facts[fact.id].statement = "Aborted configuration."
            raise ValueError("abort")
    view.ensure_current()
    assert hierarchical.load_facts(root)[fact.id].statement == "Original configuration."


def test_serialization_error_preserves_authoritative_file(tmp_path: Path) -> None:
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Original configuration.")
    view = fact_index.snapshot_facts(root)
    facts = hierarchical.load_facts(root)
    facts[fact.id].source_traces = [object()]  # type: ignore[list-item]
    with pytest.raises(TypeError):
        hierarchical.save_facts(root, facts)
    view.ensure_current()
    assert hierarchical.load_facts(root)[fact.id].source_traces == []
