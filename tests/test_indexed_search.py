import json

from commontrace import fact_store, hierarchical, search_recipes


def test_union_adds_dense_only_candidates_and_filters_scopes(tmp_path, monkeypatch):
    root = str(tmp_path)
    fact_store.migrate(root)
    sparse, _ = hierarchical.add_fact(root, "parser validates pytest results", scopes=["agent:a"])
    dense, _ = hierarchical.add_fact(root, "Validate the grammar boundary before execution", scopes=["agent:a"])
    secret, _ = hierarchical.add_fact(root, "Hidden private account", scopes=["agent:b"])

    def vector_search(query, limit):
        return [(dense.id, 0.9), (secret.id, 0.99)]

    def no_scan(*args, **kwargs):
        raise AssertionError("hybrid must not scan the whole bank")

    monkeypatch.setattr(hierarchical, "list_facts", no_scan)
    report = {}
    results = search_recipes.search(
        root, "parser pytest", context=["agent:a"], vector_search=vector_search, explain=report
    )
    assert {row["id"] for row in results} == {sparse.id, dense.id}
    assert report["fusion"] == "rrf"
    assert all(row["signals"] for row in results)


def test_multiround_bound_and_trace(tmp_path):
    root = str(tmp_path)
    fact_store.migrate(root)
    hierarchical.add_fact(root, "pytest validates parser grammar")

    def judge(prompt):
        return json.dumps({"grade": "PARTIAL", "queries": ["pytest"]}), {}

    result = search_recipes.multi_round(root, "parser", complete=judge, max_rounds=3, embedder="none")
    assert len(result["trace"]) == 2
    assert result["trace"][-1]["stopped"] == "no-new-queries"
    assert result["results"]


def test_candidate_fact_hydration_uses_bounded_id_batches():
    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, parameters):
            self.calls.append((statement, tuple(parameters)))
            return []

    class Canonical:
        removed = set()
        loaded = {}

        def __init__(self):
            self.connection = Connection()

    canonical = Canonical()
    identities = [f"fact:{index}" for index in range(501)]
    candidates = search_recipes._load_candidate_facts(canonical, [*identities, identities[0]])

    assert set(candidates) == set(identities)
    assert [len(parameters) for _, parameters in canonical.connection.calls] == [500, 1]
