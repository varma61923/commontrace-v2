"""The shared reranker stage in multi-channel recall, fact search and conversation recall."""
from __future__ import annotations

import json

import pytest

from commontrace import hierarchical, providers, recall, reranking
from commontrace.conversation import Options, Store
from commontrace.conversation import recall as conversation_recall
from commontrace.reranking import Hit, TextReranker
from commontrace.search_recipes import search
from tests.test_multichannel_recall import store  # noqa: F401 - fixture

FAKE = "fake-text-for-test"


class Preferring(TextReranker):
    """Scores a document by how often it mentions any of `words`; counts calls."""

    name = FAKE

    def __init__(self):
        self.words: tuple[str, ...] = ()
        self.calls = 0
        self.fail = False

    def score(self, query, documents):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return [float(sum(d.lower().count(w) for w in self.words)) for d in documents]


RANKER = Preferring()


@pytest.fixture(autouse=True)
def fake_reranker():
    if FAKE not in providers.reranker_names():
        providers.register_reranker(FAKE, lambda: RANKER)
    RANKER.words, RANKER.calls, RANKER.fail = (), 0, False
    return RANKER


class Fixed(TextReranker):
    name = "fixed"

    def __init__(self, order):
        self.order = order

    def rerank(self, query, documents, top_n=None):
        return [Hit(i, float(len(self.order) - n)) for n, i in enumerate(self.order)]


IDS = ["a", "b", "c", "d"]
TEXT = {i: i for i in IDS}


def test_blend_zero_keeps_first_stage_order_and_one_takes_the_rerankers():
    ranker = Fixed([3, 2, 1, 0])
    assert reranking.stage("q", IDS, TEXT, ranker, blend=0.0)[0] == IDS
    order, report = reranking.stage("q", IDS, TEXT, ranker, blend=1.0)
    assert order == ["d", "c", "b", "a"] and report["moved"] == 4 and report["items"] == 4
    assert report["top"][0] == {"id": "d", "score": 4.0} and report["reranker"] == "fixed"
    assert report["latency_ms"] >= 0


def test_an_even_blend_lets_agreement_win_and_depth_bounds_the_head():
    # The reranker swaps the first two; an even blend ties them, and ties keep fused order.
    assert reranking.stage("q", IDS, TEXT, Fixed([1, 0, 2, 3]), blend=0.5)[0] == IDS
    order, report = reranking.stage("q", IDS, TEXT, Fixed([1, 0]), depth=2, blend=0.9)
    assert order == ["b", "a", "c", "d"] and report["items"] == 2


def test_a_failing_reranker_keeps_the_order_and_reports_why(fake_reranker):
    fake_reranker.fail = True
    order, report = reranking.stage("q", IDS, TEXT, fake_reranker, blend=1.0)
    assert order == IDS and report["items"] == 0 and "provider down" in report["error"]


@pytest.mark.parametrize("depth, blend", [(0, 0.5), (201, 0.5), (True, 0.5), (5, -0.1), (5, 1.5), (5, "x")])
def test_stage_options_are_validated(depth, blend):
    with pytest.raises(ValueError):
        reranking.check_options(depth, blend)


def test_recall_without_a_reranker_is_unchanged(store):  # noqa: F811
    result = recall.recall(store, "what should I do when postgres fails over?", budget=600)
    assert "rerank" not in result.explain and RANKER.calls == 0
    assert result.to_dict()["explain"]["completeness"]["basis"] == "question-terms"


def test_recall_reranks_the_fused_head_before_packing(store, fake_reranker):  # noqa: F811
    question = "what should I do when postgres fails over?"
    base = recall.recall(store, question, budget=600)
    fake_reranker.words = ("hour", "backed")
    reranked = recall.recall(store, question, budget=600, reranker=FAKE, rerank_blend=1.0)
    report = reranked.explain["rerank"]
    assert report["reranker"] == FAKE and report["items"] == len(base.items) and fake_reranker.calls == 1
    assert report["top"][0]["id"].startswith("fact:")
    assert reranked.items[0].channel == "facts" and base.items[0].channel != "facts"
    # Fused scores stay a descending scale in the new order.
    fused = [i.fused for i in reranked.items]
    assert fused == sorted(fused, reverse=True)
    assert reranked.tokens <= 600


def test_recall_reranker_failure_keeps_the_fused_page(store, fake_reranker):  # noqa: F811
    question = "what should I do when postgres fails over?"
    base = recall.recall(store, question, budget=600)
    fake_reranker.fail = True
    failed = recall.recall(store, question, budget=600, reranker=FAKE)
    assert [i.id for i in failed.items] == [i.id for i in base.items]
    assert "provider down" in failed.explain["rerank"]["error"] and not failed.errors


def test_recall_reranker_from_budgets_json_and_per_call_override(store, tmp_memory, fake_reranker):  # noqa: F811
    (tmp_memory / "budgets.json").write_text(json.dumps(
        {"rerank": {"reranker": FAKE, "depth": 3, "blend": 1.0},
         "agents": {"plain": {"rerank": "none"}}}), encoding="utf-8")
    configured = recall.recall(store, "postgres failover", budget=600)
    assert configured.explain["rerank"]["depth"] == 3 and configured.explain["rerank"]["blend"] == 1.0
    assert "rerank" not in recall.recall(store, "postgres failover", budget=600, agent="plain").explain
    assert "rerank" not in recall.recall(store, "postgres failover", budget=600, reranker="none").explain


def test_recall_rejects_unknown_rerankers_and_reports_unavailable_ones(store, monkeypatch):  # noqa: F811
    with pytest.raises(ValueError, match="unknown reranker"):
        recall.recall(store, "postgres", reranker="nope")
    with pytest.raises(ValueError, match="rerank_blend"):
        recall.recall(store, "postgres", reranker=FAKE, rerank_blend=2)
    from commontrace import rerank_arm

    monkeypatch.setattr(rerank_arm, "available", lambda: False)
    result = recall.recall(store, "postgres failover", reranker="bge-reranker-v2-m3")
    assert result.items and "attention extra" in result.explain["rerank"]["error"]


def test_fact_search_reranks_and_reports(tmp_path, fake_reranker):
    root = str(tmp_path)
    for text in ("deploy window is friday", "deploy rollback needs approval", "deploy uses blue green"):
        hierarchical.add_fact(root, text)
    base = search(root, "deploy", dense_scores={})
    assert all("rerank_score" not in r for r in base)
    fake_reranker.words = ("green",)
    explain: dict = {}
    rows = search(root, "deploy", dense_scores={}, reranker=FAKE, rerank_blend=1.0, explain=explain)
    assert rows[0]["text"] == "deploy uses blue green" and rows[0]["rerank_score"] == 1.0
    assert explain["rerank"]["items"] == 3 and sorted(r["id"] for r in rows) == sorted(r["id"] for r in base)
    # Blend 0: the reranker runs and reports, but the order is the first stage's.
    assert [r["id"] for r in search(root, "deploy", dense_scores={}, reranker=FAKE, rerank_blend=0.0)] == \
        [r["id"] for r in base]


def test_fact_search_reads_the_configured_reranker(tmp_path, fake_reranker):
    root = str(tmp_path)
    hierarchical.add_fact(root, "deploy window is friday")
    hierarchical.add_fact(root, "deploy uses blue green")
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "budgets.json").write_text(json.dumps({"rerank": FAKE}), encoding="utf-8")
    explain: dict = {}
    search(root, "deploy", dense_scores={}, explain=explain)
    assert explain["rerank"]["reranker"] == FAKE and fake_reranker.calls == 1


def test_conversation_recall_accepts_any_text_reranker(tmp_path, fake_reranker):
    with Store(str(tmp_path), "chat") as conv:
        conv.add("s1", [{"speaker": "ana", "text": "We moved the deploy to friday."},
                        {"speaker": "ana", "text": "The deploy uses blue green switching."}])
        fake_reranker.words = ("green",)
        result = conversation_recall(conv, "deploy", options=Options(embedder=None, rerank=FAKE, rerank_blend=0))
        assert result.explain["rerank"] == FAKE and result.explain["rerank_stage"]["items"] == 2
        assert result.ranked[0] != result.ranked[-1] and fake_reranker.calls == 1
        stage = result.explain["rerank_stage"]["top"][0]
        assert isinstance(stage["id"], int) and stage["score"] == 1.0
        missing = conversation_recall(conv, "deploy", options=Options(embedder=None, rerank="nope"))
        assert "unknown reranker" in missing.explain["rerank_stage"]["error"]
