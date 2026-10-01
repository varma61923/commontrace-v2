"""Tests for `commontrace lesson suggest-revision` --
commontrace/commands/lesson_cmd.py's run_suggest_revision.

The behavior worth protecting: this drafts a NEW review-status lesson from
a MISCALIBRATED lesson's own retrieval evidence, changes nothing about the
original, and refuses outright for any other verdict -- a HARMFUL lesson's
rule may be wrong, not just its activation condition, and tightening WHEN
it fires does not fix that.
"""
from __future__ import annotations

import json
import os

import pytest
import yaml

from commontrace import frontmatter, lesson_io, llm, paths
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


def _harmful_lesson(store) -> None:
    """A lesson that fires and correlates with worse outcomes -- HARMFUL
    per reliability.py's own verdict boundary."""
    main(["init", "--agent-type", "code", "--dest", str(store)])
    _active_lesson(str(store), "bad")
    for i in range(12):
        _episode(str(store), f"ok{i}", "CONFORM", "other", "other")
    for i in range(8):
        _episode(str(store), f"bad{i}", "ABANDON", "bad", "bad",
                 task_invocation=f"task about gadget {i}")


_GOOD_DRAFT_JSON = {
    "rule": "Do the thing, but only for widgets.",
    "applies_when": "the task is specifically about widget provisioning",
    "do_not_apply_when": "the task is about anything else",
    "evidence": [],  # filled in per-test with real occasion ids
}


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


class TestDraftWithLLM:
    """`--draft` never bypasses the approval gate (that is still
    lesson_cmd.run_approve's job) -- what it changes is whether the draft
    starts as a real attempt or a "TODO: tighten" placeholder. Both paths
    still write a status=review draft; this class checks the LLM path fills
    it in, and degrades to exactly the pre-`--draft` behavior whenever no
    usable draft comes back.
    """

    def test_llm_assisted_draft_fills_in_applies_when(self, store, monkeypatch, capsys):
        _miscalibrated_lesson(store)
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")

        def fake_call(cfg, prompt):
            payload = dict(_GOOD_DRAFT_JSON, evidence=["ep0", "ep1"])
            return json.dumps(payload), {"input_tokens": 5, "output_tokens": 5}

        monkeypatch.setattr(llm, "_call_anthropic", fake_call)
        rc = main(["lesson", "suggest-revision", "broad", "--draft", "--dest", str(store)])
        assert rc == 0

        draft_fm, _draft_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_broad-revision.md")
        )
        assert draft_fm["applies_when"] == _GOOD_DRAFT_JSON["applies_when"]
        assert draft_fm["do_not_apply_when"] == _GOOD_DRAFT_JSON["do_not_apply_when"]
        assert draft_fm["llm_draft"]["provider"] == "anthropic"
        assert draft_fm["llm_draft"]["cited_evidence"] == ["ep0", "ep1"]
        assert "LLM-assisted" in capsys.readouterr().out

    def test_falls_back_to_todo_scaffold_when_no_llm_is_configured(self, store, monkeypatch, capsys):
        _miscalibrated_lesson(store)
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        rc = main(["lesson", "suggest-revision", "broad", "--draft", "--dest", str(store)])
        assert rc == 0
        assert "falling back to the template scaffold" in capsys.readouterr().err
        draft_fm, _draft_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_broad-revision.md")
        )
        assert draft_fm["applies_when"].startswith("TODO:")
        assert "llm_draft" not in draft_fm

    def test_falls_back_when_the_model_cites_nothing_verifiable(self, store, monkeypatch, capsys):
        _miscalibrated_lesson(store)
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")

        def fake_call(cfg, prompt):
            payload = dict(_GOOD_DRAFT_JSON, evidence=["made-up-occasion"])
            return json.dumps(payload), {}

        monkeypatch.setattr(llm, "_call_anthropic", fake_call)
        rc = main(["lesson", "suggest-revision", "broad", "--draft", "--dest", str(store)])
        assert rc == 0
        assert "LLMDraftRejected" in capsys.readouterr().err
        draft_fm, _draft_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_broad-revision.md")
        )
        assert draft_fm["applies_when"].startswith("TODO:")

    def test_without_the_draft_flag_no_llm_is_called_even_if_configured(self, store, monkeypatch):
        _miscalibrated_lesson(store)
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")

        def boom(cfg, prompt):
            raise AssertionError("the LLM must not be called without --draft")

        monkeypatch.setattr(llm, "_call_anthropic", boom)
        assert main(["lesson", "suggest-revision", "broad", "--dest", str(store)]) == 0


class TestSuggestRewrite:
    def test_refuses_a_non_harmful_lesson(self, store, capsys):
        _miscalibrated_lesson(store)
        rc = main(["lesson", "suggest-rewrite", "broad", "--dest", str(store)])
        assert rc == 1
        assert "not HARMFUL" in capsys.readouterr().err

    def test_writes_a_review_draft_and_leaves_the_original_untouched(self, store, monkeypatch, capsys):
        _harmful_lesson(store)
        monkeypatch.setenv("COMMONTRACE_LLM_API_KEY", "k")

        def fake_call(cfg, prompt):
            payload = dict(_GOOD_DRAFT_JSON, rule="Never do the thing for gadgets.",
                          evidence=["bad0", "bad1"])
            return json.dumps(payload), {}

        monkeypatch.setattr(llm, "_call_anthropic", fake_call)
        before_fm, before_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_bad.md")
        )
        rc = main(["lesson", "suggest-rewrite", "bad", "--dest", str(store)])
        assert rc == 0

        draft_path = os.path.join(paths.lessons_dir(str(store)), "lesson_bad-rewrite.md")
        draft_fm, draft_body = frontmatter.read(draft_path)
        assert draft_fm["status"] == "review"
        assert draft_fm["revises"] == "bad"
        assert "Never do the thing for gadgets." in draft_body

        after_fm, after_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_bad.md")
        )
        assert after_fm == before_fm
        assert after_body == before_body

    def test_falls_back_to_a_todo_rule_without_an_llm(self, store, monkeypatch, capsys):
        _harmful_lesson(store)
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        rc = main(["lesson", "suggest-rewrite", "bad", "--dest", str(store)])
        assert rc == 0
        _draft_fm, draft_body = frontmatter.read(
            os.path.join(paths.lessons_dir(str(store)), "lesson_bad-rewrite.md")
        )
        assert "TODO: rewrite this rule" in draft_body
        assert "no LLM draft was available" in capsys.readouterr().err

    def test_the_draft_is_refused_at_approval_until_edited(self, store, monkeypatch, capsys):
        _harmful_lesson(store)
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        main(["lesson", "suggest-rewrite", "bad", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["lesson", "approve", "bad-rewrite", "--dest", str(store)])
        assert rc == 1
        assert "unedited scaffolding" in capsys.readouterr().err

    def test_refuses_a_second_draft_while_one_is_pending(self, store, monkeypatch, capsys):
        _harmful_lesson(store)
        monkeypatch.delenv("COMMONTRACE_LLM_API_KEY", raising=False)
        assert main(["lesson", "suggest-rewrite", "bad", "--dest", str(store)]) == 0
        capsys.readouterr()
        rc = main(["lesson", "suggest-rewrite", "bad", "--dest", str(store)])
        assert rc == 1
        assert "already exists" in capsys.readouterr().err
