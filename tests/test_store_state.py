from __future__ import annotations

import os

import pytest

from commontrace import store_state


def _store(tmp_path, *, traces=(), lessons=()):
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
        state = store_state.inspect(_store(tmp_path))
        assert state.traces == 0

    def test_the_shipped_lesson_template_is_not_a_lesson(self, tmp_path):
        state = store_state.inspect(_store(tmp_path))
        assert state.lessons == 0

    def test_real_lessons_are_read_and_bucketed_by_status(self, tmp_path):
        root = _store(tmp_path, lessons=[("lesson_a", "active"), ("lesson_b", "review")])
        state = store_state.inspect(root)
        assert state.lessons == 2
        assert state.active == 1
        assert state.review == 1
        assert state.review_slugs == ["lesson_b"]

    def test_a_malformed_lesson_is_skipped_rather_than_raising(self, tmp_path):
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
        msg = store_state.why_no_results(_store(tmp_path, traces=["a", "b"]))
        assert "2 traces" in msg
        assert "LESSONS, not traces" in msg
        assert "commontrace distill" in msg

    def test_it_offers_the_direct_path_when_distill_cannot_help(self, tmp_path):
        msg = store_state.why_no_results(_store(tmp_path, traces=["only one"]))
        assert "1 trace captured" in msg
        assert "commontrace lesson new" in msg

    def test_review_only_lessons_name_the_actual_blocker_and_the_slug(self, tmp_path):
        root = _store(tmp_path, lessons=[("lesson_mine", "review")])
        msg = store_state.why_no_results(root)
        assert "status=review" in msg
        assert "only returns ACTIVE" in msg
        assert "commontrace lesson approve lesson_mine" in msg

    def test_a_genuine_miss_says_so_rather_than_blaming_the_pipeline(self, tmp_path):
        root = _store(tmp_path, lessons=[("lesson_a", "active")])
        msg = store_state.why_no_results(root)
        assert "no match among 1 active lesson" in msg
        assert "relevance-floor" in msg

    def test_the_four_cases_are_mutually_exclusive(self, tmp_path):
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
        msg = store_state.why_no_results(_store(tmp_path, traces=["x"]), searched=caller)
        assert f"`{caller}` ranks LESSONS" in msg


class TestTheMcpRetrieveToolMakesTheSameDistinction:
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
        note = __import__(
            "commontrace.mcp_server", fromlist=["x"]
        )._no_active_lessons_note(_store(tmp_path, traces=["a"]))
        assert not note.startswith("[commontrace]")
        assert "\n" not in note


class TestDoctorAnswersWhyRetrievalIsEmpty:
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
        root = _store(tmp_path, traces=["a"], lessons=[("lesson_mine", "review")])
        out = self._doctor(root, capsys)
        assert "0 active" in out
        assert "1 at review" in out

    def test_it_prints_the_same_next_step_query_would(self, tmp_path, capsys):
        root = _store(tmp_path, lessons=[("lesson_mine", "review")])
        out = self._doctor(root, capsys)
        assert "commontrace lesson approve lesson_mine" in out

    def test_a_healthy_store_passes_without_the_extra_advice(self, tmp_path, capsys):
        root = _store(tmp_path, lessons=[("lesson_a", "active")])
        out = self._doctor(root, capsys)
        assert "1 active" in out
        assert "lesson approve" not in out

    def test_a_brand_new_store_is_not_warned_at_twice(self, tmp_path, capsys):
        out = self._doctor(_store(tmp_path), capsys)
        warns = [ln for ln in out.splitlines() if ln.startswith("[WARN]")]
        assert not any("retrieval ready" in ln for ln in warns), (
            f"a fresh store was warned at for being fresh: {warns}"
        )

    def test_a_store_with_traces_but_nothing_active_IS_warned(self, tmp_path, capsys):
        out = self._doctor(_store(tmp_path, traces=["a", "b"]), capsys)
        warns = [ln for ln in out.splitlines() if ln.startswith("[WARN]")]
        assert any("retrieval ready" in ln for ln in warns), (
            f"a stuck store was not flagged: {warns}"
        )
