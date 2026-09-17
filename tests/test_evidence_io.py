"""Tests for evidence_io.uncaptured_retrieval_counts -- a coverage signal
for an occasion a `--experiment` call logged that no episode or trace ever
recorded an outcome for.

The gap: an agent that retrieves under `--experiment` and never calls
`capture` afterward left that occasion invisible everywhere -- a lesson
retrieved constantly but never reported on looked identical to one nobody
ever asked for. The naive fix (write a placeholder "occasion" record a
later capture would complete) collides with
`commontrace/commands/capture_cmd.py`'s own re-capture semantics, which
keep an EXISTING trace's title/context/solution over a new call's -- a
real capture's content would silently lose to a placeholder that arrived
first. Reading the holdout log instead needs no new write path.

This is deliberately NOT fed into `evidence_io.load_evidence`/
`reliability.score_lessons`: that function reads a slug absent from
`Evidence.hit` as a confirmed miss, so folding an UNKNOWN outcome in there
would drag every under-captured lesson's measured precision down for no
reason but under-reporting. It surfaces as its own coverage statistic
instead.
"""
from __future__ import annotations

import os

import pytest

from commontrace import evidence_io, holdout_io


@pytest.fixture
def root(tmp_path):
    os.makedirs(str(tmp_path / "memory"), exist_ok=True)
    return str(tmp_path)


class TestUncapturedRetrievalCounts:
    def test_an_occasion_with_no_capture_is_counted(self, root):
        holdout_io.assign_and_log(root, ["a"], occasion_id="occ-1", rate=0.0, salt="s")
        assert evidence_io.uncaptured_retrieval_counts(root) == {"a": 1}

    def test_a_withheld_lesson_is_not_counted(self, root):
        # rate=1.0 -- everything eligible is withheld, so nothing was shown.
        holdout_io.assign_and_log(root, ["a"], occasion_id="occ-1", rate=1.0, salt="s")
        assert evidence_io.uncaptured_retrieval_counts(root) == {}

    def test_counts_accumulate_across_distinct_occasions(self, root):
        for i in range(10):
            holdout_io.assign_and_log(
                root, ["never_captured"], occasion_id=f"occ-{i}", rate=0.0, salt="s",
            )
        assert evidence_io.uncaptured_retrieval_counts(root) == {"never_captured": 10}

    def test_an_occasion_that_was_also_captured_via_a_trace_is_excluded(self, root):
        """The occasion has a real trace (an outcome IS known) -- it must
        not also count as "uncaptured", which would falsely claim
        under-reporting for an occasion whose outcome is already on
        record, whatever that outcome turned out to be."""
        import yaml

        holdout_io.assign_and_log(root, ["a"], occasion_id="occ-1", rate=0.0, salt="s")
        traces_dir = os.path.join(root, "memory", "traces")
        os.makedirs(traces_dir, exist_ok=True)
        fm = {
            "id": "occ-1", "title": "t", "extensions": {"lessons_retrieved": ["a"],
                                                          "lessons_hit": []},
            "outcome": {"resolved": False},
        }
        with open(os.path.join(traces_dir, "occ-1.md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm) + "---\n\n## Context\nc\n\n## Solution\ns\n")

        assert evidence_io.uncaptured_retrieval_counts(root) == {}

    def test_an_occasion_captured_via_an_episode_is_excluded_too(self, root):
        import yaml

        holdout_io.assign_and_log(root, ["a"], occasion_id="ep-1", rate=0.0, salt="s")
        eps_dir = os.path.join(root, "memory", "episodes")
        os.makedirs(eps_dir, exist_ok=True)
        fm = {"name": "ep-1", "verdict": "CONFORM",
              "lessons_retrieved_by_alpha": ["a"], "lessons_hit": ["a"]}
        with open(os.path.join(eps_dir, "ep-1.md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm) + "---\n\nbody\n")

        assert evidence_io.uncaptured_retrieval_counts(root) == {}

    def test_no_holdout_log_at_all_is_an_empty_dict_not_an_error(self, root):
        assert evidence_io.uncaptured_retrieval_counts(root) == {}

    def test_multiple_lessons_and_occasions_are_each_counted_correctly(self, root):
        holdout_io.assign_and_log(root, ["a", "b"], occasion_id="occ-1", rate=0.0, salt="s")
        holdout_io.assign_and_log(root, ["a"], occasion_id="occ-2", rate=0.0, salt="s")
        assert evidence_io.uncaptured_retrieval_counts(root) == {"a": 2, "b": 1}

    def test_load_evidence_itself_is_unaffected_by_the_holdout_log(self, root):
        """The whole point of keeping these separate: load_evidence's
        output -- what feeds reliability.score_lessons -- must not change
        shape just because a holdout log exists."""
        holdout_io.assign_and_log(root, ["a"], occasion_id="occ-1", rate=0.0, salt="s")
        assert evidence_io.load_evidence(root) == []
