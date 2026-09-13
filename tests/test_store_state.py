"""The four reasons a query comes back empty, told apart.

Walking a cold start end to end, a new user hits three dead ends before
anything works, and until `commontrace/store_state.py` existed all three
printed the same sentence -- one whose advice ("try `lesson list`") shows
an empty list in exactly the cases where the user is most lost.

These tests pin the DISCRIMINATION, not the wording. A message that is
merely friendlier is worth nothing if it says "no lessons yet" to a store
that has lessons; being confidently wrong about where someone is standing
is worse than the terse message it replaced. So each test asserts that the
right case was selected and that the command it names is the one that
actually moves that store forward.
"""
from __future__ import annotations

import os

import pytest

from commontrace import store_state


def _store(tmp_path, *, traces=(), lessons=()):
    """A store on disk, shaped like `commontrace init` leaves it.

    Includes the README and lesson_template that `init` writes, because
    both were miscounted by the first version of this module and the
    fixture is where that stays fixed.
    """
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "memory", "traces"), exist_ok=True)
    os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
    with open(os.path.join(root, "memory", "traces", "README.md"), "w") as fh:
        fh.write("# Traces\nExplanatory scaffolding written by `commontrace init`.\n")
    with open(os.path.join(root, "memory", "lessons", "lesson_template.md"), "w") as fh:
        fh.write("---\nname: lesson-slug\nstatus: review\n---\n# template\n")
    for i, title in enumerate(traces):
        with open(os.path.join(root, "memory", "traces", f"2026-01-0{i+1}_t{i}.md"), "w") as fh:
            fh.write(f"---\ntitle: {title}\n---\nbody\n")
    for slug, status in lessons:
        with open(os.path.join(root, "memory", "lessons", f"{slug}.md"), "w") as fh:
            fh.write(f"---\nname: {slug}\nstatus: {status}\ndescription: d\n---\n# rule\n")
    return root


class TestItCountsWhatTheUserActuallyPut_There:
    def test_a_freshly_initialised_store_has_no_traces(self, tmp_path):
        """`init` writes traces/README.md. Counting it reported "1 trace"
        for a store nobody had captured into -- which turns "you are at the
        beginning" into "something is wrong with what you captured"."""
        state = store_state.inspect(_store(tmp_path))
        assert state.traces == 0

    def test_the_shipped_lesson_template_is_not_a_lesson(self, tmp_path):
        """Counted, it would make "this store has no lessons" impossible to
        ever be true, so the one case that most needs saying could never be
        said."""
        state = store_state.inspect(_store(tmp_path))
        assert state.lessons == 0

    def test_real_lessons_are_read_and_bucketed_by_status(self, tmp_path):
        """The regression for the bug that made this module's first version
        useless: `frontmatter.read` returns a (dict, body) TUPLE, so
        `fm.get(...)` raised AttributeError on every lesson -- and a blanket
        `except Exception: continue` swallowed it, leaving the module to
        report "no lessons yet" for a store full of them. Confidently wrong,
        which is worse than the crash the handler was defending against."""
        root = _store(tmp_path, lessons=[("lesson_a", "active"), ("lesson_b", "review")])
        state = store_state.inspect(root)
        assert state.lessons == 2
        assert state.active == 1
        assert state.review == 1
        assert state.review_slugs == ["lesson_b"]

    def test_a_malformed_lesson_is_skipped_rather_than_raising(self, tmp_path):
        """A diagnostic that crashes fails at exactly the moment the user is
        already confused. Store health belongs to `commontrace doctor`."""
        root = _store(tmp_path, lessons=[("lesson_ok", "active")])
        with open(os.path.join(root, "memory", "lessons", "lesson_bad.md"), "w") as fh:
            fh.write("no frontmatter here at all\n")
        state = store_state.inspect(root)
        assert state.active == 1

    def test_a_missing_store_does_not_raise(self, tmp_path):
        state = store_state.inspect(str(tmp_path / "nonexistent"))
        assert state.traces == 0 and state.lessons == 0


class TestItNamesTheRightNextCommand:
    def test_an_empty_store_is_told_to_capture_or_write(self, tmp_path):
        msg = store_state.why_no_results(_store(tmp_path))
        assert "empty" in msg
        assert "commontrace capture" in msg
        assert "commontrace lesson new" in msg

    def test_traces_without_lessons_explains_that_query_ranks_lessons(self, tmp_path):
        """The most important of the four. The user did the thing the
        product told them to do -- captured a trace -- and got nothing back,
        because `query` ranks lessons and a trace is not one."""
        msg = store_state.why_no_results(_store(tmp_path, traces=["a", "b"]))
        assert "2 traces" in msg
        assert "LESSONS, not traces" in msg
        assert "commontrace distill" in msg

    def test_it_offers_the_direct_path_when_distill_cannot_help(self, tmp_path):
        """One trace can never distill -- distillation needs a repeat -- so
        pointing only at `distill` would be a loop. The direct path has to
        be named too."""
        msg = store_state.why_no_results(_store(tmp_path, traces=["only one"]))
        assert "1 trace captured" in msg
        assert "commontrace lesson new" in msg

    def test_review_only_lessons_name_the_actual_blocker_and_the_slug(self, tmp_path):
        """The third dead end, and the least guessable: the lesson exists,
        the user wrote it, and `query` still ignores it. The message has to
        name the real slug, because `approve <slug>` is the whole fix."""
        root = _store(tmp_path, lessons=[("lesson_mine", "review")])
        msg = store_state.why_no_results(root)
        assert "status=review" in msg
        assert "only returns ACTIVE" in msg
        assert "commontrace lesson approve lesson_mine" in msg

    def test_a_genuine_miss_says_so_rather_than_blaming_the_pipeline(self, tmp_path):
        """The one case where "nothing matched" is the honest answer. If
        this said "no lessons yet" it would send someone to re-create a
        lesson they already have active."""
        root = _store(tmp_path, lessons=[("lesson_a", "active")])
        msg = store_state.why_no_results(root)
        assert "no match among 1 active lesson" in msg
        assert "relevance-floor" in msg

    def test_the_four_cases_are_mutually_exclusive(self, tmp_path):
        """Each store shape selects exactly one message. Overlapping advice
        is how a user ends up running the wrong command confidently."""
        shapes = {
            "empty": _store(tmp_path / "a"),
            "traces": _store(tmp_path / "b", traces=["x"]),
            "review": _store(tmp_path / "c", lessons=[("lesson_r", "review")]),
            "active": _store(tmp_path / "d", lessons=[("lesson_a", "active")]),
        }
        messages = {k: store_state.why_no_results(v) for k, v in shapes.items()}
        assert len(set(messages.values())) == 4

    @pytest.mark.parametrize("caller", ["query", "retrieve"])
    def test_the_message_names_the_caller_that_found_nothing(self, tmp_path, caller):
        """`serve`'s MCP tool is called `retrieve`, not `query`; telling an
        agent operator that "`query` ranks lessons" when they called
        `retrieve` sends them looking for the wrong command."""
        msg = store_state.why_no_results(_store(tmp_path, traces=["x"]), searched=caller)
        assert f"`{caller}` ranks LESSONS" in msg


class TestTheMcpRetrieveToolMakesTheSameDistinction:
    """`retrieve` is what an agent actually calls, so the same confusion
    costs more there: a CLI user can go read the docs, an agent just gets a
    JSON `note` and acts on it.

    That surface already told "no active lessons" apart from "nothing
    matched", which the CLI did not -- but it collapsed the three
    no-active-lessons states into "`capture` your work, then
    `propose_lessons` once a pattern repeats". For a store whose lessons are
    all at status=review that is a loop with no exit: the operator has
    already captured and already proposed, and more of either changes
    nothing.
    """

    def test_review_pending_names_approval_rather_than_more_capturing(self, tmp_path):
        from commontrace import mcp_server

        root = _store(tmp_path, traces=["a", "b"], lessons=[("lesson_mine", "review")])
        note = mcp_server._no_active_lessons_note(root)
        assert "status=review" in note
        assert "lesson approve" in note
        assert "propose_lessons" not in note, (
            "telling an operator who already has a proposed lesson to propose again "
            "is the loop this exists to break"
        )

    def test_traces_without_lessons_points_at_proposing(self, tmp_path):
        from commontrace import mcp_server

        note = mcp_server._no_active_lessons_note(_store(tmp_path, traces=["a"]))
        assert "propose_lessons" in note
        assert "1 trace" in note

    def test_an_empty_store_says_so(self, tmp_path):
        from commontrace import mcp_server

        note = mcp_server._no_active_lessons_note(_store(tmp_path))
        assert "empty" in note

    def test_the_note_is_plain_prose_for_a_json_field(self, tmp_path):
        """It ships inside a JSON payload an agent reads, not to a terminal,
        so the CLI's "[commontrace]" prefix and indented command block would
        be noise in the wrong medium."""
        note = __import__(
            "commontrace.mcp_server", fromlist=["x"]
        )._no_active_lessons_note(_store(tmp_path, traces=["a"]))
        assert not note.startswith("[commontrace]")
        assert "\n" not in note


class TestDoctorAnswersWhyRetrievalIsEmpty:
    """`doctor` is where someone goes when the product is misbehaving.

    It checked the ENVIRONMENT -- Python version, importable deps, files on
    disk -- and reported a wall of green OKs to a store that could not
    serve a single retrieval, because "lessons in store: 1 found" counts
    lessons at any status and `query` only ranks ACTIVE ones. The one
    question the tool was actually being run to answer went unanswered.
    """

    def _doctor(self, root, capsys):
        from commontrace.cli import main

        main(["doctor", "--dest", str(root)])
        return capsys.readouterr().out

    def test_a_store_with_no_active_lessons_is_flagged_not_passed(self, tmp_path, capsys):
        root = _store(tmp_path, lessons=[("lesson_mine", "review")])
        out = self._doctor(root, capsys)
        assert "retrieval ready" in out
        assert "[WARN]" in out

    def test_the_flag_carries_the_counts_that_explain_it(self, tmp_path, capsys):
        """"0 active, 1 at review" is the whole diagnosis in one line --
        a bare WARN would just relocate the confusion."""
        root = _store(tmp_path, traces=["a"], lessons=[("lesson_mine", "review")])
        out = self._doctor(root, capsys)
        assert "0 active" in out
        assert "1 at review" in out

    def test_it_prints_the_same_next_step_query_would(self, tmp_path, capsys):
        """Two tools disagreeing about the fix is worse than one staying
        quiet, so doctor defers to the same diagnosis `query` gives."""
        root = _store(tmp_path, lessons=[("lesson_mine", "review")])
        out = self._doctor(root, capsys)
        assert "commontrace lesson approve lesson_mine" in out

    def test_a_healthy_store_passes_without_the_extra_advice(self, tmp_path, capsys):
        """Advice printed at a store that is working is noise, and noise is
        how the message that does matter gets skipped."""
        root = _store(tmp_path, lessons=[("lesson_a", "active")])
        out = self._doctor(root, capsys)
        assert "1 active" in out
        assert "lesson approve" not in out

    def test_a_brand_new_store_is_not_warned_at_twice(self, tmp_path, capsys):
        """"Nothing here yet" is the CORRECT state of a fresh install, not a
        problem, and "lessons in store - 0 found" already says it once.

        The first version of this check warned unconditionally, which added
        a second alarm to a store that had done nothing wrong -- and
        flattened "you have not started" back together with "you started
        and it is stuck", which is the distinction this whole diagnosis
        exists to draw. tests/test_doctor.py caught it by asserting a fresh
        install produces exactly one warning; this pins the reasoning next
        to the code that has to keep it true.
        """
        out = self._doctor(_store(tmp_path), capsys)
        warns = [ln for ln in out.splitlines() if ln.startswith("[WARN]")]
        assert not any("retrieval ready" in ln for ln in warns), (
            f"a fresh store was warned at for being fresh: {warns}"
        )

    def test_a_store_with_traces_but_nothing_active_IS_warned(self, tmp_path, capsys):
        """The other side of that line: this user did the work and it is not
        serving. Staying quiet here would be the original bug."""
        out = self._doctor(_store(tmp_path, traces=["a", "b"]), capsys)
        warns = [ln for ln in out.splitlines() if ln.startswith("[WARN]")]
        assert any("retrieval ready" in ln for ln in warns), (
            f"a stuck store was not flagged: {warns}"
        )
