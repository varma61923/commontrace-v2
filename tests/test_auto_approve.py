"""`commontrace lesson auto-approve`: LLM drafts activated only through
the normal approval gates, only with a started holdout, and the holdout
cannot then be stopped while an auto-approved lesson is active."""
from __future__ import annotations

import os

import pytest

from commontrace import approval, frontmatter, lesson_io, paths
from commontrace.cli import main

COMPLETE_BODY = (
    "## Rule\nPersist the provider's event id before any side effect.\n\n"
    "## Why\nProviders redeliver webhooks.\n\n"
    "## How to apply\nCheck the stored id at the top of the handler.\n\n"
    "## Counter-examples\nHandlers with no side effects.\n"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "code", "--dest", str(tmp_path)])
    return tmp_path


def _policy(store, text):
    with open(approval.policy_path(str(store)), "w", encoding="utf-8") as fh:
        fh.write(text)


def _draft(store, slug, *, body=COMPLETE_BODY, llm=True):
    fm = {
        "name": slug, "status": "review", "description": f"draft {slug}",
        "applies_when": "a webhook may be redelivered", "do_not_apply_when": "the handler is pure",
        "importance": 3, "importance_rationale": "fixture", "tags": [], "agent_type": "code",
        "domain": "webhooks", "uses": 0, "last_hit": "NEVER",
    }
    if llm:
        fm["llm_draft"] = {"provider": "anthropic", "model": "m", "prompt_sha256": "x"}
    path = os.path.join(paths.lessons_dir(str(store)), f"lesson_{slug}.md")
    lesson_io.write_lesson(path, fm, body, root=str(store), actor="distill", reason="fixture")
    return path


def _status(path):
    return frontmatter.read(path)[0]


class TestPolicy:
    def test_parses_and_defaults_off(self, store):
        assert approval.load_policy(str(store)).auto_approve_drafts is False
        _policy(store, "auto_approve_drafts: true\n")
        assert approval.load_policy(str(store)).auto_approve_drafts is True

    def test_contradicts_require_human(self, store):
        _policy(store, "require_human: true\nauto_approve_drafts: true\n")
        with pytest.raises(approval.PolicyError, match="contradict"):
            approval.load_policy(str(store))

    def test_a_non_boolean_is_refused(self, store):
        _policy(store, "auto_approve_drafts: maybe\n")
        with pytest.raises(approval.PolicyError):
            approval.load_policy(str(store))


class TestAutoApprove:
    def test_refuses_when_the_policy_is_off(self, store, capsys):
        path = _draft(store, "d1")
        assert main(["lesson", "auto-approve", "--dest", str(store)]) == 1
        assert "auto-approval is off" in capsys.readouterr().err
        assert _status(path)["status"] == "review"

    def test_refuses_without_a_started_holdout(self, store, capsys):
        _policy(store, "auto_approve_drafts: true\n")
        path = _draft(store, "d1")
        assert main(["lesson", "auto-approve", "--dest", str(store)]) == 1
        assert "started holdout" in capsys.readouterr().err
        assert _status(path)["status"] == "review"

    def test_activates_a_complete_llm_draft_and_stamps_it(self, store):
        _policy(store, "auto_approve_drafts: true\n")
        main(["experiment", "--configure", "--rate", "0.1", "--dest", str(store)])
        path = _draft(store, "d1")
        assert main(["lesson", "auto-approve", "--dest", str(store)]) == 0
        fm = _status(path)
        assert fm["status"] == "active"
        assert fm["auto_approved"] is True
        assert approval.AUTO_APPROVER in approval.authors_of(str(store), "d1")

    def test_the_normal_gates_still_refuse_scaffolding(self, store):
        _policy(store, "auto_approve_drafts: true\n")
        main(["experiment", "--configure", "--rate", "0.1", "--dest", str(store)])
        path = _draft(store, "d1", body=COMPLETE_BODY.replace(
            "Handlers with no side effects.", "TODO: cases where the rule does NOT apply."))
        main(["lesson", "auto-approve", "--dest", str(store)])
        assert _status(path)["status"] == "review"

    def test_a_human_written_draft_is_left_alone(self, store):
        _policy(store, "auto_approve_drafts: true\n")
        main(["experiment", "--configure", "--rate", "0.1", "--dest", str(store)])
        path = _draft(store, "d1", llm=False)
        main(["lesson", "auto-approve", "--dest", str(store)])
        assert _status(path)["status"] == "review"


class TestHoldoutStaysOn:
    def test_stopping_the_holdout_is_refused_while_an_auto_approved_lesson_is_active(self, store, capsys):
        _policy(store, "auto_approve_drafts: true\n")
        main(["experiment", "--configure", "--rate", "0.1", "--dest", str(store)])
        _draft(store, "d1")
        main(["lesson", "auto-approve", "--dest", str(store)])
        capsys.readouterr()
        assert main(["experiment", "--configure", "--rate", "0", "--dest", str(store)]) == 1
        assert "auto-approved" in capsys.readouterr().err

    def test_stopping_is_allowed_with_no_auto_approved_lesson(self, store):
        main(["experiment", "--configure", "--rate", "0.1", "--dest", str(store)])
        assert main(["experiment", "--configure", "--rate", "0", "--dest", str(store)]) == 0
