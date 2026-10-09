"""Graduation: a memory proven to help stops being randomized and is always delivered."""
from __future__ import annotations

import json
import random

import pytest

from benchmarks import causalmembench
from commontrace import holdout_io
from commontrace.measure import CausalMemory, HarmWatch

ITEMS = [{"id": "good", "memory": "check the idempotency key"}, {"id": "plain", "memory": "say hello"}]


def _seed(root, n=300):
    config = holdout_io.configure(str(root), rate=0.5, salt="graduation-tests")
    rng = random.Random(5)
    for i in range(n):
        withheld = holdout_io.assign_and_log(str(root), ["good", "plain"], occasion_id=f"seed-{i}",
                                             rate=0.5, salt=config.salt)
        p = 0.35 + (0.4 if "good" not in withheld else 0.0)
        holdout_io.record_outcome(str(root), f"seed-{i}", rng.random() < p)


def _logged(root):
    with open(holdout_io.holdout_log_path(str(root)), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_a_proven_memory_graduates_and_is_never_withheld_again(tmp_path):
    _seed(tmp_path)
    before = len(_logged(tmp_path))
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path), graduate=True)
    results = [memory.recall_detailed("q", occasion_id=f"live-{n}") for n in range(40)]
    assert all("good" in [i["id"] for i in r.items] for r in results)
    assert results[0].graduated["good"]["verdict"] == "HELPS"
    new = _logged(tmp_path)[before:]
    assert new and all(row["lesson"] == "plain" for row in new)  # only the unproven memory is randomized


def test_without_graduation_the_proven_memory_keeps_its_holdout(tmp_path):
    _seed(tmp_path)
    memory = CausalMemory(lambda q, **kw: ITEMS, root=str(tmp_path))
    results = [memory.recall_detailed("q", occasion_id=f"live-{n}") for n in range(60)]
    assert any("good" not in [i["id"] for i in r.items] for r in results)
    assert all(r.graduated == {} for r in results)


def test_a_shared_watch_must_agree_on_graduation(tmp_path):
    watch = HarmWatch(str(tmp_path))
    with pytest.raises(ValueError, match="graduate"):
        CausalMemory(lambda q: [], root=str(tmp_path), harm_watch=watch, graduate=True)


def test_causalmembench_is_reproducible_and_scores_the_oracle_perfectly():
    a = causalmembench.run(["correlational", "oracle", "commontrace@0.5+graduate"], seeds=1, occasions=600,
                           lag=10, missing=0.0)
    b = causalmembench.run(["correlational", "oracle", "commontrace@0.5+graduate"], seeds=1, occasions=600,
                           lag=10, missing=0.0)
    assert a["results"] == b["results"]
    oracle = a["results"]["oracle"]
    assert oracle["harmful_deliveries"]["mean"] == 0 and oracle["regret_vs_oracle"]["mean"] == 0
    assert "CausalMemBench" in causalmembench.render(a)


@pytest.mark.parametrize("spec", ["nope", "commontrace@2", "commontrace@x", "commontrace-0.3"])
def test_causalmembench_rejects_bad_policies(spec):
    with pytest.raises((ValueError, ImportError)):
        causalmembench.load_policy(spec)


def test_causalmembench_accepts_an_external_policy_factory():
    class AlwaysAll:
        def __init__(self, memories):
            pass

        def deliver(self, occasion_id, eligible):
            return list(eligible)

        def observe(self, occasion_id, succeeded):
            pass

        def withdrawn(self):
            return set()

    one = causalmembench.run_one(AlwaysAll, causalmembench.default_memories(), seed=0, occasions=200, lag=0,
                                 missing=0.0)
    assert one["false_withdrawals"] == [] and one["harmful_deliveries"] > 0
