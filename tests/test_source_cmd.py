from __future__ import annotations

import json
import os

import pytest

from commontrace import holdout_io, memory_sources
from commontrace.cli import main

FIXTURE = """# Notes

## Idempotency

Set an idempotency key.

## Office hours

Closed Fridays.
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(FIXTURE, encoding="utf-8")
    holdout_io.configure(str(tmp_path), rate=0.0)
    return tmp_path


def test_sections_lists_ids_and_headings(store, capsys):
    assert main(["source", "sections", "CLAUDE.md", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == [
        {"id": "idempotency", "heading": "Idempotency"},
        {"id": "office-hours", "heading": "Office hours"},
    ]


def test_sections_on_a_file_with_no_headings_says_so(store, capsys):
    (store / "PLAIN.md").write_text("just text\n", encoding="utf-8")
    assert main(["source", "sections", "PLAIN.md"]) == 0
    assert "nothing to measure separately" in capsys.readouterr().out


def test_sections_on_a_missing_file_fails_cleanly(store, capsys):
    assert main(["source", "sections", "NOPE.md"]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_render_with_experiment_stopped_writes_the_whole_file(store, capsys):
    assert main(["source", "render", "CLAUDE.md", "--occasion-id", "o1"]) == 0
    out = capsys.readouterr().out
    assert out == FIXTURE


def test_render_to_a_file_reports_the_write(store, capsys):
    out_path = str(store / "rendered.md")
    assert main(["source", "render", "CLAUDE.md", "--occasion-id", "o1", "--out", out_path]) == 0
    assert os.path.isfile(out_path)
    assert f"wrote {out_path}" in capsys.readouterr().err


def test_render_reports_the_next_command_to_run(store, capsys):
    main(["source", "render", "CLAUDE.md", "--occasion-id", "o1"])
    assert "source outcome --occasion-id o1" in capsys.readouterr().err


def test_outcome_records_success(store, capsys):
    assert main(["source", "outcome", "--occasion-id", "o1", "--succeeded"]) == 0
    assert holdout_io.read_outcomes(str(store)) == {"o1": True}
    assert "recorded" in capsys.readouterr().out


def test_outcome_records_failure(store):
    assert main(["source", "outcome", "--occasion-id", "o1", "--failed"]) == 0
    assert holdout_io.read_outcomes(str(store)) == {"o1": False}


def test_a_contradicting_outcome_report_fails_cleanly(store, capsys):
    main(["source", "outcome", "--occasion-id", "o1", "--succeeded"])
    assert main(["source", "outcome", "--occasion-id", "o1", "--failed"]) == 1
    assert "already recorded" in capsys.readouterr().err


def test_withdraw_then_render_excludes_the_section(store, capsys):
    assert main(["source", "withdraw", "CLAUDE.md", "--section-id", "office-hours"]) == 0
    capsys.readouterr()
    main(["source", "render", "CLAUDE.md", "--occasion-id", "o1"])
    captured = capsys.readouterr()
    assert "Office hours" not in captured.out
    assert "blocked (harm-withdrawn" in captured.err
    assert "office-hours" in captured.err


def test_reinstate_after_withdraw_restores_it(store, capsys):
    main(["source", "withdraw", "CLAUDE.md", "--section-id", "office-hours"])
    capsys.readouterr()
    assert main(["source", "reinstate", "CLAUDE.md", "--section-id", "office-hours"]) == 0
    assert "eligible again" in capsys.readouterr().out
    main(["source", "render", "CLAUDE.md", "--occasion-id", "o1"])
    assert "Office hours" in capsys.readouterr().out


def test_reinstate_something_never_blocked_says_so(store, capsys):
    assert main(["source", "reinstate", "CLAUDE.md", "--section-id", "office-hours"]) == 0
    assert "was not blocked" in capsys.readouterr().out


def test_dest_flag_controls_where_the_blocklist_lives(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    claude_md = project / "CLAUDE.md"
    claude_md.write_text(FIXTURE, encoding="utf-8")
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    assert main([
        "source", "withdraw", str(claude_md), "--section-id", "office-hours",
        "--dest", str(store_dir),
    ]) == 0
    assert os.path.isfile(memory_sources.blocklist_path(str(store_dir)))
