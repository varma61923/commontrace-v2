from dataclasses import replace

import pytest

from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


def _layered_records(journal, layers=12):
    parents, records = [], []
    for level in range(layers):
        following = [f"layer-{level}-a", f"layer-{level}-b"]
        for identity in following:
            records.append(journal.put(MemoryRecord(
                identity, "observation", 1, {"text": identity}, relations={"derives": parents},
            )))
        parents = following
    records.append(journal.put(MemoryRecord(
        "root", "observation", 1, {"text": "uniquerootquery"}, relations={"derives": parents},
    )))
    return records


@pytest.mark.parametrize("operation", ["current", "search"])
def test_shared_evidence_does_not_amplify_grounding_work(tmp_path, monkeypatch, operation):
    journal = Journal(str(tmp_path))
    records = _layered_records(journal)
    checked = []
    readable = journal._runtime_readable

    def count_checks(record):
        checked.append(record.id)
        return readable(record)

    monkeypatch.setattr(journal, "_runtime_readable", count_checks)
    result = (journal.current("root", scope={}) if operation == "current"
              else journal.search("uniquerootquery", scope={}))
    assert result
    assert len(checked) <= len(records), "shared dependencies must not be recursively rechecked"

    # Request memoization must never survive a source revision/retraction.
    journal.put(replace(records[0], version=2, status="retired"))
    assert journal.current("root", scope={}) is None
    assert journal.search("uniquerootquery", scope={}) == []


def test_grounding_cycles_and_depth_are_not_admitted(tmp_path):
    journal = Journal(str(tmp_path))
    for identity, parent in (("cycle-a", "cycle-b"), ("cycle-b", "cycle-a")):
        journal.put(MemoryRecord(identity, "observation", 1, {"text": "cycle"},
                                 relations={"derives": [parent]}))
    assert journal.current("cycle-a", scope={}) is None
    for index in range(65):
        journal.put(MemoryRecord(f"chain-{index}", "observation", 1, {"text": "chain"},
                                 relations={"derives": [f"chain-{index - 1}"]} if index else {}))
    assert journal.current("chain-64", scope={}) is None
    assert journal.current("chain-63", scope={}) is not None


def test_grounding_has_a_total_work_budget_and_deduplicates_edges(tmp_path, monkeypatch):
    import unimem.memory_journal as memory_journal
    from unimem.record_visibility import GroundingState

    journal = Journal(str(tmp_path))
    leaves = [f"leaf-{index}" for index in range(10)]
    for identity in leaves:
        journal.put(MemoryRecord(identity, "observation", 1, {"text": identity}))
    journal.put(MemoryRecord("wide", "observation", 1, {"text": "wide"}, relations={"derives": leaves}))
    journal.put(MemoryRecord("duplicates", "observation", 1, {"text": "duplicate"},
                             relations={"derives": [leaves[0]] * 5000}))
    monkeypatch.setattr(memory_journal, "GroundingState", lambda: GroundingState(remaining=8))
    assert journal.current("wide", scope={}) is None
    assert journal.current("duplicates", scope={}) is not None


@pytest.mark.parametrize("operation", ["current", "search"])
def test_grounding_memo_uses_one_sqlite_revision_during_concurrent_write(tmp_path, monkeypatch, operation):
    journal = Journal(str(tmp_path))
    source = journal.put(MemoryRecord("source", "observation", 1, {"text": "evidence"}))
    for identity in ("left", "right"):
        journal.put(MemoryRecord(identity, "observation", 1, {"text": identity},
                                 relations={"derives": [source.id]}))
    journal.put(MemoryRecord("root", "observation", 1, {"text": "uniquerootquery"},
                             relations={"derives": ["left", "right"]}))
    grounded, readable = journal._grounded, journal._runtime_readable
    versions, changed = [], []

    def observe(db, record, *args, **kwargs):
        if record.id == source.id:
            versions.append(record.version)
        return grounded(db, record, *args, **kwargs)

    def retract_between_branches(record):
        if record.id == "right" and not changed:
            changed.append(True)
            # WAL permits a different connection to commit while the reader
            # continues proving the snapshot it opened before the update.
            journal.put(replace(source, version=2, status="retired"))
        return readable(record)

    monkeypatch.setattr(journal, "_grounded", observe)
    monkeypatch.setattr(journal, "_runtime_readable", retract_between_branches)
    result = (journal.current("root", scope={}) if operation == "current"
              else journal.search("uniquerootquery", scope={}))
    assert result
    assert versions == [1, 1]
    assert journal.current("root", scope={}) is None
