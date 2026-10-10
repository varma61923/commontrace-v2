"""Fact deduplication: exact and near-duplicate reinforcement, semantic mode, and the persistent dense cache."""
from __future__ import annotations

import time

import pytest

from commontrace import fact_embeddings, hierarchical


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("COMMONTRACE_FACT_DEDUP", "COMMONTRACE_FACT_EMBEDDER", "COMMONTRACE_FACT_EMBEDDER_PATH"):
        monkeypatch.delenv(name, raising=False)
    fact_embeddings._VECTORS.clear()
    fact_embeddings._MODELS.clear()


@pytest.mark.parametrize("a, b, same", [
    ("The office is located in Tokyo, Japan", "Our office is located in Tokyo Japan", True),
    ("I have 2 cats at home now", "I have 3 cats at home now", False),
    ("Alice likes spicy food a lot", "Alice does not like spicy food a lot", False),
    ("Prefers dark mode in the editor", "Prefers light mode in the editor", False),
    ("Bob works at Acme", "Bob worked at Initech", False),
    ("Short one", "Short one!", False),  # too few content words to call a paraphrase
])
def test_the_near_duplicate_rule_is_precision_first(a, b, same):
    assert hierarchical.near_duplicate(a, b) is same


def test_a_paraphrase_reinforces_instead_of_adding(tmp_path):
    root = str(tmp_path)
    first, action = hierarchical.add_fact(root, "The office is located in Tokyo, Japan", source_trace_id="t1")
    again, action2 = hierarchical.add_fact(root, "Our office is located in Tokyo Japan", source_trace_id="t2")
    assert action == "ADD" and action2 == "NOOP" and again.id == first.id
    stored = hierarchical.load_facts(root)[first.id]
    assert stored.confirmations == 2 and stored.source_traces == ["t1", "t2"]
    other, action3 = hierarchical.add_fact(root, "I have 2 cats at home now")
    third, action4 = hierarchical.add_fact(root, "I have 3 cats at home now")
    assert action3 == action4 == "ADD" and other.id != third.id


def test_exact_mode_keeps_paraphrases_apart(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "exact")
    root = str(tmp_path)
    hierarchical.add_fact(root, "The office is located in Tokyo, Japan")
    _fact, action = hierarchical.add_fact(root, "Our office is located in Tokyo Japan")
    assert action == "ADD"
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "fuzzy")
    with pytest.raises(ValueError, match="COMMONTRACE_FACT_DEDUP"):
        hierarchical.add_fact(root, "anything at all here")


def test_scope_stays_an_authorization_boundary_for_near_duplicates(tmp_path):
    root = str(tmp_path)
    tenant, _ = hierarchical.add_fact(root, "The office is located in Tokyo, Japan", scopes=["tenant-a"])
    other, action = hierarchical.add_fact(root, "Our office is located in Tokyo Japan", scopes=["tenant-b"])
    glob, action2 = hierarchical.add_fact(root, "Our office is located in Tokyo Japan")
    assert action == action2 == "ADD" and len({tenant.id, other.id, glob.id}) == 3


def test_append_only_admission_treats_a_paraphrase_as_a_noop_and_never_edits(tmp_path):
    root = str(tmp_path)
    [(first, a1)] = hierarchical.append_facts(root, [{"statement": "The office is located in Tokyo, Japan"}])
    before = hierarchical.load_facts(root)[first.id].to_dict()
    [(dup, a2)] = hierarchical.append_facts(root, [{"statement": "Our office is located in Tokyo Japan"}])
    assert a1 == "ADD" and a2 == "NOOP" and dup.id == first.id
    assert hierarchical.load_facts(root)[first.id].to_dict() == before


def test_batches_use_one_index_and_dedupe_within_the_batch(tmp_path, monkeypatch):
    root = str(tmp_path)
    builds = []
    original = hierarchical._StatementIndex.__init__

    def counting(self, facts):
        builds.append(len(facts))
        original(self, facts)

    monkeypatch.setattr(hierarchical._StatementIndex, "__init__", counting)
    results = hierarchical.add_facts(root, [
        {"statement": "The office is located in Tokyo, Japan"},
        {"statement": "Our office is located in Tokyo Japan"},
        {"statement": "The quarterly report is due on Friday"},
    ])
    assert [action for _f, action in results] == ["ADD", "NOOP", "ADD"]
    many = [{"statement": f"Customer {i} prefers invoices by email in region {i % 7}"} for i in range(1500)]
    builds.clear()
    started = time.perf_counter()
    hierarchical.add_facts(root, many)
    assert len(hierarchical.load_facts(root)) == 1502
    assert builds == [2]  # one index per batch, not one scan per statement (O(N x M))
    assert time.perf_counter() - started < 60  # a loose backstop: CI runs this under parallel load


class _Topics:
    """A fake embedder: statements about the same topic word share a direction."""

    TOPICS = ("tokyo", "cats", "report", "spicy")

    def __init__(self):
        self.calls = 0

    def encode(self, texts, normalize_embeddings=True, query=False):
        self.calls += len(texts)
        out = []
        for text in texts:
            low = text.lower()
            vec = [1.0 if topic in low else 0.0 for topic in self.TOPICS] + [0.05]
            norm = sum(v * v for v in vec) ** 0.5
            out.append([v / norm for v in vec])
        return out


def test_semantic_mode_catches_rewordings_the_lexical_rule_misses(tmp_path, monkeypatch):
    model = _Topics()
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "semantic")
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda m, p: (model, ("tag", "fake-topics")))
    root = str(tmp_path)
    first, _ = hierarchical.add_fact(root, "Bob's company headquarters sit in Tokyo")
    dup, action = hierarchical.add_fact(root, "Tokyo hosts the head office where Bob works")
    assert action == "NOOP" and dup.id == first.id
    # Same topic but a different number is never merged, whatever the cosine.
    _f, action2 = hierarchical.add_fact(root, "Bob visited Tokyo 3 times")
    assert action2 == "ADD"


def test_fact_vectors_persist_across_processes(tmp_path, monkeypatch):
    model = _Topics()
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda m, p: (model, ("tag", "fake-topics")))
    root = str(tmp_path)
    facts = [hierarchical.add_fact(root, s)[0] for s in ("Tokyo office opens at nine",
                                                         "The cats sleep all day long",
                                                         "The report is due this Friday")]
    scores = fact_embeddings.scores("when does the tokyo office open", facts, root=root)
    assert max(scores, key=scores.get) == facts[0].id and model.calls == 4
    fact_embeddings._VECTORS.clear()  # a new process: only the on-disk cache remains
    fact_embeddings.scores("anything about cats", facts, root=root)
    assert model.calls == 5  # the query only; every fact vector came from disk
    assert list((tmp_path / "memory" / "facts").glob("embeddings-fake-topics.db"))


def test_a_provider_tag_selects_the_embedder(tmp_path, monkeypatch):
    from commontrace import embeddings

    class Fixed:
        def __init__(self, spec):
            self.spec = spec

        def embed(self, texts, *, query):
            return [[1.0, 0.0] if "tokyo" in t.lower() else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(embeddings, "provider", lambda tag, post=None: Fixed(embeddings.parse(tag)))
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", "openai:text-embedding-3-small")
    root = str(tmp_path)
    facts = [hierarchical.add_fact(root, s)[0] for s in ("Tokyo office opens at nine", "Cats sleep all day")]
    scores = fact_embeddings.scores("tokyo hours", facts, root=root)
    assert scores[facts[0].id] == pytest.approx(1.0) and scores[facts[1].id] == pytest.approx(0.0)
    assert list((tmp_path / "memory" / "facts").glob("embeddings-openai_text-embedding-3-small.db"))


def test_recall_fuses_a_dense_arm_into_the_facts_channel(tmp_path, monkeypatch):
    from commontrace import recall

    model = _Topics()
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda m, p: (model, ("tag", "fake-topics")))
    root = str(tmp_path)
    tokyo, _ = hierarchical.add_fact(root, "Bob's company headquarters sit in Tokyo")
    hierarchical.add_fact(root, "The quarterly report is due on Friday")
    question = "where is the tokyo head office"
    lexical_only = recall.recall(root, question, channels=("facts",))
    assert all(i.id != f"fact:{tokyo.id}" or "dense_cosine" not in i.provenance["search"]
               for i in lexical_only.items)  # no embedder configured: lexical behaviour unchanged
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", "fake")
    hybrid = recall.recall(root, question, channels=("facts",))
    top = hybrid.items[0]
    assert top.id == f"fact:{tokyo.id}" and top.provenance["search"]["dense_cosine"] > 0.9
    assert top.score <= 1.0
    forgotten, _ = hierarchical.add_fact(root, "Tokyo branch hosts the archive servers")
    hierarchical.forget_fact(root, forgotten.id)
    again = recall.recall(root, question, channels=("facts",))
    assert f"fact:{forgotten.id}" not in {i.id for i in again.items}  # the dense arm honours forgetting
