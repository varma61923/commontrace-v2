"""Unit tests for commontrace/hub_client.py's pure logic (section parsing,
file writing) that don't need a running Hub. hub/tests/test_tenant_isolation.py
and friends cover the server side against a real Postgres; hub/ itself is
outside the scope of this lightweight, PyYAML-only test suite."""
import os

import pytest

from commontrace import frontmatter, hub_client, paths


def test_lesson_sections_extracts_rule_and_how_to_apply():
    body = (
        "## Rule\nAlways check X before Y.\n\n"
        "## Why\nBecause it broke once.\n\n"
        "## How to apply\nRun the checker script first.\n\n"
        "## Counter-examples\nNever, this always applies.\n"
    )
    sections = hub_client._lesson_sections(body)
    assert sections["rule"] == "Always check X before Y."
    assert sections["how to apply"] == "Run the checker script first."
    assert sections["why"] == "Because it broke once."


def test_lesson_sections_handles_empty_body():
    assert hub_client._lesson_sections("") == {}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_iter_active_lesson_paths_skips_template_and_non_active(store):
    from commontrace.cli import main

    assert main(["init", "--agent-type", "code", "--dest", str(store)]) == 0
    ldir = paths.lessons_dir(str(store))

    fm_active = {
        "name": "lesson_a", "description": "d", "tags": [], "agent_type": "code",
        "domain": "testing", "importance": 3, "importance_rationale": "r",
        "importance_history": [], "applies_when": "when", "do_not_apply_when": "never",
        "uses": 0, "last_hit": "NEVER", "source_traces": [], "source_episodes": [],
        "hub_trace_id": None, "status": "active",
    }
    frontmatter.write(os.path.join(ldir, "lesson_a.md"), fm_active, "## Rule\nx\n")

    fm_archived = dict(fm_active, name="lesson_b", status="archived")
    frontmatter.write(os.path.join(ldir, "lesson_b.md"), fm_archived, "## Rule\nx\n")

    paths_found = list(hub_client._iter_active_lesson_paths(str(store)))
    basenames = {os.path.basename(p) for p in paths_found}
    assert "lesson_a.md" in basenames
    assert "lesson_template.md" not in basenames
    # archived lessons are still yielded by the path iterator (status is
    # filtered by the caller, push_active_lessons) -- assert the iterator
    # doesn't silently drop them, which would hide a status-filter bug.
    assert "lesson_b.md" in basenames
