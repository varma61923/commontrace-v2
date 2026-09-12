"""Named environments a release is promoted to, with scheduled activation
and an approval gate on protected ones (audit 4.2).

What these tests defend, in order of how badly getting it wrong would
hurt:

1. **This is pure record-keeping.** Nothing here is asserted about
   retrieval, because nothing here is supposed to change it -- see
   commontrace/environments.py's own module docstring for why serving a
   frozen release's content is not something this codebase can do
   honestly yet.
2. **Scheduled activation needs no separate flip.** A future promotion
   has no effect until its moment arrives; comparing against `now` is
   the whole mechanism.
3. **The approval gate reuses commontrace/approval.py's own
   separation-of-duties logic exactly** -- not a second, differently-
   behaved copy of it -- and only re-litigates lessons NEWLY entering
   the environment, not everything already running there.
"""
from __future__ import annotations

import datetime
import os

import pytest

from commontrace import approval, environments, lesson_io, paths, release


def _lesson(root, slug: str, *, actor: str = "cli:test", status: str = "active") -> str:
    ldir = paths.lessons_dir(str(root))
    os.makedirs(ldir, exist_ok=True)
    path = os.path.join(ldir, f"lesson_{slug}.md")
    lesson_io.write_lesson(
        path, {"name": slug, "status": status},
        f"## Rule\n{slug}.\n## Why\nw\n## How to apply\nh\n## Counter-examples\nc\n",
        root=str(root), actor=actor, reason="seed",
    )
    return path


def _cut(root, **kwargs) -> release.Release:
    return release.cut(str(root), base_id=release.current_id(str(root)), **kwargs)


def _write_policy(root, text: str) -> None:
    os.makedirs(paths.memory_dir(str(root)), exist_ok=True)
    with open(approval.policy_path(str(root)), "w", encoding="utf-8") as fh:
        fh.write(text)


class TestPromote:
    def test_promoting_an_unknown_environment_is_refused(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        with pytest.raises(environments.EnvironmentError, match="unknown environment"):
            environments.promote(str(tmp_path), cut.release_id, "canary")

    def test_promoting_a_nonexistent_release_is_refused(self, tmp_path):
        with pytest.raises(environments.EnvironmentError, match="no such release"):
            environments.promote(str(tmp_path), "0" * 64, "dev")

    def test_current_reflects_the_promotion(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "dev", actor="cli:alice")
        assert environments.current(str(tmp_path), "dev") == cut.release_id

    def test_an_environment_never_promoted_to_has_no_current_release(self, tmp_path):
        assert environments.current(str(tmp_path), "prod") is None

    def test_re_promoting_replaces_the_current_release(self, tmp_path):
        _lesson(tmp_path, "alpha")
        first = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), first.release_id, "dev", actor="cli:alice")
        _lesson(tmp_path, "beta")
        second = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), second.release_id, "dev", actor="cli:alice")
        assert environments.current(str(tmp_path), "dev") == second.release_id

    def test_promotions_to_different_environments_are_independent(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "dev", actor="cli:alice")
        assert environments.current(str(tmp_path), "stage") is None
        assert environments.current(str(tmp_path), "prod") is None


class TestScheduledActivation:
    def test_a_future_promotion_has_no_effect_yet(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        tomorrow = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        environments.promote(
            str(tmp_path), cut.release_id, "stage", actor="cli:alice", activate_at=tomorrow,
        )
        assert environments.current(str(tmp_path), "stage") is None

    def test_it_takes_effect_once_that_moment_arrives(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        environments.promote(
            str(tmp_path), cut.release_id, "stage", actor="cli:alice", activate_at=soon,
        )
        later = soon + datetime.timedelta(minutes=1)
        assert environments.current(str(tmp_path), "stage", now=later) == cut.release_id

    def test_pending_lists_future_promotions_soonest_first(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        now = datetime.datetime.now(datetime.timezone.utc)
        later = now + datetime.timedelta(days=2)
        sooner = now + datetime.timedelta(days=1)
        environments.promote(
            str(tmp_path), cut.release_id, "stage", actor="cli:alice", activate_at=later,
        )
        environments.promote(
            str(tmp_path), cut.release_id, "stage", actor="cli:alice", activate_at=sooner,
        )
        upcoming = environments.pending(str(tmp_path), "stage", now=now)
        assert [r["activate_at"] for r in upcoming] == [
            sooner.isoformat(), later.isoformat(),
        ]

    def test_pending_excludes_promotions_already_in_effect(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "stage", actor="cli:alice")
        assert environments.pending(str(tmp_path), "stage") == []

    def test_a_past_dated_promotion_takes_effect_immediately(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        yesterday = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        environments.promote(
            str(tmp_path), cut.release_id, "dev", actor="cli:alice", activate_at=yesterday,
        )
        assert environments.current(str(tmp_path), "dev") == cut.release_id


class TestApprovalGate:
    def test_default_policy_lets_anyone_promote_to_prod(self, tmp_path):
        _lesson(tmp_path, "alpha", actor="cli:alice")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "prod", actor="cli:alice")
        assert environments.current(str(tmp_path), "prod") == cut.release_id

    def test_two_person_policy_refuses_the_sole_author_of_a_new_lesson(self, tmp_path):
        _write_policy(tmp_path, "mode: two-person\n")
        _lesson(tmp_path, "alpha", actor="cli:alice")
        cut = _cut(tmp_path, actor="cli:alice")
        with pytest.raises(approval.ApprovalDenied, match="separation of duties"):
            environments.promote(str(tmp_path), cut.release_id, "prod", actor="cli:alice")
        assert environments.current(str(tmp_path), "prod") is None

    def test_two_person_policy_allows_a_different_promoter(self, tmp_path):
        _write_policy(tmp_path, "mode: two-person\n")
        _lesson(tmp_path, "alpha", actor="cli:alice")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "prod", actor="cli:bob")
        assert environments.current(str(tmp_path), "prod") == cut.release_id

    def test_dev_and_stage_are_not_protected_even_under_two_person(self, tmp_path):
        _write_policy(tmp_path, "mode: two-person\n")
        _lesson(tmp_path, "alpha", actor="cli:alice")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "dev", actor="cli:alice")
        assert environments.current(str(tmp_path), "dev") == cut.release_id

    def test_a_lesson_already_running_in_prod_is_not_re_litigated(self, tmp_path):
        """Only lessons NEWLY entering the environment need a second
        author -- one already running there was already reviewed once."""
        _write_policy(tmp_path, "mode: two-person\n")
        _lesson(tmp_path, "alpha", actor="cli:alice")
        first = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), first.release_id, "prod", actor="cli:bob")

        # A second release that keeps alpha (already in prod, written only
        # by alice) and adds beta (written only by bob) -- alice may
        # promote it, because alpha is not NEW to prod and beta was not
        # written by her.
        _lesson(tmp_path, "beta", actor="cli:bob")
        second = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), second.release_id, "prod", actor="cli:alice")
        assert environments.current(str(tmp_path), "prod") == second.release_id

    def test_require_human_refuses_an_agent_actor(self, tmp_path):
        _write_policy(tmp_path, "mode: single\nrequire_human: true\n")
        _lesson(tmp_path, "alpha", actor="mcp:agent-1")
        cut = _cut(tmp_path, actor="mcp:agent-1")
        with pytest.raises(approval.ApprovalDenied, match="requires a human"):
            environments.promote(str(tmp_path), cut.release_id, "prod", actor="mcp:agent-1")

    def test_a_lesson_with_no_recorded_authors_refuses_under_two_person(self, tmp_path):
        """Matches approval.check's own rule for a single lesson: no
        provenance to separate from is a refusal, not a pass."""
        _write_policy(tmp_path, "mode: two-person\n")
        # Written directly, bypassing lesson_io.write_lesson's journal.
        from commontrace import frontmatter

        ldir = paths.lessons_dir(str(tmp_path))
        os.makedirs(ldir, exist_ok=True)
        frontmatter.write(
            os.path.join(ldir, "lesson_orphan.md"), {"name": "orphan", "status": "active"},
            "## Rule\nr.\n## Why\nw\n## How to apply\nh\n## Counter-examples\nc\n",
        )
        cut = _cut(tmp_path, actor="cli:alice")
        with pytest.raises(approval.ApprovalDenied, match="no recorded authorship"):
            environments.promote(str(tmp_path), cut.release_id, "prod", actor="cli:alice")


class TestHistory:
    def test_history_is_scoped_to_one_environment(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "dev", actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "stage", actor="cli:alice")
        dev_history = environments.history(str(tmp_path), "dev")
        assert len(dev_history) == 1
        assert dev_history[0]["environment"] == "dev"

    def test_history_is_oldest_first(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), cut.release_id, "dev", actor="cli:alice", reason="first")
        _lesson(tmp_path, "beta")
        second = _cut(tmp_path, actor="cli:alice")
        environments.promote(str(tmp_path), second.release_id, "dev", actor="cli:alice", reason="second")
        reasons = [r["reason"] for r in environments.history(str(tmp_path), "dev")]
        assert reasons == ["first", "second"]
