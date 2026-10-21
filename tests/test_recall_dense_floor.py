"""Facts recall must inject what is relevant, not whatever clears a score that suits one embedding model."""
from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from unimem import hierarchical, recall


def _pool(related: dict[str, float], *, baseline: float, noise: float, n: int, seed: int = 7):
    rng = random.Random(seed)
    scored = [(SimpleNamespace(id=f"noise-{i}"), baseline + rng.uniform(-noise, noise)) for i in range(n)]
    scored += [(SimpleNamespace(id=name), cosine) for name, cosine in related.items()]
    rng.shuffle(scored)
    return scored


@pytest.mark.parametrize("baseline, hit", [
    (0.50, 0.82),  # bge-small: unrelated text scores about 0.45-0.55
    (0.12, 0.62),  # arctic: unrelated text scores about 0.1
    (0.30, 0.75),
])
def test_only_the_outlier_is_kept_whatever_the_models_baseline(baseline, hit):
    chosen = recall._dense_select(_pool({"answer": hit}, baseline=baseline, noise=0.04, n=30), k=12)
    assert list(chosen) == ["answer"]


def test_several_genuinely_relevant_facts_are_all_kept():
    chosen = recall._dense_select(
        _pool({"a": 0.80, "b": 0.78, "c": 0.76}, baseline=0.50, noise=0.04, n=40), k=12)
    assert set(chosen) == {"a", "b", "c"}


def test_when_nothing_stands_out_nothing_is_returned():
    # The best score of a noisy pool is only the largest of the noise; it must not be injected.
    assert recall._dense_select(_pool({}, baseline=0.50, noise=0.04, n=40), k=12) == {}


def test_a_tight_pool_of_near_identical_facts_does_not_all_pass():
    scored = [(SimpleNamespace(id=f"t-{i}"), 0.60 + i * 0.0001) for i in range(30)]
    assert len(recall._dense_select(scored, k=12)) < 30  # the minimum spread keeps the floor above the median


def test_tiny_pools_use_the_band_and_never_return_non_positive_scores():
    pool = [(SimpleNamespace(id="a"), 0.90), (SimpleNamespace(id="b"), 0.85), (SimpleNamespace(id="c"), 0.40),
            (SimpleNamespace(id="d"), -0.1)]
    assert set(recall._dense_select(pool, k=12)) == {"a", "b"}
    assert recall._dense_select([(SimpleNamespace(id="x"), 0.0)], k=5) == {}
    assert recall._dense_select([], k=5) == {}


def test_k_still_bounds_the_result():
    chosen = recall._dense_select(_pool({f"r{i}": 0.80 - i * 0.001 for i in range(6)}, baseline=0.5, noise=0.04, n=30), k=3)
    assert len(chosen) == 3


def test_a_fact_that_shares_only_function_words_with_the_question_is_not_injected(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_FACT_EMBEDDER", "none")  # lexical arm only
    root = str(tmp_path)
    for statement in ("The billing service is owned by Alice Chen",
                      "Release 4.2 shipped with the new retry policy",
                      "The office coffee machine is descaled on the first Monday of each month"):
        hierarchical.add_fact(root, statement)
    question = "what should I do when the billing database fails over?"
    scored = hierarchical.search_facts(root, question, limit=10)
    assert len(scored) == 3  # the legacy overlap scorer does rank all three: "the" matches each
    items = recall._facts(root, question, None, 10)
    assert [item.text for item in items] == ["The billing service is owned by Alice Chen"]


def test_fact_cli_exposes_record_time_queries(tmp_path, capsys, monkeypatch):
    """`--known-at` answers "what did the store believe then?": a fact added later is not known earlier."""
    import argparse

    from unimem.commands import fact_cmd

    monkeypatch.setenv("UNIMEM_FACT_EMBEDDER", "none")
    root = str(tmp_path)
    hierarchical.add_fact(root, "The staging cluster runs on Nomad")
    base = {"dest": root, "scope": "", "category": "", "as_of": "", "status": "active", "limit": 5, "scorer": "overlap-v1"}
    assert fact_cmd.run_list(argparse.Namespace(**base, known_at="2000-01-01")) == 0
    assert "No matching facts" in capsys.readouterr().out
    assert fact_cmd.run_list(argparse.Namespace(**base, known_at="2100-01-01")) == 0
    assert "staging cluster" in capsys.readouterr().out
    assert fact_cmd.run_search(argparse.Namespace(**base, query="staging cluster Nomad", known_at="2000-01-01")) == 0
    assert "No facts found" in capsys.readouterr().out
    assert fact_cmd.run_search(argparse.Namespace(**base, query="staging cluster Nomad", known_at="2100-01-01")) == 0
    assert "staging cluster" in capsys.readouterr().out
