from commontrace import fact_embeddings, fact_store, fact_vector_projection, hierarchical, recall


def test_dense_recall_uses_projection_and_scores_only_pending_facts(tmp_path, monkeypatch):
    root = str(tmp_path)
    model = "recall-projection-test"
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", model)
    fact_store.migrate(root)
    indexed, _ = hierarchical.add_fact(root, "current parser boundary guidance", scopes=["repo"])
    private, _ = hierarchical.add_fact(root, "private parser boundary guidance", scopes=["private"])
    expired, _ = hierarchical.add_fact(
        root, "expired parser boundary guidance", scopes=["repo"], expires_at="2000-01-01T00:00:00Z"
    )
    with fact_store.transaction(root) as current:
        cursor = current.connection.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    watermark = fact_vector_projection._watermark(model, "lance")
    with fact_store.transaction(root, write=True) as current:
        current.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (watermark, str(cursor)),
        )
    pending, _ = hierarchical.add_fact(root, "new parser boundary guidance", scopes=["repo"])

    def projected_search(actual_root, query, *, model, limit):
        assert actual_root == root
        assert query == "how to change parser boundaries"
        assert model == "recall-projection-test"
        assert limit == 200
        return [(indexed.id, 0.9), (private.id, 0.99), (expired.id, 0.98)]

    def score_pending(query, facts, *, model, root):
        assert query == "how to change parser boundaries"
        assert model == "recall-projection-test"
        assert root == str(tmp_path)
        assert [fact.id for fact in facts] == [pending.id]
        return {pending.id: 0.95}

    monkeypatch.setattr(fact_vector_projection, "_backend", lambda _root: "lance")
    monkeypatch.setattr(fact_vector_projection, "search", projected_search)
    monkeypatch.setattr(fact_embeddings, "scores", score_pending)

    results = recall._dense_facts(root, "how to change parser boundaries", None, 10, "repo")

    assert set(results) == {indexed.id, pending.id}
    assert results[indexed.id][1] == 0.9
    assert results[pending.id][1] == 0.95


def test_dense_recall_keeps_exact_path_for_historical_queries(tmp_path, monkeypatch):
    root = str(tmp_path)
    configured_model = "recall-projection-history-test"
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", configured_model)
    fact_store.migrate(root)
    older, _ = hierarchical.add_fact(
        root, "earlier parser boundary guidance", valid_from="2026-01-01T00:00:00Z"
    )
    with hierarchical.mutate_facts(root) as facts:
        facts[older.id].status = "superseded"
        facts[older.id].valid_until = "2026-06-01T00:00:00Z"
    monkeypatch.setattr(fact_vector_projection, "_backend", lambda _root: "lance")
    monkeypatch.setattr(
        fact_vector_projection,
        "search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("historical queries must not use projection")),
    )

    def score_history(query, facts, *, model, model_path, root):
        assert [fact.id for fact in facts] == [older.id]
        assert model == configured_model
        assert model_path is None
        return {older.id: 0.9}

    monkeypatch.setattr(fact_embeddings, "scores", score_history)

    results = recall._dense_facts(root, "parser boundaries in spring", "2026-03-01T00:00:00Z", 10, "")

    assert set(results) == {older.id}


def test_dense_recall_falls_back_when_projection_watermark_is_invalid(tmp_path, monkeypatch):
    root = str(tmp_path)
    configured_model = "recall-projection-invalid-test"
    monkeypatch.setenv("COMMONTRACE_FACT_EMBEDDER", configured_model)
    fact_store.migrate(root)
    fact, _ = hierarchical.add_fact(root, "current parser boundary guidance")
    watermark = fact_vector_projection._watermark(configured_model, "lance")
    with fact_store.transaction(root, write=True) as current:
        current.connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (watermark, "invalid"),
        )
    monkeypatch.setattr(fact_vector_projection, "_backend", lambda _root: "lance")
    monkeypatch.setattr(
        fact_vector_projection,
        "search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("invalid watermark is not trusted")),
    )

    def score_fallback(query, facts, *, model, model_path, root):
        assert [candidate.id for candidate in facts] == [fact.id]
        assert model == configured_model
        assert model_path is None
        return {fact.id: 0.9}

    monkeypatch.setattr(fact_embeddings, "scores", score_fallback)

    results = recall._dense_facts(root, "parser boundaries", None, 10, "")

    assert set(results) == {fact.id}
