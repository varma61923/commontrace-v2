from unimem import fact_embeddings, fact_store, fact_vector_projection, hierarchical, search_recipes


class Model:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.extend(texts)
        return [[1.0, 0.0] if "grammar" in text or "parse" in text else [0.0, 1.0] for text in texts]


def test_event_projection_dense_union_restart_and_revocation(tmp_path, monkeypatch):
    root = str(tmp_path)
    model = Model()
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda *_: (model, ("tag", "projection-test")))
    fact_store.migrate(root)
    wanted, _ = hierarchical.add_fact(root, "grammar boundary rules", scopes=["agent:a"])
    secret, _ = hierarchical.add_fact(root, "grammar private rules", scopes=["agent:b"])
    assert fact_vector_projection.drain(root, model="projection-test")["processed"] == 2
    calls = len(model.calls)
    fact_embeddings._VECTORS.clear()
    assert fact_vector_projection.drain(root, model="projection-test")["processed"] == 0
    assert len(model.calls) == calls
    report = {}
    results = search_recipes.search(root, "parse", embedder="projection-test", context=["agent:a"], explain=report)
    assert [row["id"] for row in results] == [wanted.id]
    assert report["dense_candidates"]
    hierarchical.forget_fact(root, wanted.id)
    assert search_recipes.search(root, "parse", embedder="projection-test", context=["agent:a"]) == []


def test_pending_canonical_revision_cannot_use_old_dense_scores(tmp_path, monkeypatch):
    root, model = str(tmp_path), Model()
    monkeypatch.setattr(fact_embeddings, "_resolve", lambda *_: (model, ("tag", "projection-test")))
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "grammar boundary rules")
    fact_vector_projection.drain(root, model="projection-test")
    assert fact_vector_projection.search(root, "parse", model="projection-test")
    with hierarchical.mutate_facts(root) as facts:
        facts[fact.id].statement = "database retention rules"
    assert fact_vector_projection.search(root, "parse", model="projection-test") == []
    fact_vector_projection.drain(root, model="projection-test")
    assert fact_vector_projection.search(root, "database", model="projection-test")[0][0] == fact.id
