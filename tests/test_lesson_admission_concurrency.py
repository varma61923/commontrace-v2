"""Fresh approval is required across CLI and cross-channel body admission."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from commontrace import frontmatter, lesson_cache, recall, retrieval_io
from commontrace.commands import query_cmd


@pytest.mark.parametrize("change", [
    {"status": "review"}, {"status": "archived"}, {"name": "replacement"},
    {"valid_until": "2020-01-01"}, {"valid_from": "2999-01-01"},
])
def test_body_read_cannot_admit_revoked_cached_lesson(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any],
) -> None:
    path = tmp_path / "memory" / "lessons" / "lesson_recovery.md"
    path.parent.mkdir(parents=True)
    cached: dict[str, Any] = {
        "name": "recovery", "status": "active", "importance": 4,
        "description": "postgres transaction recovery", "tags": ["postgres", "transaction"],
        "applies_when": "A postgres transaction fails",
    }
    frontmatter.write(str(path), {**cached, **change}, "## Rule\nA newly revoked instruction.")
    active = [(str(path), cached)]
    monkeypatch.setattr(lesson_cache, "load_active_with_terms", lambda *a, **kw: (active, None))
    considered, dose = query_cmd._apply_dosage(
        active, [("recovery", 1.0)], retrieval_io.RetrievalConfig(),
    )
    assert considered == {}
    assert not dose.admitted
    result = recall.recall(str(tmp_path), "postgres transaction recovery", channels=("lessons",))
    assert result.items == []
    assert "revoked instruction" not in result.context


def test_scope_change_is_rechecked_after_ranking(tmp_path: Path) -> None:
    path = tmp_path / "lesson_scope.md"
    cached = {"name": "scope", "status": "active", "scopes": ["payments"]}
    frontmatter.write(str(path), {**cached, "scopes": ["billing"]}, "## Rule\nBilling only.")
    considered, _ = query_cmd._apply_dosage(
        [(str(path), cached)], [("scope", 1.0)], retrieval_io.RetrievalConfig(), scope="payments",
    )
    assert considered == {}
