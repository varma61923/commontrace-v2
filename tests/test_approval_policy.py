"""Separation of duties on the one action with fleet-wide blast radius.

An `active` lesson is injected into every later retrieval verbatim.
protocol/PROTOCOL.md puts a Validator between proposing and activating for
that reason, and the gate enforced everything about WHAT was being
activated -- no scaffolding, valid schema, no secrets or injection payloads
-- while leaving WHO entirely open. The same actor could draft a lesson and
approve it a second later, which is the case the Validator role exists for.

These tests cover the policy that closes it, and equally the promise that
it changes nothing until a store opts in: a control that breaks existing
stores on upgrade is a control that gets reverted.
"""
from __future__ import annotations

import argparse
import os

import pytest

from commontrace import approval, frontmatter, lesson_io, paths
from commontrace.commands import lesson_cmd


def _write_policy(root, text: str) -> None:
    os.makedirs(paths.memory_dir(str(root)), exist_ok=True)
    with open(approval.policy_path(str(root)), "w", encoding="utf-8") as fh:
        fh.write(text)


def _draft(root, slug: str, *, actor: str, body: str | None = None) -> str:
    """Write a lesson through the journaling path, so it has an author."""
    ldir = paths.lessons_dir(str(root))
    os.makedirs(ldir, exist_ok=True)
    path = os.path.join(ldir, f"lesson_{slug}.md")
    fm = {"name": slug, "status": "review"}
    text = body or (
        "## Rule\nCheck the suppression list first.\n"
        "## Why\nBounced addresses are auto-suppressed.\n"
        "## How to apply\nLook it up, clear it, retry.\n"
        "## Counter-examples\nNot when the link merely expired.\n"
    )
    lesson_io.write_lesson(path, fm, text, root=str(root), actor=actor, reason="drafted")
    return path


def _approve(root, slug: str, *, force: bool = False) -> int:
    args = argparse.Namespace(slug=slug, rationale="", force=force, dest=str(root))
    return lesson_cmd.run_approve(args)


class TestTheDefaultIsUnchanged:
    def test_no_policy_file_means_single_approver(self, tmp_path):
        assert approval.load_policy(str(tmp_path)) == approval.ApprovalPolicy(
            mode=approval.POLICY_SINGLE, require_human=False
        )

    def test_an_author_may_still_approve_their_own_lesson(self, tmp_path, monkeypatch):
        """The behaviour this product shipped with. An upgrade that silently
        started refusing it would break every existing store."""
        monkeypatch.setattr(lesson_cmd, "_actor", lambda: "cli:alice")
        path = _draft(tmp_path, "solo", actor="cli:alice")
        assert _approve(tmp_path, "solo") == 0
        assert frontmatter.read(path)[0]["status"] == "active"


class TestTwoPersonApproval:
    POLICY = "mode: two-person\n"

    def test_the_author_cannot_approve_their_own_lesson(self, tmp_path, monkeypatch):
        _write_policy(tmp_path, self.POLICY)
        monkeypatch.setattr(lesson_cmd, "_actor", lambda: "cli:alice")
        path = _draft(tmp_path, "mine", actor="cli:alice")
        assert _approve(tmp_path, "mine") == 1
        assert frontmatter.read(path)[0]["status"] == "review"

    def test_a_different_actor_may_approve_it(self, tmp_path, monkeypatch):
        _write_policy(tmp_path, self.POLICY)
        path = _draft(tmp_path, "theirs", actor="cli:alice")
        monkeypatch.setattr(lesson_cmd, "_actor", lambda: "cli:bob")
        assert _approve(tmp_path, "theirs") == 0
        assert frontmatter.read(path)[0]["status"] == "active"

    def test_every_editor_counts_as_an_author_not_just_the_first(
        self, tmp_path, monkeypatch
    ):
        """Conservative on purpose: a rule that had to judge which edits were
        "substantive" would be a rule an author could route around by making
        their real change look trivial."""
        _write_policy(tmp_path, self.POLICY)
        _draft(tmp_path, "shared", actor="cli:alice")
        _draft(tmp_path, "shared", actor="cli:bob",
               body="## Rule\nDifferent rule entirely.\n## Why\nw\n"
                    "## How to apply\nh\n## Counter-examples\nc\n")
        monkeypatch.setattr(lesson_cmd, "_actor", lambda: "cli:bob")
        assert _approve(tmp_path, "shared") == 1

    def test_force_does_not_override_separation_of_duties(self, tmp_path, monkeypatch):
        """--force exists for an author who judged a content warning a false
        positive. That is precisely the judgement this policy says this
        person may not make, so the flag must not reach it."""
        _write_policy(tmp_path, self.POLICY)
        monkeypatch.setattr(lesson_cmd, "_actor", lambda: "cli:alice")
        _draft(tmp_path, "forced", actor="cli:alice")
        assert _approve(tmp_path, "forced", force=True) == 1

    def test_a_lesson_with_no_recorded_authorship_is_refused(self, tmp_path, monkeypatch):
        """A lesson that appeared on disk with no journal entry is exactly
        the case a separation-of-duties rule should decline to certify: it
        cannot tell whether the approver wrote it."""
        _write_policy(tmp_path, self.POLICY)
        ldir = paths.lessons_dir(str(tmp_path))
        os.makedirs(ldir, exist_ok=True)
        # Written straight through frontmatter, bypassing the journal.
        frontmatter.write(
            os.path.join(ldir, "lesson_orphan.md"),
            {"name": "orphan", "status": "review"},
            "## Rule\nr\n## Why\nw\n## How to apply\nh\n## Counter-examples\nc\n",
        )
        monkeypatch.setattr(lesson_cmd, "_actor", lambda: "cli:alice")
        assert _approve(tmp_path, "orphan") == 1


class TestRequireHuman:
    def test_an_agent_actor_cannot_approve(self, tmp_path):
        policy = approval.ApprovalPolicy(require_human=True)
        with pytest.raises(approval.ApprovalDenied, match="requires a human"):
            approval.check(policy, slug="x", approver="mcp:agent", authors=("cli:bob",))

    def test_a_human_actor_still_can(self, tmp_path):
        policy = approval.ApprovalPolicy(require_human=True)
        approval.check(policy, slug="x", approver="cli:alice", authors=("cli:bob",))

    def test_it_is_independent_of_separation(self, tmp_path):
        """require_human refuses an agent even when the agent did not write
        the lesson -- the two switches answer different questions."""
        policy = approval.ApprovalPolicy(require_human=True)
        with pytest.raises(approval.ApprovalDenied):
            approval.check(policy, slug="x", approver="mcp:reviewer", authors=())


class TestThePolicyFileItself:
    def test_an_unknown_mode_is_refused_loudly(self, tmp_path):
        """A malformed policy that silently fell back to `single` would
        disable a control the operator believes is on."""
        _write_policy(tmp_path, "mode: whatever\n")
        with pytest.raises(approval.PolicyError, match="mode must be one of"):
            approval.load_policy(str(tmp_path))

    def test_comments_and_blank_lines_are_ignored(self, tmp_path):
        _write_policy(tmp_path, "# how we review\n\nmode: two-person  # opt in\n")
        assert approval.load_policy(str(tmp_path)).separation_required

    def test_require_human_accepts_yaml_style_booleans(self, tmp_path):
        _write_policy(tmp_path, "require_human: true\n")
        assert approval.load_policy(str(tmp_path)).require_human is True

    def test_a_malformed_line_is_refused(self, tmp_path):
        _write_policy(tmp_path, "two-person\n")
        with pytest.raises(approval.PolicyError, match="expected"):
            approval.load_policy(str(tmp_path))

    def test_unrecognized_policy_keys_raise_policy_error(self, tmp_path):
        _write_policy(tmp_path, "require-human: true\n")
        with pytest.raises(approval.PolicyError, match="unrecognized policy key"):
            approval.load_policy(str(tmp_path))



class TestAuthorsOf:
    def test_authors_come_from_the_revision_journal_oldest_first(self, tmp_path):
        _draft(tmp_path, "hist", actor="cli:alice")
        _draft(tmp_path, "hist", actor="mcp:agent",
               body="## Rule\nchanged\n## Why\nw\n## How to apply\nh\n"
                    "## Counter-examples\nc\n")
        assert approval.authors_of(str(tmp_path), "hist") == ("cli:alice", "mcp:agent")

    def test_unknown_actors_are_not_counted_as_authors(self, tmp_path):
        """`write_lesson` records "unknown" when no actor was supplied.
        Treating that as a person would let an unattributed edit satisfy a
        separation rule."""
        _draft(tmp_path, "anon", actor="")
        assert approval.authors_of(str(tmp_path), "anon") == ()

    def test_a_lesson_nobody_journaled_has_no_authors(self, tmp_path):
        assert approval.authors_of(str(tmp_path), "never-written") == ()
