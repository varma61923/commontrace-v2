"""commontrace/measure.py: causal measurement of memory held by another store.

The claim being tested is end to end, so the most important test here is
too: a fake external store in which one memory genuinely raises the task
success rate and another does nothing, run through CausalMemory and then
through the SAME analysis path `commontrace experiment` uses. If that path
cannot tell the two apart, nothing else in this file matters.
"""
from __future__ import annotations

import json
import random

import pytest

from commontrace import experiment, holdout_io
from commontrace.commands import experiment_cmd
from commontrace.measure import CausalMemory, content_revision


class FakeStore:
    """Returns every memory it holds, in rank order, as dicts shaped like a
    typical memory API's results (`id` + `memory`)."""

    def __init__(self, memories):
        self.memories = memories  # list of dicts
        self.calls = 0

    def search(self, query, **kwargs):
        self.calls += 1
        return [dict(m) for m in self.memories]


def _log_rows(root):
    path = holdout_io.holdout_log_path(str(root))
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def _analyze(root):
    """Exactly the CLI's path: load, scope to the current salt, collapse to
    observations, estimate."""
    rows, _rate, _corrupt = experiment_cmd._load(str(root))
    rows, _salt, _excluded = experiment_cmd.scope_to_current_salt(str(root), rows)
    return {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}


class TestEndToEnd:
    def test_a_memory_that_helps_is_found_and_one_that_does_not_is_not(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore([
            {"id": "good", "memory": "set an idempotency key on webhook handlers"},
            {"id": "neutral", "memory": "the office is closed on Fridays"},
        ])
        memory = CausalMemory(store.search, root=str(tmp_path))
        rng = random.Random(1234)

        for i in range(400):
            occasion = f"task-{i}"
            delivered = {m["id"] for m in memory.recall("webhook fired twice", occasion_id=occasion)}
            # The ground truth this test plants: only `good` changes outcomes.
            p = 0.8 if "good" in delivered else 0.4
            memory.record_outcome(occasion, succeeded=rng.random() < p)

        effects = _analyze(tmp_path)
        assert effects["good"].verdict == experiment.VERDICT_HELPS
        assert effects["good"].effect > 0.2
        assert effects["neutral"].verdict != experiment.VERDICT_HELPS

    def test_every_eligible_memory_is_logged_in_both_arms(self, tmp_path):
        """The withheld arm is the evidence. A wrapper that logged only what
        it delivered would have no control group at all."""
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore([{"id": "m1", "memory": "a"}])
        memory = CausalMemory(store.search, root=str(tmp_path))
        delivered = 0
        for i in range(60):
            delivered += len(memory.recall("q", occasion_id=f"o{i}"))
        rows = _log_rows(tmp_path)
        assert len(rows) == 60
        withheld = sum(1 for r in rows if not r["injected"])
        assert 0 < withheld < 60
        assert delivered == 60 - withheld


class TestWhatIsDelivered:
    def test_order_is_preserved_and_only_withheld_items_are_removed(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.5)
        ids = [f"m{i}" for i in range(12)]
        store = FakeStore([{"id": i, "memory": i} for i in ids])
        memory = CausalMemory(store.search, root=str(tmp_path))
        out = [m["id"] for m in memory.recall("q", occasion_id="o1")]
        withheld = {r["lesson"] for r in _log_rows(tmp_path) if not r["injected"]}
        assert out == [i for i in ids if i not in withheld]

    def test_pinned_memories_are_always_delivered_and_never_logged(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.99)
        store = FakeStore([{"id": "proven", "memory": "x"}, {"id": "new", "memory": "y"}])
        memory = CausalMemory(store.search, root=str(tmp_path), pinned=["proven"])
        for i in range(20):
            assert "proven" in {m["id"] for m in memory.recall("q", occasion_id=f"o{i}")}
        assert {r["lesson"] for r in _log_rows(tmp_path)} == {"new"}

    def test_a_duplicate_result_is_one_observation(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore([{"id": "m", "memory": "a"}, {"id": "m", "memory": "a"}])
        memory = CausalMemory(store.search, root=str(tmp_path))
        out = memory.recall("q", occasion_id="o1")
        assert len(_log_rows(tmp_path)) == 1
        # Both copies share one arm, so they are delivered or withheld together.
        assert len(out) in (0, 2)

    def test_a_stopped_experiment_passes_everything_through_and_logs_nothing(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.0)
        store = FakeStore([{"id": "m", "memory": "a"}])
        memory = CausalMemory(store.search, root=str(tmp_path))
        assert len(memory.recall("q", occasion_id="o1")) == 1
        assert not (tmp_path / "memory" / "holdout_log.jsonl").exists()

    def test_kwargs_reach_the_wrapped_store(self, tmp_path):
        seen = {}

        def search(query, **kwargs):
            seen.update(kwargs, query=query)
            return []

        CausalMemory(search, root=str(tmp_path)).recall("q", occasion_id="o", user_id="u1", limit=3)
        assert seen == {"query": "q", "user_id": "u1", "limit": 3}


class TestIdentity:
    def test_an_item_without_an_id_is_refused_not_guessed(self, tmp_path):
        class Opaque:
            pass

        memory = CausalMemory(lambda q: [Opaque()], root=str(tmp_path))
        with pytest.raises(TypeError, match="key="):
            memory.recall("q", occasion_id="o1")

    def test_a_custom_key_is_used(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore([{"uuid": "abc", "memory": "a"}])
        memory = CausalMemory(store.search, root=str(tmp_path), key=lambda m: m["uuid"])
        memory.recall("q", occasion_id="o1")
        assert _log_rows(tmp_path)[0]["lesson"] == "abc"

    def test_objects_with_attributes_work(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.5)

        class Item:
            def __init__(self, key, value):
                self.key, self.value = key, value

        memory = CausalMemory(lambda q: [Item("k1", "text")], root=str(tmp_path))
        memory.recall("q", occasion_id="o1")
        row = _log_rows(tmp_path)[0]
        assert row["lesson"] == "k1"
        assert row["revision"] == content_revision("text")


class TestRevisions:
    def test_an_in_place_update_is_a_different_revision(self, tmp_path):
        """A store that rewrites a memory under the same id must not have
        both versions pooled as one treatment."""
        holdout_io.configure(str(tmp_path), rate=0.5)
        store = FakeStore([{"id": "m", "memory": "old advice"}])
        memory = CausalMemory(store.search, root=str(tmp_path))
        memory.recall("q", occasion_id="o1")
        store.memories[0]["memory"] = "corrected advice"
        memory.recall("q", occasion_id="o2")
        revs = [r["revision"] for r in _log_rows(tmp_path)]
        assert revs[0] != revs[1]
        assert None not in revs

    def test_no_text_means_unknown_revision_not_a_guess(self, tmp_path):
        holdout_io.configure(str(tmp_path), rate=0.5)
        memory = CausalMemory(lambda q: [{"id": "m"}], root=str(tmp_path))
        memory.recall("q", occasion_id="o1")
        assert _log_rows(tmp_path)[0]["revision"] is None


class TestOutcomes:
    def test_recorded_outcomes_reach_the_analysis(self, tmp_path):
        holdout_io.record_outcome(str(tmp_path), "o1", True)
        holdout_io.record_outcome(str(tmp_path), "o2", False)
        assert experiment_cmd._outcomes_by_occasion(str(tmp_path)) == {"o1": True, "o2": False}

    def test_a_repeated_identical_report_is_harmless(self, tmp_path):
        assert holdout_io.record_outcome(str(tmp_path), "o1", True) is True
        assert holdout_io.record_outcome(str(tmp_path), "o1", True) is False
        lines = (tmp_path / "memory" / "occasion_outcomes.jsonl").read_text().splitlines()
        assert len(lines) == 1

    def test_a_contradicting_report_is_refused(self, tmp_path):
        holdout_io.record_outcome(str(tmp_path), "o1", True)
        with pytest.raises(holdout_io.ConflictingOutcome):
            holdout_io.record_outcome(str(tmp_path), "o1", False)
        assert holdout_io.read_outcomes(str(tmp_path)) == {"o1": True}

    @pytest.mark.parametrize("bad", [1, 0, "yes", None])
    def test_only_a_real_bool_is_accepted(self, tmp_path, bad):
        with pytest.raises(TypeError):
            holdout_io.record_outcome(str(tmp_path), "o1", bad)

    def test_a_torn_line_does_not_lose_the_others(self, tmp_path):
        holdout_io.record_outcome(str(tmp_path), "o1", True)
        path = tmp_path / "memory" / "occasion_outcomes.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"occasion_id": "o2", "succ')
        holdout_io.record_outcome(str(tmp_path), "o3", False)
        assert holdout_io.read_outcomes(str(tmp_path)) == {"o1": True, "o3": False}


class TestTornWritesToTheAssignmentLog:
    def test_a_torn_assignment_line_does_not_take_the_next_one_with_it(self, tmp_path):
        """Same repair, on the log the estimate is computed from -- where a
        lost row removes one arm's observation from a randomized comparison."""
        root = str(tmp_path)
        holdout_io.assign_and_log(root, ["a"], occasion_id="o1", rate=0.5, salt="s")
        with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
            fh.write('{"occasion_id": "o2", "less')
        holdout_io.assign_and_log(root, ["b"], occasion_id="o3", rate=0.5, salt="s")
        records, corrupt = holdout_io.read_log(root)
        assert [(r.occasion_id, r.lesson) for r in records] == [("o1", "a"), ("o3", "b")]
        assert corrupt == 1
