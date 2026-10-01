"""Failure signals recover labelled failure modes (commons/eval/signal_ari.py), and the clustering
behind them is average-linkage, sparse and fast enough for a real store."""
import time

import pytest

from commons.eval import signal_ari as sa
from commontrace import failure_signals as fs


def test_ari_is_one_for_the_same_partition_under_any_naming_and_near_zero_for_unrelated_ones():
    assert sa.adjusted_rand_index(["a", "a", "b", "b"], [1, 1, 2, 2]) == 1.0
    assert sa.adjusted_rand_index(["a", "a", "b", "b"], [2, 2, 1, 1]) == 1.0
    assert sa.adjusted_rand_index(["a", "a", "b", "b"], [1, 2, 1, 2]) < 0
    import random
    rng = random.Random(0)
    true = [i % 4 for i in range(400)]
    shuffled = true[:]
    rng.shuffle(shuffled)
    assert abs(sa.adjusted_rand_index(true, shuffled)) < 0.05


def test_the_dataset_is_deterministic_labelled_and_has_one_offs():
    a, b = sa.generate(3), sa.generate(3)
    assert a == b and a != sa.generate(4)
    labels = [r["label"] for r in a]
    assert all(labels.count(m) == sa.TRACES_PER_MODE for m in sa.MODES)
    assert sum(label.startswith("noise-") for label in labels) == sa.NOISE_TRACES


def test_held_out_seeds_reach_the_target_ari():
    scores = [sa.score_seed(s)["ari"] for s in range(5)]       # 100-104 chose the default; these did not
    assert sum(scores) / len(scores) >= sa.TARGET and min(scores) >= sa.TARGET - 0.1


def test_one_shared_sentence_does_not_chain_two_failure_modes_together():
    shared = "the customer was frustrated and asked for a manager"
    rows = ([f"refund for order {i} is late and the policy page contradicts the email. {shared}" for i in range(6)]
            + [f"sso redirect loop at the identity provider callback cookie dropped {i}. {shared}"
               for i in range(6)])
    occ = [fs.FailureOccurrence(id=f"t{i}", title="Failure", context_text=text, solution_text="", tags=[],
                                agent_type="support", agent_id="", created_at="") for i, text in enumerate(rows)]
    groups = fs._average_linkage(fs._vectors(occ), fs.DEFAULT_SIMILARITY)
    assert sorted(sorted(g) for g in groups) == [list(range(6)), list(range(6, 12))]


def test_a_thousand_failures_cluster_in_seconds():
    rows = sa.generate(0) * 10
    occ = [fs.FailureOccurrence(id=f"t{i}", title=r["title"], context_text=r["context"] + f" run {i}",
                                solution_text="", tags=[], agent_type="support", agent_id="", created_at="")
           for i, r in enumerate(rows)]
    start = time.perf_counter()
    groups = fs._average_linkage(fs._vectors(occ), fs.DEFAULT_SIMILARITY)
    assert time.perf_counter() - start < 20
    assert sum(len(g) for g in groups) == len(occ)


def test_a_zero_threshold_is_one_signal_not_a_crash(tmp_path, monkeypatch):
    from commontrace.cli import main
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    sa._write(str(tmp_path), sa.generate(1)[:6])
    signals, _ = fs.build_signals(str(tmp_path), similarity_threshold=0)
    assert len(signals) == 1 and signals[0].size == 6


@pytest.mark.parametrize("threshold", [0.1, 0.15])
def test_the_chosen_scale_is_not_a_knife_edge(threshold):
    assert sa.score_seed(7, similarity_threshold=threshold)["ari"] >= 0.8
