"""Malformed model decisions and interrupted conflict journals fail safely."""
from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from commontrace import fact_conflicts, hierarchical


@pytest.mark.parametrize("payload", [
    {"duplicates": "a"}, {"contradictions": "a"}, {"replaces": "a"},
    {"contradictions": {"a": True}}, {"duplicates": ["a", 1]},
    {"duplicates": None, "contradictions": ["a"]}, {"contradictions": ["a"] * 16},
    {"duplicates": ["a"], "contradictions": ["a"]},
])
def test_untyped_or_unbounded_judgments_cannot_mutate_truth(payload):
    candidates = [SimpleNamespace(id="a", statement="Alice likes spicy food")]
    result = fact_conflicts.judge_joint("Alice dislikes spicy food", candidates,
                                       complete=lambda _: (json.dumps(payload), {}))
    assert result == {"duplicates": set(), "contradictions": set()}


def test_typed_judgment_keeps_only_offered_ids_and_supports_legacy_replaces():
    candidates = [SimpleNamespace(id="a", statement="Alice likes spicy food")]
    result = fact_conflicts.judge_joint("Alice dislikes spicy food", candidates,
                                       complete=lambda _: ('{"replaces":["a","fabricated"]}', {}))
    assert result == {"duplicates": set(), "contradictions": {"a"}}


def test_interrupted_or_malformed_queue_preserves_valid_pending_conflicts(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setenv("COMMONTRACE_FACT_CONFLICTS", "off")
    older = hierarchical.add_fact(root, "Alice likes spicy food")[0]
    newer = hierarchical.add_fact(root, "Alice does not like spicy food")[0]
    conflict = fact_conflicts.Conflict(older.id, newer.id, "negation", older.statement,
                                       newer.statement, hierarchical._now())
    fact_conflicts.record(root, [conflict])
    filename = fact_conflicts._queue_path(root)
    with open(filename, "a", encoding="utf-8") as handle:
        handle.write('{"older":\n')  # interrupted append
        handle.write(json.dumps({"older": older.id}) + "\n")
        handle.write(json.dumps({**asdict(conflict), "older": []}) + "\n")
        handle.write("[]\nnull\n")
    assert fact_conflicts.pending(root) == [conflict]
