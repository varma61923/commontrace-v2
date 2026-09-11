"""What the fleet was running, as one named, immutable thing.

A lesson has a slug (mutable) and its text has a revision. Neither answered
"what was the fleet running on Monday", which is the question rollback,
attribution and atomic promotion all turn out to be.

The tests below are mostly about the two ways a release abstraction
silently becomes useless: recording a set that is not what the fleet is
actually running, and letting a rollback put back something that is not
what was there.
"""
from __future__ import annotations

import datetime
import os

import pytest

from commontrace import frontmatter, lesson_io, paths, release


def _lesson(root, slug: str, *, rule: str = "Do the thing.", status: str = "active") -> str:
    ldir = paths.lessons_dir(str(root))
    os.makedirs(ldir, exist_ok=True)
    path = os.path.join(ldir, f"lesson_{slug}.md")
    lesson_io.write_lesson(
        path, {"name": slug, "status": status},
        f"## Rule\n{rule}\n## Why\nw\n## How to apply\nh\n## Counter-examples\nc\n",
        root=str(root), actor="cli:test", reason="seed",
    )
    return path


def _set_status(root, slug: str, status: str) -> None:
    path = lesson_io.lesson_path(str(root), slug)
    fm, body = frontmatter.read(path)
    fm["status"] = status
    lesson_io.write_lesson(path, fm, body, root=str(root), actor="cli:test", reason="status")


def _cut(root, **kwargs) -> release.Release:
    return release.cut(str(root), base_id=release.current_id(str(root)), **kwargs)


class TestWhatARelesaseRecords:
    def test_it_pins_every_active_lesson_to_its_revision(self, tmp_path):
        _lesson(tmp_path, "alpha")
        _lesson(tmp_path, "beta")
        cut = _cut(tmp_path, actor="cli:alice", reason="first")

        assert cut.slugs == ("alpha", "beta")
        for entry in cut.entries:
            path = lesson_io.lesson_path(str(tmp_path), entry.slug)
            assert entry.revision == lesson_io.current_revision(path)

    def test_lessons_under_review_are_not_in_it(self, tmp_path):
        """A release records what the fleet is RUNNING. A candidate awaiting
        review is not running."""
        _lesson(tmp_path, "live")
        _lesson(tmp_path, "candidate", status="review")
        assert _cut(tmp_path).slugs == ("live",)

    def test_an_unreadable_lesson_refuses_the_release(self, tmp_path):
        """Quietly omitting it would record a set the fleet is not running,
        which is worse than refusing to record anything."""
        _lesson(tmp_path, "fine")
        broken = os.path.join(paths.lessons_dir(str(tmp_path)), "lesson_broken.md")
        with open(broken, "w", encoding="utf-8") as fh:
            fh.write("---\nthis: is: not: yaml\n---\nbody\n")
        with pytest.raises(release.ReleaseError, match="cannot read"):
            _cut(tmp_path)

    def test_the_id_is_content_addressed_over_the_pinned_set(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path)
        assert cut.release_id == release.compute_id(cut.parent_id, cut.entries)

    def test_the_same_set_from_a_different_base_is_a_different_release(self, tmp_path):
        """The id is a position in a history, not a description of a set:
        two deployments that happen to agree about the outcome are still two
        deployments."""
        _lesson(tmp_path, "alpha")
        entries = release.active_entries(str(tmp_path))
        assert release.compute_id("base-one", entries) != release.compute_id(
            "base-two", entries
        )

    def test_the_first_release_has_a_named_genesis_parent(self, tmp_path):
        """An empty parent would let this chain be spliced onto any other."""
        _lesson(tmp_path, "alpha")
        assert _cut(tmp_path).parent_id == release._RELEASE_GENESIS


class TestStaleBaseRejection:
    def test_cutting_from_a_base_the_store_has_moved_past_is_refused(self, tmp_path):
        """Two curators, each approving a different lesson, each cutting from
        what they saw. The thing lost is a deployment decision, not a text
        edit."""
        _lesson(tmp_path, "alpha")
        first = _cut(tmp_path)

        _lesson(tmp_path, "beta")
        _cut(tmp_path)  # somebody else got there first

        with pytest.raises(release.StaleBaseError) as excinfo:
            release.cut(str(tmp_path), base_id=first.release_id)
        assert excinfo.value.expected == first.release_id

    def test_the_refusal_names_both_ids(self, tmp_path):
        """A conflict a caller cannot see the shape of is one they resolve by
        retrying blindly."""
        _lesson(tmp_path, "alpha")
        stale = _cut(tmp_path).release_id
        _lesson(tmp_path, "beta")
        current = _cut(tmp_path).release_id

        with pytest.raises(release.StaleBaseError) as excinfo:
            release.cut(str(tmp_path), base_id=stale)
        assert excinfo.value.actual == current

    def test_cutting_from_the_current_base_succeeds(self, tmp_path):
        _lesson(tmp_path, "alpha")
        _cut(tmp_path)
        _lesson(tmp_path, "beta")
        assert _cut(tmp_path).slugs == ("alpha", "beta")


class TestTheDiff:
    def test_a_rewritten_lesson_is_its_own_category(self, tmp_path):
        """"We changed what this rule says" and "we swapped one rule for
        another" are different deployments, and a diff that renders them
        identically is the one an operator misreads at 3am."""
        path = _lesson(tmp_path, "alpha", rule="Original.")
        before = _cut(tmp_path)

        fm, _ = frontmatter.read(path)
        lesson_io.write_lesson(
            path, fm, "## Rule\nRewritten.\n## Why\nw\n## How to apply\nh\n"
            "## Counter-examples\nc\n",
            root=str(tmp_path), actor="cli:test", reason="edit",
        )
        after = _cut(tmp_path)

        changes = release.diff(before, after)
        assert changes.changed and not changes.added and not changes.removed
        slug, old, new = changes.changed[0]
        assert slug == "alpha" and old != new

    def test_added_and_removed_are_reported_separately(self, tmp_path):
        _lesson(tmp_path, "keep")
        _lesson(tmp_path, "drop")
        before = _cut(tmp_path)

        _set_status(tmp_path, "drop", "review")
        _lesson(tmp_path, "new")
        after = _cut(tmp_path)

        changes = release.diff(before, after)
        assert [e.slug for e in changes.added] == ["new"]
        assert [e.slug for e in changes.removed] == ["drop"]

    def test_diffing_against_nothing_reports_everything_as_added(self, tmp_path):
        _lesson(tmp_path, "alpha")
        assert len(release.diff(None, _cut(tmp_path)).added) == 1

    def test_an_unchanged_set_diffs_empty(self, tmp_path):
        _lesson(tmp_path, "alpha")
        first = _cut(tmp_path)
        assert release.diff(first, first).empty


class TestRollback:
    def test_the_plan_is_computed_without_changing_anything(self, tmp_path):
        _lesson(tmp_path, "alpha")
        target = _cut(tmp_path)
        _set_status(tmp_path, "alpha", "review")
        _cut(tmp_path)

        plan = release.plan_rollback(str(tmp_path), target)
        assert plan.reactivate == ("alpha",)
        # Nothing moved.
        fm, _ = frontmatter.read(lesson_io.lesson_path(str(tmp_path), "alpha"))
        assert fm["status"] == "review"

    def test_applying_it_restores_the_set(self, tmp_path):
        _lesson(tmp_path, "alpha")
        _lesson(tmp_path, "beta")
        target = _cut(tmp_path)

        _set_status(tmp_path, "beta", "review")
        _cut(tmp_path)

        plan = release.plan_rollback(str(tmp_path), target)
        release.apply_rollback(str(tmp_path), plan, actor="cli:test")
        assert {e.slug for e in release.active_entries(str(tmp_path))} == {"alpha", "beta"}

    def test_a_rewritten_lesson_cannot_be_restored_by_flipping_a_status(self, tmp_path):
        """THE case this exists for. The target release pinned a revision the
        store no longer holds; reactivating the current text would put back a
        DIFFERENT rule under the same name."""
        path = _lesson(tmp_path, "alpha", rule="Original.")
        target = _cut(tmp_path)

        fm, _ = frontmatter.read(path)
        lesson_io.write_lesson(
            path, fm, "## Rule\nSomething else entirely.\n## Why\nw\n"
            "## How to apply\nh\n## Counter-examples\nc\n",
            root=str(tmp_path), actor="cli:test", reason="rewrite",
        )
        _cut(tmp_path)

        plan = release.plan_rollback(str(tmp_path), target)
        assert not plan.clean
        assert plan.unrestorable[0][0] == "alpha"

    def test_an_unclean_rollback_is_refused_by_default(self, tmp_path):
        path = _lesson(tmp_path, "alpha", rule="Original.")
        target = _cut(tmp_path)
        fm, _ = frontmatter.read(path)
        lesson_io.write_lesson(
            path, fm, "## Rule\nChanged.\n## Why\nw\n## How to apply\nh\n"
            "## Counter-examples\nc\n",
            root=str(tmp_path), actor="cli:test", reason="rewrite",
        )
        plan = release.plan_rollback(str(tmp_path), target)
        with pytest.raises(release.ReleaseError, match="DIFFERENT rule"):
            release.apply_rollback(str(tmp_path), plan)

    def test_a_partial_rollback_proceeds_when_asked_knowingly(self, tmp_path):
        path = _lesson(tmp_path, "alpha", rule="Original.")
        _lesson(tmp_path, "beta")
        target = _cut(tmp_path)

        fm, _ = frontmatter.read(path)
        lesson_io.write_lesson(
            path, fm, "## Rule\nChanged.\n## Why\nw\n## How to apply\nh\n"
            "## Counter-examples\nc\n",
            root=str(tmp_path), actor="cli:test", reason="rewrite",
        )
        _set_status(tmp_path, "beta", "review")

        plan = release.plan_rollback(str(tmp_path), target)
        cut = release.apply_rollback(str(tmp_path), plan, allow_partial=True)
        assert "partial" in cut.reason
        assert "beta" in {e.slug for e in release.active_entries(str(tmp_path))}

    def test_rolling_back_appends_rather_than_rewinds(self, tmp_path):
        """History is append-only: returning to an earlier state is itself a
        deployment. A log that could be rewound would lose the single fact
        everyone asks about afterwards -- that a rollback happened."""
        _lesson(tmp_path, "alpha")
        target = _cut(tmp_path)
        _set_status(tmp_path, "alpha", "review")
        _cut(tmp_path)

        plan = release.plan_rollback(str(tmp_path), target)
        recorded = release.apply_rollback(str(tmp_path), plan)

        history = release.read_all(str(tmp_path))
        assert len(history) == 3
        assert history[-1].release_id == recorded.release_id
        assert "rollback" in history[-1].reason


class TestTheLog:
    def test_releases_round_trip_through_the_log(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path, actor="cli:alice", reason="because")
        [stored] = release.read_all(str(tmp_path))
        assert stored == cut

    def test_a_corrupt_line_does_not_lose_the_rest_of_the_history(self, tmp_path):
        """A damaged history is worth more than no history, and the gap is
        visible in the parent chain."""
        _lesson(tmp_path, "alpha")
        _cut(tmp_path)
        with open(release.releases_log_path(str(tmp_path)), "a", encoding="utf-8") as fh:
            fh.write("{not json at all\n")
        _lesson(tmp_path, "beta")
        _cut(tmp_path)
        assert len(release.read_all(str(tmp_path))) == 2

    def test_find_accepts_an_unambiguous_prefix(self, tmp_path):
        _lesson(tmp_path, "alpha")
        cut = _cut(tmp_path)
        assert release.find(str(tmp_path), cut.release_id[:10]) == cut

    def test_find_refuses_an_ambiguous_prefix(self, tmp_path):
        """Acting on the wrong release is worse than being asked to type
        more."""
        _lesson(tmp_path, "alpha")
        first = _cut(tmp_path)
        _lesson(tmp_path, "beta")
        second = _cut(tmp_path)
        # Force a collision by writing a record whose id shares a prefix.
        import json
        forged = second.to_dict()
        forged["release_id"] = first.release_id[:6] + "0" * 58
        with open(release.releases_log_path(str(tmp_path)), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(forged) + "\n")
        with pytest.raises(release.ReleaseError, match="matches"):
            release.find(str(tmp_path), first.release_id[:6])

    def test_current_id_is_the_genesis_before_anything_is_cut(self, tmp_path):
        assert release.current_id(str(tmp_path)) == release._RELEASE_GENESIS

    def test_a_release_records_who_and_when(self, tmp_path):
        _lesson(tmp_path, "alpha")
        moment = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
        cut = release.cut(
            str(tmp_path), base_id=release.current_id(str(tmp_path)),
            actor="cli:alice", reason="ship it", now=moment,
        )
        assert cut.actor == "cli:alice"
        assert cut.reason == "ship it"
        assert cut.created_at == moment.isoformat()
