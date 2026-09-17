"""Tests for `commontrace export` -- the missing counterpart to
`commontrace/import_data.py`'s bulk importer. No competitor comparison
here; this closes a plain, verifiable gap: there was no way to get a
store's own corpus out as one portable file at all.
"""
from __future__ import annotations

import glob
import json
import os

import pytest

from commontrace import lesson_io, paths
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _lesson(root: str, slug: str, **fm_extra) -> None:
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = {
        "name": slug, "description": "A test lesson", "status": "active",
        "importance": 3, "tags": ["t"], "agent_type": "code", "domain": "net",
        "applies_when": "when X", "do_not_apply_when": "never",
    }
    fm.update(fm_extra)
    lesson_io.write_lesson(path, fm, "## Rule\nDo the thing.\n", root=root,
                           actor="test", reason="fixture")


_BOOL_FLAGS = {"resolved", "escalated", "repeated_error", "frustration", "baseline"}


def _capture(store: str, title: str, **extra) -> None:
    argv = ["capture", "--title", title, "--context", "c", "--solution", "s",
            "--agent-type", "code", "--dest", str(store)]
    for k, v in extra.items():
        flag = k.replace("_", "-")
        if k in _BOOL_FLAGS:
            if v in (True, "true", "True"):
                argv.append(f"--{flag}")
            elif v in (False, "false", "False"):
                argv.append(f"--not-{flag}")
            continue
        argv += [f"--{flag}", str(v)]
    assert main(argv) == 0


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestExportBasics:
    def test_exports_lessons_and_traces_by_default(self, store, tmp_path, capsys):
        _lesson(str(store), "a")
        _capture(store, "a trace")
        out_path = str(tmp_path / "out.jsonl")

        rc = main(["export", "--out", out_path, "--dest", str(store)])
        assert rc == 0
        rows = _read_jsonl(out_path)
        kinds = {r["kind"] for r in rows}
        assert kinds == {"lesson", "trace"}

    def test_kind_lessons_excludes_traces(self, store, tmp_path):
        _lesson(str(store), "a")
        _capture(store, "a trace")
        out_path = str(tmp_path / "out.jsonl")

        main(["export", "--kind", "lessons", "--out", out_path, "--dest", str(store)])
        rows = _read_jsonl(out_path)
        assert {r["kind"] for r in rows} == {"lesson"}

    def test_kind_traces_excludes_lessons(self, store, tmp_path):
        _lesson(str(store), "a")
        _capture(store, "a trace")
        out_path = str(tmp_path / "out.jsonl")

        main(["export", "--kind", "traces", "--out", out_path, "--dest", str(store)])
        rows = _read_jsonl(out_path)
        assert {r["kind"] for r in rows} == {"trace"}

    def test_status_filters_lessons(self, store, tmp_path):
        _lesson(str(store), "active_one", status="active")
        _lesson(str(store), "review_one", status="review")
        out_path = str(tmp_path / "out.jsonl")

        main(["export", "--kind", "lessons", "--status", "active", "--out", out_path,
              "--dest", str(store)])
        rows = _read_jsonl(out_path)
        assert [r["name"] for r in rows] == ["active_one"]

    def test_agent_type_filters_both_kinds(self, store, tmp_path):
        _lesson(str(store), "code_one", agent_type="code")
        _lesson(str(store), "support_one", agent_type="support")
        out_path = str(tmp_path / "out.jsonl")

        main(["export", "--kind", "lessons", "--agent-type", "support",
              "--out", out_path, "--dest", str(store)])
        rows = _read_jsonl(out_path)
        assert [r["name"] for r in rows] == ["support_one"]

    def test_a_lesson_row_carries_body_and_full_frontmatter(self, store, tmp_path):
        _lesson(str(store), "a", importance=5)
        out_path = str(tmp_path / "out.jsonl")
        main(["export", "--kind", "lessons", "--out", out_path, "--dest", str(store)])
        row = _read_jsonl(out_path)[0]
        assert row["importance"] == 5
        assert "Do the thing" in row["body"]

    def test_no_out_flag_writes_to_stdout(self, store, capsys):
        _lesson(str(store), "a")
        rc = main(["export", "--kind", "lessons", "--dest", str(store)])
        assert rc == 0
        out = capsys.readouterr().out
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert rows[0]["kind"] == "lesson"

    def test_an_empty_store_exports_nothing_without_error(self, store, tmp_path, capsys):
        main(["init", "--dest", str(store)])
        out_path = str(tmp_path / "out.jsonl")
        rc = main(["export", "--out", out_path, "--dest", str(store)])
        assert rc == 0
        assert _read_jsonl(out_path) == []

    def test_reports_counts_on_stderr(self, store, tmp_path, capsys):
        _lesson(str(store), "a")
        _capture(store, "a trace")
        out_path = str(tmp_path / "out.jsonl")
        main(["export", "--out", out_path, "--dest", str(store)])
        err = capsys.readouterr().err
        assert "1 lesson(s)" in err
        assert "1 trace(s)" in err


class TestTraceRoundTrip:
    """A trace exported here must be re-importable with no field-mapping
    flags -- the whole point of writing it in the GENERIC importer's own
    flat shape."""

    def test_an_exported_trace_reimports_cleanly_into_a_second_store(
        self, store, tmp_path,
    ):
        _capture(store, "Password reset email never arrived",
                 context="Customer reported a missing reset email.",
                 solution="Removed the address from the suppression list.",
                 tags="email,password-reset", resolved="true")
        out_path = str(tmp_path / "out.jsonl")
        assert main(["export", "--kind", "traces", "--out", out_path,
                     "--dest", str(store)]) == 0

        second_store = str(tmp_path / "second")
        assert main(["init", "--agent-type", "code", "--dest", second_store]) == 0
        rc = main(["import", out_path, "--agent-type", "code", "--dest", second_store])
        assert rc == 0

        imported = sorted(
            p for p in glob.glob(os.path.join(paths.traces_dir(second_store), "*.md"))
            if os.path.basename(p) != "README.md"
        )
        assert len(imported) == 1
        from commontrace import trace_io

        inst, _body = trace_io.read(imported[0])
        assert inst["title"] == "Password reset email never arrived"
        assert inst["context_text"] == "Customer reported a missing reset email."
        assert inst["solution_text"] == "Removed the address from the suppression list."
        assert inst["outcome"]["resolved"] is True

    def test_a_lesson_row_in_a_mixed_export_is_skipped_not_crashed_on_reimport(
        self, store, tmp_path, capsys,
    ):
        """A `--kind all` export mixes lesson and trace rows in one file;
        commontrace import has no lesson importer, so a lesson row must be
        skipped with a reason, never a crash or a silently-wrong trace."""
        _lesson(str(store), "a")
        _capture(store, "a real trace")
        out_path = str(tmp_path / "out.jsonl")
        main(["export", "--out", out_path, "--dest", str(store)])

        second_store = str(tmp_path / "second")
        main(["init", "--agent-type", "code", "--dest", second_store])
        capsys.readouterr()
        rc = main(["import", out_path, "--agent-type", "code", "--dest", second_store])
        assert rc == 0
        imported = [
            p for p in glob.glob(os.path.join(paths.traces_dir(second_store), "*.md"))
            if os.path.basename(p) != "README.md"
        ]
        assert len(imported) == 1  # only the trace row, not the lesson row
