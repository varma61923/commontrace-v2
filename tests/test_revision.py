"""Content identity for a lesson -- what an experiment was actually measuring.

The digest's job is to be sensitive to exactly one thing: a change to what an
agent reads. Too sensitive and every run is flagged and nobody reads the
report; not sensitive enough and a rewritten rule slips through as the same
treatment. Both failures are here.
"""
from __future__ import annotations

import pytest

from commontrace import lesson_io, revision, templates


def lesson(**overrides) -> tuple[dict, str]:
    fm = templates.lesson_frontmatter(
        slug="lesson_x", description="Retry with exponential backoff.",
        agent_type="code", domain="net", tags=["retry", "http"],
        applies_when="A request fails with 429.",
        do_not_apply_when="The failure is a 4xx other than 429.",
        importance=4, importance_rationale="Common.",
    )
    body = "## Rule\nRetry with exponential backoff.\n\n## Why\nThe server is shedding load.\n"
    fm.update(overrides.pop("fm", {}))
    return fm, overrides.pop("body", body)


class TestWhatCountsAsTheSameTreatment:
    @pytest.mark.parametrize("field,value", [
        ("uses", 41),
        ("last_hit", "2026-09-02"),
        ("hub_trace_id", "abc-123"),
        ("source_traces", ["t1", "t2"]),
        ("source_episodes", ["e1"]),
        ("importance_history", [{"from": 3, "to": 4}]),
        ("status", "archived"),
        ("name", "lesson_renamed"),
    ])
    def test_bookkeeping_does_not_count_as_a_change(self, field, value):
        """`uses` and `last_hit` move on EVERY retrieval. Hashing them would
        make every lesson look edited constantly, every experiment would be
        flagged, and the check would be noise inside a week."""
        fm, body = lesson()
        before = revision.revision_of(fm, body)
        fm[field] = value
        assert revision.revision_of(fm, body) == before

    @pytest.mark.parametrize("field,value", [
        ("description", "Retry with jitter."),
        ("applies_when", "A request fails with any 5xx."),
        ("do_not_apply_when", "Never."),
        ("importance", 1),
        ("domain", "storage"),
        ("tags", ["retry", "http", "backoff"]),
    ])
    def test_anything_the_agent_reads_counts(self, field, value):
        fm, body = lesson()
        before = revision.revision_of(fm, body)
        fm[field] = value
        assert revision.revision_of(fm, body) != before

    def test_a_changed_rule_counts(self):
        fm, body = lesson()
        before = revision.revision_of(fm, body)
        assert revision.revision_of(fm, body.replace("exponential", "linear")) != before

    @pytest.mark.parametrize("mutate", [
        lambda b: b + "\n\n\n",
        lambda b: b.replace("\n", "  \n"),
        lambda b: b.replace("\n\n", "\n\n\n\n"),
        lambda b: b.replace("\n", "\r\n"),
        lambda b: "\n" + b,
    ])
    def test_whitespace_does_not_count(self, mutate):
        """A reflowed paragraph is not a different instruction, and flagging
        one as a changed treatment costs the same credibility as missing a
        real change."""
        fm, body = lesson()
        assert revision.revision_of(fm, mutate(body)) == revision.revision_of(fm, body)

    def test_reordered_tags_do_not_count(self):
        fm, body = lesson()
        before = revision.revision_of(fm, body)
        fm["tags"] = list(reversed(fm["tags"]))
        assert revision.revision_of(fm, body) == before

    def test_yaml_key_order_does_not_count(self):
        fm, body = lesson()
        assert revision.revision_of(dict(reversed(list(fm.items()))), body) == \
            revision.revision_of(fm, body)

    def test_it_is_stable_across_calls(self):
        fm, body = lesson()
        assert revision.revision_of(fm, body) == revision.revision_of(fm, body)

    def test_it_is_short_enough_to_print_in_a_report_line(self):
        fm, body = lesson()
        assert len(revision.revision_of(fm, body)) == revision.REVISION_LENGTH <= 16


class TestItCoversWhatTheAgentIsActuallyHanded:
    def test_every_injected_frontmatter_field_is_hashed(self):
        """`mcp_server._lesson_wire` is the surface that hands a lesson to a
        model. A field that reaches an agent but is not in the digest is a
        change to the treatment that the stability check cannot see."""
        from commontrace import mcp_server

        fm, body = lesson()
        wire = mcp_server._lesson_wire(fm, body, include_body=True)
        # Excluded on purpose, each for a reason in revision.py's docstring:
        # identity, telemetry, provenance, lifecycle, and derived fields.
        not_treatment = {
            "slug", "uses", "source_traces", "status", "agent_type",
            "unfilled", "revision", "body", "score", "matched",
        }
        carried = set(wire) - not_treatment
        assert carried <= set(revision.INJECTED_FIELDS), (
            f"reaches the agent but is not in the digest: {carried - set(revision.INJECTED_FIELDS)}"
        )

    def test_the_body_is_hashed(self):
        """The body is where the Rule actually is; a digest over frontmatter
        alone would call a rewritten rule the same treatment."""
        fm, body = lesson()
        assert revision.revision_of(fm, body) != revision.revision_of(fm, "")


class TestTraceRevision:
    def test_it_covers_the_text_a_hub_trace_shows(self):
        base = revision.revision_of_trace("t", "c", "s", ["a"])
        assert revision.revision_of_trace("t2", "c", "s", ["a"]) != base
        assert revision.revision_of_trace("t", "c2", "s", ["a"]) != base
        assert revision.revision_of_trace("t", "c", "s2", ["a"]) != base
        assert revision.revision_of_trace("t", "c", "s", ["b"]) != base

    def test_reordered_tags_and_whitespace_do_not_count(self):
        assert revision.revision_of_trace("t", "c", "s", ["a", "b"]) == \
            revision.revision_of_trace("t ", "c\n\n\n", "s", ["b", "a"])

    def test_it_uses_the_same_digest_length_as_a_lesson(self):
        """A revision that meant one thing on one tier and another on the
        other would make the two integrity reports incomparable."""
        assert len(revision.revision_of_trace("t", "c", "s")) == revision.REVISION_LENGTH


class TestTheRevisionJournal:
    def test_a_content_change_is_recorded_with_who_and_why(self, tmp_path):
        root = str(tmp_path)
        path = str(tmp_path / "memory" / "lessons" / "lesson_x.md")
        __import__("os").makedirs(__import__("os").path.dirname(path), exist_ok=True)
        fm, body = lesson()

        first = lesson_io.write_lesson(path, fm, body, root=root,
                                       actor="cli:alice", reason="initial")
        fm["applies_when"] = "A request fails with any 5xx."
        second = lesson_io.write_lesson(path, fm, body, root=root,
                                        actor="mcp:agent", reason="widened")

        history = lesson_io.history(root, "lesson_x")
        assert [r["to"] for r in history] == [first, second]
        assert history[0]["from"] is None      # a new lesson has no predecessor
        assert history[1]["from"] == first
        assert [r["actor"] for r in history] == ["cli:alice", "mcp:agent"]
        assert history[1]["reason"] == "widened"

    def test_a_write_that_changes_nothing_is_not_recorded(self, tmp_path):
        """Approve sets status, retrieval bumps `uses`, a push stamps
        `hub_trace_id`. Journaling those would bury the changes that matter
        under the ones that do not."""
        import os

        root = str(tmp_path)
        path = str(tmp_path / "memory" / "lessons" / "lesson_x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fm, body = lesson()

        lesson_io.write_lesson(path, fm, body, root=root)
        fm["status"] = "active"
        fm["uses"] = 12
        lesson_io.write_lesson(path, fm, body, root=root)

        assert len(lesson_io.history(root, "lesson_x")) == 1

    def test_history_survives_a_corrupt_line(self, tmp_path):
        import os

        root = str(tmp_path)
        path = str(tmp_path / "memory" / "lessons" / "lesson_x.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lesson_io.write_lesson(path, *lesson(), root=root)

        with open(lesson_io.revisions_log_path(root), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")

        records, corrupt = lesson_io.read_revisions(root)
        # One torn write must not make the rest of a lesson's history
        # unreadable, and the count keeps the loss visible.
        assert len(records) == 1 and corrupt == 1

    def test_an_unwritten_lesson_has_no_history(self, tmp_path):
        assert lesson_io.history(str(tmp_path), "lesson_missing") == []

    @pytest.mark.parametrize("slug", ["../../etc/passwd", "a/b", "..", "lesson name"])
    def test_a_slug_cannot_escape_the_lessons_directory(self, tmp_path, slug):
        """The one guard now shared by the CLI, the MCP server and the
        holdout logger -- a second copy would be a second chance to get one
        of them wrong."""
        assert lesson_io.lesson_path(str(tmp_path), slug) is None
        assert lesson_io.revision_for_slug(str(tmp_path), slug) is None


class TestTheHistoryCommand:
    """An effect size is about a revision, not a slug. This is how someone
    recovers which -- and it is what makes the 'edited mid-run' finding
    actionable rather than merely alarming.
    """

    @staticmethod
    def _cli(*argv):
        import os
        import subprocess
        import sys

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "-m", "commontrace.cli", *argv],
                              capture_output=True, text=True, cwd=repo, check=False)

    def test_it_shows_each_change_in_the_order_it_happened(self, tmp_path):
        import os

        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        path = os.path.join(root, "memory", "lessons", "lesson_x.md")
        fm, body = lesson()
        first = lesson_io.write_lesson(path, fm, body, root=root,
                                       actor="cli:alice", reason="initial")
        fm["applies_when"] = "A request fails with any 5xx."
        second = lesson_io.write_lesson(path, fm, body, root=root,
                                        actor="mcp:agent", reason="widened after new traces")

        result = self._cli("lesson", "history", "lesson_x", "--dest", root)
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert f"Currently at **{second}**" in out
        # Chronological: the earlier change must appear above the later one,
        # so this cross-references against the experiment report's arrow.
        assert out.index(f"(new) -> {first}") < out.index(f"{first} -> {second}")
        assert "cli:alice" in out and "mcp:agent" in out
        assert "widened after new traces" in out

    def test_a_lesson_with_no_recorded_history_is_not_a_missing_lesson(self, tmp_path):
        """Every lesson written before the journal existed is in this state.
        Reporting it as 'no such lesson' would send someone looking for a
        file that is right there."""
        import os

        from commontrace import frontmatter

        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        frontmatter.write(os.path.join(root, "memory", "lessons", "lesson_old.md"), *lesson())

        result = self._cli("lesson", "history", "lesson_old", "--dest", root)
        assert result.returncode == 0
        assert "no recorded history" in result.stdout
        assert "no lesson found" not in result.stderr

    def test_a_genuinely_missing_lesson_fails(self, tmp_path):
        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        result = self._cli("lesson", "history", "lesson_nope", "--dest", root)
        assert result.returncode == 1
        assert "no lesson found" in result.stderr

    def test_curating_through_the_cli_records_history(self, tmp_path):
        """`lesson new` then `lesson approve` is the ordinary path, and it has
        to leave a record without anyone opting in."""
        root = str(tmp_path / "fleet")
        assert self._cli("init", "--dest", root, "--agent-type", "code").returncode == 0
        result = self._cli("lesson", "new", "--slug", "thing", "--description", "A thing.",
                           "--domain", "net", "--dest", root)
        assert result.returncode == 0, result.stderr

        history = lesson_io.history(root, "thing")
        assert len(history) == 1
        assert history[0]["actor"].startswith("cli:")
        assert "lesson new" in history[0]["reason"]
