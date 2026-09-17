"""Tests for lesson_io.content_as_of and `commontrace lesson history --as-of`
-- point-in-time reconstruction of what a lesson actually said.

Before this, the revision journal recorded a before/after HASH on every
change, enough to detect that a lesson changed mid-experiment but not to
answer what it actually said on a given date, because the prior text was
never kept (see commontrace/environments.py's own docstring, which named
this as separate, larger work before this closed it).
"""
from __future__ import annotations

import os

import pytest

from commontrace import lesson_io, templates


def _lesson_fm(applies_when="original condition") -> dict:
    return templates.lesson_frontmatter(
        slug="lesson_x", description="A test lesson.", agent_type="code",
        domain="net", tags=["retry"], applies_when=applies_when,
        do_not_apply_when="never", importance=3,
    )


@pytest.fixture
def root(tmp_path):
    os.makedirs(str(tmp_path / "memory" / "lessons"), exist_ok=True)
    return str(tmp_path)


@pytest.fixture
def path(root):
    return os.path.join(root, "memory", "lessons", "lesson_x.md")


class TestContentAsOf:
    def test_a_date_after_the_only_write_returns_the_current_content(self, root, path):
        fm = _lesson_fm("original condition")
        lesson_io.write_lesson(path, fm, "## Rule\nOriginal.\n", root=root)

        got_fm, got_body = lesson_io.content_as_of(root, "lesson_x", "2099-01-01")
        assert got_fm["applies_when"] == "original condition"
        assert "Original" in got_body

    def test_a_date_between_two_edits_returns_the_version_that_was_live_then(self, root, path):
        import time

        fm = _lesson_fm("original condition")
        lesson_io.write_lesson(path, fm, "## Rule\nOriginal.\n", root=root,
                               actor="a", reason="first")
        time.sleep(0.01)
        midpoint = _now_iso()
        time.sleep(0.01)

        fm["applies_when"] = "tightened condition"
        lesson_io.write_lesson(path, fm, "## Rule\nTightened.\n", root=root,
                               actor="a", reason="second")

        got_fm, got_body = lesson_io.content_as_of(root, "lesson_x", midpoint)
        assert got_fm["applies_when"] == "original condition"
        assert "Original" in got_body

        # And the current content is still the latest version.
        current_fm, current_body = lesson_io.content_as_of(root, "lesson_x", _now_iso())
        assert current_fm["applies_when"] == "tightened condition"

    def test_three_versions_each_date_resolves_to_the_right_one(self, root, path):
        import time

        fm = _lesson_fm("v1")
        lesson_io.write_lesson(path, fm, "## Rule\nV1.\n", root=root)
        time.sleep(0.01)
        t_after_v1 = _now_iso()
        time.sleep(0.01)

        fm["applies_when"] = "v2"
        lesson_io.write_lesson(path, fm, "## Rule\nV2.\n", root=root)
        time.sleep(0.01)
        t_after_v2 = _now_iso()
        time.sleep(0.01)

        fm["applies_when"] = "v3"
        lesson_io.write_lesson(path, fm, "## Rule\nV3.\n", root=root)

        assert lesson_io.content_as_of(root, "lesson_x", t_after_v1)[0]["applies_when"] == "v1"
        assert lesson_io.content_as_of(root, "lesson_x", t_after_v2)[0]["applies_when"] == "v2"
        assert lesson_io.content_as_of(root, "lesson_x", _now_iso())[0]["applies_when"] == "v3"

    def test_a_bare_yyyy_mm_dd_date_is_accepted(self, root, path):
        fm = _lesson_fm()
        lesson_io.write_lesson(path, fm, "## Rule\nOriginal.\n", root=root)
        got_fm, _got_body = lesson_io.content_as_of(root, "lesson_x", "2099-01-01")
        assert got_fm["applies_when"] == "original condition"

    def test_an_unparseable_date_raises_a_clear_error(self, root, path):
        lesson_io.write_lesson(path, _lesson_fm(), "## Rule\nOriginal.\n", root=root)
        with pytest.raises(lesson_io.ContentAsOfError, match="could not parse"):
            lesson_io.content_as_of(root, "lesson_x", "not-a-date")

    def test_a_missing_lesson_raises_a_clear_error(self, root):
        with pytest.raises(lesson_io.ContentAsOfError, match="no lesson found"):
            lesson_io.content_as_of(root, "lesson_missing", "2026-01-01")

    def test_a_date_before_the_lessons_creation_raises_rather_than_guessing(self, root, path):
        """The lesson did not exist yet at this point -- returning the
        current (or any) content would be fabricating a fact, exactly what
        `plan_rollback` already refuses to do for the hash-pinned case."""
        lesson_io.write_lesson(path, _lesson_fm(), "## Rule\nOriginal.\n", root=root)
        with pytest.raises(lesson_io.ContentAsOfError):
            lesson_io.content_as_of(root, "lesson_x", "2000-01-01")

    def test_a_write_that_changes_nothing_does_not_create_a_reconstructable_boundary(
        self, root, path
    ):
        """Bookkeeping-only writes (status, uses) are not journaled at all
        (see commontrace/lesson_io.py's own docstring) -- content_as_of
        must not be tripped up by their absence."""
        fm = _lesson_fm()
        lesson_io.write_lesson(path, fm, "## Rule\nOriginal.\n", root=root)
        fm["status"] = "active"
        fm["uses"] = 5
        lesson_io.write_lesson(path, fm, "## Rule\nOriginal.\n", root=root)

        got_fm, _body = lesson_io.content_as_of(root, "lesson_x", "2099-01-01")
        assert got_fm["applies_when"] == "original condition"


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class TestLessonHistoryAsOfCli:
    def test_prints_the_reconstructed_content(self, tmp_path, capsys):
        from commontrace.cli import main

        store = str(tmp_path)
        assert main(["init", "--agent-type", "code", "--dest", store]) == 0
        assert main([
            "lesson", "new", "--slug", "my_rule", "--description", "d",
            "--domain", "net", "--applies-when", "when X", "--dest", store,
        ]) == 0
        capsys.readouterr()

        rc = main(["lesson", "history", "my_rule", "--as-of", "2099-01-01", "--dest", store])
        assert rc == 0
        out = capsys.readouterr().out
        assert "when X" in out

    def test_an_unparseable_as_of_exits_nonzero_with_a_clear_message(self, tmp_path, capsys):
        from commontrace.cli import main

        store = str(tmp_path)
        main(["init", "--agent-type", "code", "--dest", store])
        main([
            "lesson", "new", "--slug", "my_rule", "--description", "d",
            "--domain", "net", "--dest", store,
        ])
        capsys.readouterr()

        rc = main(["lesson", "history", "my_rule", "--as-of", "garbage", "--dest", store])
        assert rc == 1
        assert "could not parse" in capsys.readouterr().err

    def test_without_as_of_the_ordinary_change_list_still_prints(self, tmp_path, capsys):
        from commontrace.cli import main

        store = str(tmp_path)
        main(["init", "--agent-type", "code", "--dest", store])
        main([
            "lesson", "new", "--slug", "my_rule", "--description", "d",
            "--domain", "net", "--dest", store,
        ])
        capsys.readouterr()

        rc = main(["lesson", "history", "my_rule", "--dest", store])
        assert rc == 0
        assert "recorded change" in capsys.readouterr().out
