"""Authoritative real-file boundaries for incremental fact snapshot publication."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from commontrace import _jsonl, fact_index, frontmatter, hierarchical, lesson_admission
from commontrace.fact_evidence import bind_evidence

SCORERS = ("overlap-v1", "bm25-v1")


def add(root: Path, statement: str, **options):
    return hierarchical.add_fact(str(root), statement, valid_from="2020-01-01T00:00:00Z", **options)[0]


def facts_path(root: Path) -> Path:
    return root / "memory" / "facts" / "facts.jsonl"


def replace_from_another_process(path: Path, old: str, new: str, *, atomic: bool) -> None:
    script = """
import os, sys
path, old, new, atomic = sys.argv[1:]
before = os.stat(path)
with open(path, encoding='utf-8') as source:
    content = source.read()
assert old in content and len(old.encode()) == len(new.encode())
target = path + '.external' if atomic == '1' else path
with open(target, 'w', encoding='utf-8') as output:
    output.write(content.replace(old, new))
    output.flush()
    os.fsync(output.fileno())
os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
if atomic == '1':
    os.replace(target, path)
"""
    subprocess.run([sys.executable, "-c", script, str(path), old, new, "1" if atomic else "0"],
                   check=True, capture_output=True, text=True)


def serialized(root: Path) -> tuple[tuple[str, ...], str]:
    rows = tuple(facts_path(root).read_text(encoding="utf-8").splitlines())
    return rows, hashlib.sha256("".join(row + "\n" for row in rows).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("scorer", SCORERS)
@pytest.mark.parametrize("atomic", [False, True])
def test_external_write_after_replace_cannot_be_labeled_as_our_commit(tmp_path, monkeypatch, scorer, atomic):
    fact = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"])
    held = fact_index.snapshot_facts(str(tmp_path))
    hierarchical.search_facts(str(tmp_path), "Lumen timeout", scope="alpha", scorer=scorer)
    original = _jsonl._replace
    observed = []

    def competing_replace(path, write):
        original(path, write)
        if Path(path) == facts_path(tmp_path):
            before = os.stat(path)
            replace_from_another_process(Path(path), "timeout is nine", "timeout is zero", atomic=atomic)
            after = os.stat(path)
            observed.append((before, after))

    monkeypatch.setattr(_jsonl, "_replace", competing_replace)
    returned = hierarchical.update_fact(str(tmp_path), fact.id, statement="Lumen timeout is nine seconds")
    assert returned.statement == "Lumen timeout is nine seconds"
    assert len(observed) == 1
    before, after = observed[0]
    assert before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
    assert (before.st_ino == after.st_ino) is (not atomic)
    with pytest.raises(fact_index.FactSnapshotChanged):
        held[fact.id]
    authoritative = hierarchical.load_facts(str(tmp_path))[fact.id].to_dict()
    ranked = hierarchical.search_facts(str(tmp_path), "Lumen timeout", scope="alpha", scorer=scorer)
    assert [row.to_dict() for row, _ in ranked] == [authoritative]
    assert ranked[0][0].statement == "Lumen timeout is zero seconds"
    assert hierarchical.search_facts(str(tmp_path), "nine", scope="alpha", scorer=scorer) == []


@pytest.mark.parametrize("scorer", SCORERS)
def test_removal_after_commit_cannot_publish_phantom_cached_rows(tmp_path, monkeypatch, scorer):
    fact = add(tmp_path, "Lumen timeout is five seconds")
    held = fact_index.snapshot_facts(str(tmp_path))
    original = _jsonl._replace

    def removing_replace(path, write):
        original(path, write)
        if Path(path) == facts_path(tmp_path):
            os.unlink(path)

    monkeypatch.setattr(_jsonl, "_replace", removing_replace)
    hierarchical.update_fact(str(tmp_path), fact.id, statement="Lumen timeout is nine seconds")
    assert not facts_path(tmp_path).exists()
    assert hierarchical.search_facts(str(tmp_path), "Lumen timeout", scorer=scorer) == []
    with pytest.raises(fact_index.FactSnapshotChanged):
        held.ensure_current()


@pytest.mark.parametrize("scorer", SCORERS)
def test_symlink_retarget_after_commit_does_not_publish_writer_payload(tmp_path, monkeypatch, scorer):
    local, routed = tmp_path / "local", tmp_path / "routed"
    fact = add(local, "Lumen timeout is five seconds", scopes=["alpha"])
    foreign = add(routed, "PRIVATE_ORCHID calibration", scopes=["beta"])
    held = fact_index.snapshot_facts(str(local))
    original = _jsonl._replace

    def rerouting_replace(path, write):
        original(path, write)
        if Path(path) == facts_path(local):
            os.unlink(path)
            os.symlink(facts_path(routed), path)

    monkeypatch.setattr(_jsonl, "_replace", rerouting_replace)
    hierarchical.update_fact(str(local), fact.id, statement="Lumen timeout is nine seconds")
    assert hierarchical.search_facts(str(local), "Lumen timeout", scope="alpha", scorer=scorer) == []
    assert hierarchical.search_facts(str(local), "PRIVATE_ORCHID", scope="alpha", scorer=scorer) == []
    # Existing trusted local fact-file symlink reads stay compatible. Index
    # ownership follows the actual current bytes, not the writer's old payload.
    ranked = hierarchical.search_facts(str(local), "PRIVATE_ORCHID", scope="beta", scorer=scorer)
    assert [row.id for row, _ in ranked] == [foreign.id]
    with pytest.raises(fact_index.FactSnapshotChanged):
        held.ensure_current()


@pytest.mark.parametrize("scorer", SCORERS)
def test_direct_full_replacement_removes_old_postings_and_scoped_statistics(tmp_path, scorer):
    private = add(tmp_path, "PRIVATE_ORCHID calibration", scopes=["alpha"])
    survivor = add(tmp_path, "Lumen timeout is five seconds", scopes=["beta"])
    held = fact_index.snapshot_facts(str(tmp_path))
    for scope in ("alpha", "beta"):
        hierarchical.search_facts(str(tmp_path), "calibration timeout", scope=scope, scorer="bm25-v1")
    survivor.statement = "Lumen latency is five seconds"
    hierarchical.save_facts(str(tmp_path), {survivor.id: survivor})
    with pytest.raises(fact_index.FactSnapshotChanged):
        held[private.id]
    assert hierarchical.search_facts(str(tmp_path), "PRIVATE_ORCHID", scope="alpha", scorer=scorer) == []
    assert hierarchical.search_facts(str(tmp_path), "timeout", scope="beta", scorer=scorer) == []
    assert set(fact_index.snapshot_facts(str(tmp_path))) == {survivor.id}
    assert hierarchical.search_facts(str(tmp_path), "Lumen latency", scope="alpha", scorer=scorer) == []
    ranked = hierarchical.search_facts(str(tmp_path), "Lumen latency", scope="beta", scorer=scorer)
    assert [row.to_dict() for row, _ in ranked] == [survivor.to_dict()]


@pytest.mark.parametrize("scorer", SCORERS)
def test_empty_commit_erases_retained_rows_postings_and_statistics(tmp_path, scorer):
    fact = add(tmp_path, "PRIVATE_ORCHID calibration", scopes=["alpha"])
    fact_index.clear_cache()
    held = fact_index.snapshot_facts(str(tmp_path))
    hierarchical.search_facts(str(tmp_path), "PRIVATE_ORCHID", scope="alpha", scorer="bm25-v1")
    hierarchical.save_facts(str(tmp_path), {})
    assert facts_path(tmp_path).read_bytes() == b""
    assert fact_index.cache_info()["statistics"]["entries"] == 0
    current = fact_index.snapshot_facts(str(tmp_path))
    assert len(current) == 0
    assert not current._snapshot.overlap and not current._snapshot.bm25
    assert hierarchical.search_facts(str(tmp_path), "PRIVATE_ORCHID", scope="alpha", scorer=scorer) == []
    assert hierarchical.search_facts(str(tmp_path), "", scope="alpha", scorer=scorer) == []
    with pytest.raises(fact_index.FactSnapshotChanged):
        held[fact.id]


@pytest.mark.parametrize("scorer", SCORERS)
def test_incremental_source_forgetting_withdraws_unchanged_derived_fact(tmp_path, scorer):
    source = add(tmp_path, "Recorded Lumen timeout measurement", scopes=["alpha"])
    evidence = bind_evidence(str(tmp_path), "fact", source.id)
    target = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"], evidence=[evidence])
    fallback = add(tmp_path, "Lumen timeout fallback remains available", scopes=["alpha"])
    held = fact_index.snapshot_facts(str(tmp_path))
    old_target = held[target.id].to_dict()
    assert target.id in {row.id for row, _ in hierarchical.search_facts(
        str(tmp_path), "Lumen timeout", scope="alpha", scorer=scorer)}
    hierarchical.forget_fact(str(tmp_path), source.id)
    assert fact_index.snapshot_facts(str(tmp_path))[target.id].to_dict() == old_target
    ranked = hierarchical.search_facts(str(tmp_path), "Lumen timeout", scope="alpha", scorer=scorer)
    assert [row.id for row, _ in ranked] == [fallback.id]
    with pytest.raises(fact_index.FactSnapshotChanged):
        held.ensure_current()


def test_failed_serialization_keeps_old_snapshot_and_authoritative_file(tmp_path, monkeypatch):
    fact = add(tmp_path, "Lumen timeout is five seconds")
    held = fact_index.snapshot_facts(str(tmp_path))
    before = facts_path(tmp_path).read_bytes()
    original = _jsonl._replace

    def failing_replace(path, write):
        def write_then_fail(output):
            write(output)
            raise OSError("intentional write interruption")
        return original(path, write_then_fail)

    monkeypatch.setattr(_jsonl, "_replace", failing_replace)
    with pytest.raises(OSError, match="write interruption"):
        hierarchical.update_fact(str(tmp_path), fact.id, statement="Lumen timeout is nine seconds")
    assert facts_path(tmp_path).read_bytes() == before
    assert held[fact.id].to_dict() == fact.to_dict()
    assert hierarchical.search_facts(str(tmp_path), "nine") == []


def test_returned_mutable_objects_cannot_change_committed_snapshot(tmp_path):
    fact = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"])
    old = fact_index.snapshot_facts(str(tmp_path))
    owned = old[fact.id]
    changed = hierarchical.update_fact(str(tmp_path), fact.id, statement="Lumen timeout is nine seconds")
    expected = json.loads(facts_path(tmp_path).read_text(encoding="utf-8"))
    changed.statement = "MUTABLE_CALLER_FORGERY"
    changed.scopes.append("beta")
    assert owned.statement == "Lumen timeout is five seconds"
    assert fact_index.snapshot_facts(str(tmp_path))[fact.id].to_dict() == expected
    assert hierarchical.search_facts(str(tmp_path), "MUTABLE_CALLER_FORGERY", scope="alpha") == []
    with pytest.raises(fact_index.FactSnapshotChanged):
        old[fact.id]


@pytest.mark.parametrize("scorer", SCORERS)
@pytest.mark.parametrize("change", ["body", "revoke", "restore_after_revoke"])
def test_unchanged_lesson_dependency_is_revalidated_after_incremental_write(tmp_path, scorer, change):
    path = tmp_path / "memory" / "lessons" / "lesson_lumen.md"
    path.parent.mkdir(parents=True)
    metadata = {"name": "lesson_lumen", "status": "active", "scopes": ["alpha"]}
    body = "Lumen timeout measurements establish a five second deadline.\n"
    metadata[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(
        str(tmp_path), str(path), metadata, body, actor="reviewer")
    frontmatter.write(str(path), metadata, body)
    target = add(tmp_path, "Lumen timeout is five seconds", scopes=["alpha"],
                 evidence=[bind_evidence(str(tmp_path), "lesson", "lumen")])
    fallback = add(tmp_path, "Lumen timeout troubleshooting has detailed fallback instructions", scopes=["alpha"])
    unrelated = add(tmp_path, "Independent calibration measurement", scopes=["alpha"])
    before = fact_index.snapshot_facts(str(tmp_path))
    old_target = before[target.id].to_dict()
    assert hierarchical.search_facts(str(tmp_path), "Lumen timeout", scope="alpha", scorer=scorer,
                                     limit=1)[0][0].id == target.id
    hierarchical.update_fact(str(tmp_path), unrelated.id, statement="Independent calibration procedure")
    committed = fact_index.snapshot_facts(str(tmp_path))
    assert committed.generation != before.generation
    assert committed[target.id].to_dict() == old_target
    generation = committed.generation
    if change == "body":
        frontmatter.write(str(path), metadata, body + "Unreviewed ordinary modification.\n")
    else:
        lesson_admission.revoke(str(tmp_path), str(path), actor="reviewer")
        if change == "restore_after_revoke":
            frontmatter.write(str(path), metadata, body)
    assert fact_index.snapshot_facts(str(tmp_path)).generation == generation
    for as_of in (None, "2025-01-01T00:00:00Z"):
        ranked = hierarchical.search_facts(str(tmp_path), "Lumen timeout", scope="alpha", scorer=scorer,
                                          limit=1, as_of=as_of)
        assert [row.id for row, _ in ranked] == [fallback.id]


@pytest.mark.parametrize("mismatch", ["digest", "rows"])
def test_publication_cannot_bind_uncommitted_rows_or_a_wrong_checksum(tmp_path, mismatch):
    fact = add(tmp_path, "Lumen timeout is five seconds")
    fact_index.snapshot_facts(str(tmp_path))
    base = fact_index.capture_for_write(str(tmp_path))
    assert base is not None
    rows, checksum = serialized(tmp_path)
    if mismatch == "digest":
        checksum = "0" * 64
    else:
        rows = tuple(row.replace("timeout is five", "timeout is nine") for row in rows)
        checksum = hashlib.sha256("".join(row + "\n" for row in rows).encode("utf-8")).hexdigest()
    assert not fact_index.publish_committed(str(tmp_path), base, rows, checksum)
    assert fact_index.snapshot_facts(str(tmp_path))[fact.id].to_dict() == fact.to_dict()
    assert hierarchical.search_facts(str(tmp_path), "nine") == []


def test_a_foreign_root_base_cannot_be_used_for_publication(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    visible = add(left, "Lumen timeout is five seconds", scopes=["alpha"])
    private = add(right, "PRIVATE_ORCHID procedure", scopes=["beta"])
    fact_index.snapshot_facts(str(right))
    foreign = fact_index.capture_for_write(str(right))
    assert foreign is not None
    rows, checksum = serialized(left)
    assert not fact_index.publish_committed(str(left), foreign, rows, checksum)
    assert set(fact_index.snapshot_facts(str(left))) == {visible.id}
    assert private.id not in fact_index.snapshot_facts(str(left))


@pytest.mark.parametrize("atomic", [False, True])
def test_source_change_during_record_construction_fences_publication(tmp_path, monkeypatch, atomic):
    fact = add(tmp_path, "Lumen timeout is five seconds")
    fact_index.snapshot_facts(str(tmp_path))
    base = fact_index.capture_for_write(str(tmp_path))
    assert base is not None
    rows = (json.dumps({**fact.to_dict(), "statement": "Lumen timeout is nine seconds"}, ensure_ascii=False),)
    checksum = _jsonl.write_serialized_rows(str(facts_path(tmp_path)), rows)
    original = fact_index._record
    changed = False

    def change_after_hash(*args):
        nonlocal changed
        record = original(*args)
        if not changed:
            changed = True
            replace_from_another_process(facts_path(tmp_path), "timeout is nine", "timeout is zero", atomic=atomic)
        return record

    monkeypatch.setattr(fact_index, "_record", change_after_hash)
    assert not fact_index.publish_committed(str(tmp_path), base, rows, checksum)
    assert changed
    assert hierarchical.search_facts(str(tmp_path), "Lumen timeout")[0][0].statement == "Lumen timeout is zero seconds"
    assert hierarchical.search_facts(str(tmp_path), "nine") == []


def test_external_generation_before_capture_drops_old_retention_without_cold_build(tmp_path):
    add(tmp_path, "Lumen timeout is five seconds")
    fact_index.clear_cache()
    held = fact_index.snapshot_facts(str(tmp_path))
    hierarchical.search_facts(str(tmp_path), "Lumen timeout", scorer="bm25-v1")
    replace_from_another_process(facts_path(tmp_path), "timeout is five", "timeout is nine", atomic=True)
    assert fact_index.capture_for_write(str(tmp_path)) is None
    assert fact_index.cache_info()["snapshots"]["entries"] == 0
    assert fact_index.cache_info()["statistics"]["entries"] == 0
    with pytest.raises(fact_index.FactSnapshotChanged):
        held.ensure_current()


def test_multiline_row_envelopes_cannot_publish_different_jsonl_interpretations(tmp_path):
    first = add(tmp_path, "Lumen timeout is five seconds")
    second = add(tmp_path, "PRIVATE_ORCHID calibration", scopes=["beta"])
    fact_index.snapshot_facts(str(tmp_path))
    base = fact_index.capture_for_write(str(tmp_path))
    assert base is not None
    ordinary, checksum = serialized(tmp_path)
    assert len(ordinary) == 2
    combined = ("\n".join(ordinary),)
    assert hashlib.sha256((combined[0] + "\n").encode("utf-8")).hexdigest() == checksum
    assert not fact_index.publish_committed(str(tmp_path), base, combined, checksum)
    assert set(fact_index.snapshot_facts(str(tmp_path))) == {first.id, second.id}
