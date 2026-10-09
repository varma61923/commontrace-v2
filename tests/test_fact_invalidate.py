"""Bi-temporal invalidation: a fact stops being true without being replaced or deleted."""
import pytest

from commontrace import hierarchical
from commontrace.cli import main


def _fact(root, text="Ana works at Acme", since="2026-01-01T00:00:00+00:00"):
    fact, _action = hierarchical.add_fact(root, text, valid_from=since)
    return fact


def test_invalidated_fact_leaves_the_present_but_stays_in_history(tmp_path):
    root = str(tmp_path)
    fact = _fact(root)
    ended = hierarchical.invalidate_fact(root, fact.id, at="2026-03-01T00:00:00+00:00")
    assert ended.status == "invalidated" and ended.valid_until.startswith("2026-03-01")
    assert fact.id not in {f.id for f in hierarchical.list_facts(root)}
    assert fact.id in {f.id for f in hierarchical.list_facts(root, as_of="2026-02-01")}
    assert fact.id not in {f.id for f in hierarchical.list_facts(root, as_of="2026-04-01")}
    assert fact.id in {f.id for f in hierarchical.list_facts(root, status="invalidated")}
    with pytest.raises(ValueError, match="not active"):
        hierarchical.invalidate_fact(root, fact.id)


def test_future_end_is_scheduled_and_end_before_start_is_refused(tmp_path):
    root = str(tmp_path)
    fact = _fact(root)
    scheduled = hierarchical.invalidate_fact(root, fact.id, at="2999-01-01")
    assert scheduled.status == "active" and scheduled.valid_until.startswith("2999-01-01")
    other = _fact(root, "Ana lives in Lima", since="2026-05-01T00:00:00+00:00")
    with pytest.raises(ValueError):
        hierarchical.invalidate_fact(root, other.id, at="2026-04-01")
    with pytest.raises(KeyError):
        hierarchical.invalidate_fact(root, "missing")


def test_cli_invalidate(tmp_path, capsys):
    root = str(tmp_path)
    fact = _fact(root)
    assert main(["fact", "invalidate", fact.id, "--at", "2026-02-01", "--dest", root]) == 0
    assert "invalidated" in capsys.readouterr().out
    assert main(["fact", "invalidate", fact.id, "--dest", root]) == 1
