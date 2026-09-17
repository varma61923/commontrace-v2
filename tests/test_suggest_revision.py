"""Tests for `commontrace lesson suggest-revision` --
commontrace/commands/lesson_cmd.py's run_suggest_revision.

The behavior worth protecting: this drafts a NEW review-status lesson from
a MISCALIBRATED lesson's own retrieval evidence, changes nothing about the
original, and refuses outright for any other verdict -- a HARMFUL lesson's
rule may be wrong, not just its activation condition, and tightening WHEN
it fires does not fix that.
"""
from __future__ import annotations

import os

import pytest
import yaml

from commontrace import frontmatter, lesson_io, paths
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _active_lesson(root: str, slug: str, **fm_extra) -> str:
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = {
        "name": slug, "description": "A lesson under test", "status": "active",
        "importance": 3, "tags": [], "domain": "testing", "agent_type": "code",
        "applies_when": "the situation broadly resembles this one",
        "do_not_apply_when": "n/a",
    }
    fm.update(fm_extra)
    lesson_io.write_lesson(
        path, fm, "## Rule\nDo the thing.\n\n## Why\nBecause.\n\n"
        "## How to apply\nJust do it.\n\n## Counter-examples\nNone.\n",
        root=root, actor="test", reason="fixture",
    )
    return path


def _episode(root: str, name: str, verdict: str, retrieved: str, hit: str,
             task_invocation: str = "") -> None:
    eps = os.path.join(root, "memory", "episodes")
    os.makedirs(eps, exist_ok=True)
    fm = {"name": name, "verdict": verdict, "task_invocation": task_invocation,
          "lessons_retrieved_by_alpha": [retrieved], "lessons_hit": [hit]}
    with open(os.path.join(eps, f"{name}.md"), "w", encoding="utf-8") as fh:
        fh.write("---\n" + yaml.safe_dump(fm) + "---\n\nbody\n")


def _miscalibrated_lesson(store) -> None:
    """A lesson that fires constantly but rarely helps -- MISCALIBRATED per
    reliability.py's own verdict boundary (precision < 0.25, enough
    evidence to clear min_evidence, and lift not below the HARMFUL bar)."""
    main(["init", "--agent-type", "code", "--dest", str(store)])
    _active_lesson(str(store), "broad")
    for i in range(12):
        # 2/12 hit -> precision ~0.17, below the 0.25 MISCALIBRATED cutoff.
        hit = "broad" if i < 2 else "not_this_one"
        _episode(str(store), f"ep{i}", "CONFORM", "broad", hit,
                 task_invocation=f"task about widget {i}")


class TestRefusesEverythingExceptMiscalibrated:
    def test_refuses_a_slug_with_no_lesson_file(self, store, capsys):
        main(["init", "--dest", str(store)])
        rc = main(["lesson", "suggest-revision", "nope", "--dest", str(store)])
        assert rc == 1
        assert "no lesson found" in capsys.readouterr().err

    def test_refuses_when_there_is_no_evidence_at_all(self, store, capsys):
        main(["init", "--dest", str(store)])
        _active_lesson(str(store), "untested")
        rc = main(["lesson", "suggest-revision", "untested", "--dest", str(store)])
        assert rc == 1
        assert "no evidence yet" in capsys.readouterr().err

    def test_refuses_a_reliable_lesson(self, store, capsys):
        main(["init", "--dest", str(store)])
        _active_lesson(str(store), "good")
        for i in range(10):
            _episode(str(store), f"ep{i}", "CONFORM", "good", "good")
        rc = main(["lesson", "suggest-revision", "good", "--dest", str(store)])
        assert rc == 1
        assert "RELIABLE" in capsys.readouterr().err

    def test_refuses_a_harmful_lesson_with_a_pointer_to_reject_instead(self, store, capsys):
        main(["init", "--dest", str(store)])
        _active_lesson(str(store), "bad")
        for i in range(12):
            _episode(str(store), f"ok{i}", "CONFORM", "other", "other")
        for i in range(8):
            _episode(str(store), f"bad{i}", "ABANDON", "bad", "bad")
        rc = main(["lesson", "suggest-revision", "bad", "--dest", str(store)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "HARMFUL" in err
        assert "lesson reject" in err

    def test_no_draft_file_is_written_on_any_refusal(self, store, capsys):
        main(["init", "--dest", str(store)])
        _active_lesson(str(store), "untested")
        main(["lesson", "suggest-revision", "untested", "--dest", str(store)])
        assert not os.path.exists(
            os.path.join(paths.lessons_dir(str(store)), "lesson_untested-revision.md")
        )


class TestDraftingARevision:
    def test_writes_a_new_review_status_draft_and_leaves_the_original_untouched(self, store, capsys):
        _miscalibrated_lesson(store)
        before_fm, before_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_broad.md")
        )
        rc = main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        assert rc == 0

        draft_path = os.path.join(paths.lessons_dir(str(store)), "lesson_broad-revision.md")
        assert os.path.exists(draft_path)
        draft_fm, draft_body = frontmatter.read(draft_path)
        assert draft_fm["status"] == "review"
        assert draft_fm["revises"] == "broad"

        after_fm, after_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_broad.md")
        )
        assert after_fm == before_fm
        assert after_body == before_body

    def test_the_draft_carries_the_todo_marker_so_approval_refuses_it_unedited(self, store, capsys):
        _miscalibrated_lesson(store)
        main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["lesson", "approve", "broad-revision", "--dest", str(store)])
        assert rc == 1
        assert "unedited scaffolding" in capsys.readouterr().err

    def test_the_draft_lists_hit_and_miss_occasions_with_their_task_text(self, store, capsys):
        _miscalibrated_lesson(store)
        main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        _fm, body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_broad-revision.md")
        )
        assert "## Evidence for revision" in body
        assert "Fired and helped" in body
        assert "Fired but did not help" in body
        assert "task about widget 0" in body

    def test_refuses_to_draft_a_second_time_while_one_is_pending(self, store, capsys):
        _miscalibrated_lesson(store)
        assert main(["lesson", "suggest-revision", "broad", "--dest", str(store)]) == 0
        capsys.readouterr()
        rc = main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        assert rc == 1
        assert "already exists" in capsys.readouterr().err

    def test_a_second_draft_is_allowed_after_the_first_is_rejected(self, store, capsys):
        _miscalibrated_lesson(store)
        main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        capsys.readouterr()
        assert main([
            "lesson", "reject", "broad-revision", "--reason", "starting over",
            "--dest", str(store),
        ]) == 0
        rc = main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        assert rc == 0

    def test_the_draft_can_be_approved_once_genuinely_edited(self, store, capsys):
        _miscalibrated_lesson(store)
        main(["lesson", "suggest-revision", "broad", "--dest", str(store)])
        draft_path = os.path.join(paths.lessons_dir(str(store)), "lesson_broad-revision.md")
        fm, body = frontmatter.read(draft_path)
        fm["applies_when"] = "the task is specifically about widget provisioning"
        fm["do_not_apply_when"] = "the task is about anything else"
        lesson_io.write_lesson(draft_path, fm, body, root=str(store), actor="test",
                               reason="tightened by hand")
        capsys.readouterr()
        rc = main([
            "lesson", "approve", "broad-revision", "--force", "--dest", str(store),
        ])
        # --force covers the near-duplicate-with-the-original refusal
        # (redundancy.py), which is expected here since only applies_when
        # changed; the point of this test is that the TODO scaffolding gate
        # itself is satisfied by a genuine edit.
        assert rc == 0
