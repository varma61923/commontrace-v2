"""Seeded metamorphic tests with independent oracles, without model services.

The seed is a pytest parameter so a failing generated corpus is reproducible.
These check behavior across a family of inputs, rather than reimplementing the
optimized posting-list, graph-index, or statistical calculation paths.
"""

from __future__ import annotations

import math
import random
from collections import deque
from fractions import Fraction

import pytest

from commontrace import experiment, graph, integrity, lesson_cache, retrieval


@pytest.fixture(params=[17, 103, 619])
def seeded_random(request):
    """Give every invariant its own reproducible generator, never global state."""
    return random.Random(request.param)


@pytest.mark.parametrize("scorer", retrieval.LEXICAL_SCORERS)
def test_ranking_permutation_preserves_scores_and_top_k_prefix(seeded_random, scorer):
    rng = seeded_random
    vocabulary = [f"term{i:03d}" for i in range(110)]
    corpus = [
        (f"lesson_{i:03d}.md", {
            "name": f"lesson_{i:03d}",
            "description": " ".join(rng.sample(vocabulary, rng.randrange(8, 45))),
            "tags": rng.sample(vocabulary, 3),
            # Unique secondary keys make permutation invariance meaningful;
            # exact ranking ties intentionally preserve source-corpus order.
            "importance": i + 1,
            "uses": i,
        })
        for i in range(50)
    ]
    for query_size in (1, 12, 90):
        query = " ".join(rng.sample(vocabulary, query_size))
        baseline = retrieval.rank_lessons(query, corpus, scorer=scorer, floor=0, top_k=100)
        shuffled = corpus[:]
        rng.shuffle(shuffled)
        assert retrieval.rank_lessons(query, shuffled, scorer=scorer, floor=0, top_k=100) == baseline
        terms = {path: lesson_cache.field_terms(fm) for path, fm in corpus}
        assert retrieval.rank_lessons(query, corpus, scorer=scorer, floor=0, top_k=100, term_cache=terms) == baseline
        for limit in (0, 1, 7, 30):
            assert retrieval.rank_lessons(query, corpus, scorer=scorer, floor=0, top_k=limit) == baseline[:limit]
        for hit in baseline:
            assert math.isfinite(hit.score) and hit.score > 0
            assert math.isfinite(hit.relevance) and hit.relevance >= 0
            if scorer != retrieval.SCORER_COUNT:
                assert hit.relevance <= 1
            assert hit.matched_terms == sorted(set(hit.matched_terms))


def test_holdout_nested_rates_order_independence_and_distribution(seeded_random):
    rng = seeded_random
    occasions = [f"occasion-{i}" for i in range(4000)]
    rates = (.05, .1, .3, .5, .8, .95)
    previous = set()
    for rate in rates:
        assigned = {occasion for occasion in occasions if experiment.is_held_out("lesson", occasion, rate, "seeded")}
        assert previous <= assigned
        # Six standard errors: detects broken hash/randomization while avoiding
        # a brittle requirement for exactly the expected binomial arm count.
        assert abs(len(assigned) - len(occasions) * rate) < 6 * math.sqrt(len(occasions) * rate * (1 - rate))
        rng.shuffle(occasions)
        assert assigned == {occasion for occasion in occasions if experiment.is_held_out("lesson", occasion, rate, "seeded")}
        previous = assigned
    one = {o for o in occasions if experiment.is_held_out("lesson", o, .5, "one")}
    two = {o for o in occasions if experiment.is_held_out("lesson", o, .5, "two")}
    assert 1700 < len(one ^ two) < 2300


def test_two_arm_statistics_obey_swap_and_complement_symmetries(seeded_random):
    rng = seeded_random
    for _ in range(50):
        n1, n2 = rng.randrange(1, 1000), rng.randrange(1, 1000)
        s1, s2 = rng.randrange(n1 + 1), rng.randrange(n2 + 1)
        z, p = experiment.two_proportion_test(s1, n1, s2, n2)
        reverse_z, reverse_p = experiment.two_proportion_test(s2, n2, s1, n1)
        assert reverse_z == pytest.approx(-z)
        assert reverse_p == pytest.approx(p)
        assert 0 <= p <= 1
        low, high = experiment.diff_confidence_interval(s1, n1, s2, n2)
        assert low <= s1 / n1 - s2 / n2 <= high
        assert experiment.diff_confidence_interval(s2, n2, s1, n1) == pytest.approx((-high, -low))
        assert experiment.diff_confidence_interval(n1 - s1, n1, n2 - s2, n2) == pytest.approx((-high, -low))
        narrow = experiment.anytime_confidence_interval(s1, n1, s2, n2, alpha=.1)
        wide = experiment.anytime_confidence_interval(s1, n1, s2, n2, alpha=.01)
        assert -1 <= wide[0] <= narrow[0] <= narrow[1] <= wide[1] <= 1
        assert experiment.anytime_confidence_interval(s2, n2, s1, n1, alpha=.1) == pytest.approx((-narrow[1], -narrow[0]))


def test_fdr_matches_adjusted_p_value_oracle_and_is_permutation_equivariant(seeded_random):
    rng = seeded_random
    for _ in range(30):
        values = [rng.choice([rng.random(), rng.random() / 100, .05]) for _ in range(rng.randrange(1, 35))]
        # Independent O(n^2) adjusted-p oracle instead of the production
        # step-up implementation. It also exercises equal p-value groups.
        sorted_values = sorted(values)
        adjusted = []
        for p in values:
            rank = max(i + 1 for i, value in enumerate(sorted_values) if value == p)
            adjusted.append(min(1.0, min(len(values) * Fraction(sorted_values[j - 1]) / j for j in range(rank, len(values) + 1))))
        for alpha in (.01, .05, .1):
            expected = [p <= alpha for p in adjusted]
            assert experiment.benjamini_hochberg(values, alpha) == expected
            order = list(range(len(values)))
            rng.shuffle(order)
            assert experiment.benjamini_hochberg([values[i] for i in order], alpha) == [expected[i] for i in order]


def test_graph_cycles_and_multisource_hops_match_shortest_path_oracle(tmp_path, seeded_random):
    rng = seeded_random
    root = str(tmp_path)
    size = 70
    pairs = {(i, (i + 1) % size) for i in range(size)}
    pairs |= {tuple(sorted(rng.sample(range(size), 2))) for _ in range(100)}
    adjacency = {i: set() for i in range(size)}
    with graph.batch(root):
        for i in range(size):
            graph.add_node(root, f"concept:{i}", "concept", f"Concept {i}")
        for a, b in sorted(pairs):
            graph.add_edge(root, f"concept:{a}", f"concept:{b}", "relates_to", valid_at="2020-01-01")
            adjacency[a].add(b)
            adjacency[b].add(a)
    for hops in range(graph.MAX_HOPS + 1):
        starts = [0, 13]
        distances = {i: 0 for i in starts}
        queue = deque(starts)
        while queue:
            node = queue.popleft()
            if distances[node] == hops:
                continue
            for neighbor in adjacency[node]:
                if neighbor not in distances:
                    distances[neighbor] = distances[node] + 1
                    queue.append(neighbor)
        result = graph.multi_hop_subgraph(root, ["concept:0", "concept:13", "concept:0", "missing"], max_hops=hops)
        assert result["hop_distances"] == {f"concept:{i}": distance for i, distance in distances.items()}
        assert {node["id"] for node in result["nodes"]} == set(result["hop_distances"])
        actual = [(edge["source"], edge["target"]) for edge in result["edges"]]
        expected = {(f"concept:{a}", f"concept:{b}") for a, b in pairs if min(distances.get(a, math.inf), distances.get(b, math.inf)) < hops}
        assert set(actual) == expected
        assert len(actual) == len(set(actual))


def test_sparse_integrity_projection_ignores_unresolved_outcomes_and_input_order(seeded_random):
    rng = seeded_random
    rows = [integrity.Assignment("lesson", f"i{i}", True, succeeded=bool(i % 2)) for i in range(7)]
    rows += [integrity.Assignment("lesson", f"w{i}", False, succeeded=bool(i % 2)) for i in range(3)]
    baseline = integrity.project(rows, min_arm=10)
    rows += [integrity.Assignment("lesson", f"unknown{i}", False, succeeded=None) for i in range(100)]
    rng.shuffle(rows)
    assert integrity.project(rows, min_arm=10) == baseline
    assert baseline[0].still_needed == 7
    assert baseline[0].per_day is None and baseline[0].eta is None
