from unimem import fact_store, hierarchical


def test_candidates_use_fts_and_preserve_governance(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    good, _ = hierarchical.add_fact(root, "pytest validates the parser boundaries", scopes=["agent:a"])
    old, _ = hierarchical.add_fact(root, "pytest old invalid parser")
    hierarchical.invalidate_fact(root, old.id)

    def no_scan(*args, **kwargs):
        raise AssertionError("indexed retrieval must not load the entire bank")

    monkeypatch.setattr(hierarchical, "load_facts", no_scan)
    result = fact_store.fts_candidates(root, "pytest", context=["agent:a"])
    assert result[0][0].id == good.id
    assert not fact_store.fts_candidates(root, "pytest", context=["agent:b"])
    assert not fact_store.fts_candidates(root, "", context=["agent:a"])
