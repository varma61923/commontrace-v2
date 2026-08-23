"""One corrupt/unreadable lesson or trace file must not abort a listing or
aggregation command that would otherwise report on every OTHER file in the
store just fine. `lesson validate`/`trace validate` already treated this as
a per-file failure; every other command that globs the same directories
(lesson list, trace list, query, commons, overlap, experiment, reliability)
read each file with no exception handling at all, so a single hand-edited
file left mid-save (bad YAML, a stray control character) raised out of the
loop and crashed the whole command with a traceback -- see
commontrace/commands/_format.py:read_or_warn, the shared fix.
"""
from __future__ import annotations

import pytest

from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _corrupt(path):
    # commontrace's own minimal frontmatter parser is forgiving of
    # malformed YAML-ish text (it just parses what it can) -- what
    # actually raises FrontmatterError is a file that can't be decoded at
    # all, e.g. one truncated mid-write or corrupted at the byte level.
    with open(path, "wb") as fh:
        fh.write(b"---\nname: \xff\xfe not valid utf-8\n---\nbody\n")


class TestLessonListSurvivesACorruptFile:
    def test_one_bad_lesson_does_not_hide_the_others(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        ldir = store / "memory" / "lessons"
        main([
            "lesson", "new", "--slug", "good-lesson", "--description", "good lesson", "--tags", "x",
            "--agent-type", "code", "--domain", "testing", "--applies-when", "w",
            "--do-not-apply-when", "n", "--dest", str(store),
        ])
        _corrupt(ldir / "lesson_broken.md")
        capsys.readouterr()

        rc = main(["lesson", "list", "--dest", str(store)])
        out, err = capsys.readouterr()
        assert rc == 0
        assert "good lesson" in out
        assert "unreadable" in err.lower() or "warning" in err.lower()


class TestTraceListSurvivesACorruptFile:
    def test_one_bad_trace_does_not_hide_the_others(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        tdir = store / "memory" / "traces"
        main([
            "capture", "--title", "good trace", "--context", "c", "--solution", "s",
            "--agent-type", "code", "--dest", str(store),
        ])
        _corrupt(tdir / "trace_broken.md")
        capsys.readouterr()

        rc = main(["trace", "list", "--dest", str(store)])
        out, err = capsys.readouterr()
        assert rc == 0
        assert "good trace" in out
        assert "unreadable" in err.lower() or "warning" in err.lower()


class TestCommonsRecurringFailuresSurvivesACorruptFile:
    def test_signing_this_store_skips_the_bad_file(self, store, capsys):
        from commontrace.commands import commons_cmd

        main(["init", "--agent-type", "code", "--dest", str(store)])
        tdir = store / "memory" / "traces"
        main([
            "capture", "--title", "recurring thing", "--context", "c", "--solution", "s",
            "--agent-type", "code", "--repeated-error", "--dest", str(store),
        ])
        _corrupt(tdir / "trace_broken.md")

        failures = commons_cmd.build_signatures(str(store))
        assert len(failures) == 1


class TestReliabilityReportSurvivesACorruptFile:
    def test_report_still_runs_past_a_bad_lesson(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        ldir = store / "memory" / "lessons"
        _corrupt(ldir / "lesson_broken.md")
        capsys.readouterr()

        rc = main(["reliability", "--dest", str(store)])
        assert rc in (0, 1)  # must complete, not crash
        assert "Traceback" not in capsys.readouterr().err
