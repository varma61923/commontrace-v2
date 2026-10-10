"""Entity resolution beyond lexical aliases: n-gram fallback, faked embeddings,
thresholds, type compatibility, merges with provenance, and the CLI."""
import json

import pytest

from commontrace import cli, entities, entity_resolution, graph, kg_similarity, provenance


@pytest.fixture
def root(tmp_path):
    (tmp_path / "memory").mkdir()
    r = str(tmp_path)
    graph.add_node(r, "organization:acme_corp", "organization", name="Acme Corp", properties={"mentions": 3})
    graph.add_node(r, "concept:acme_corporation", "concept", name="Acme Corporation")
    graph.add_node(r, "place:acme", "place", name="Acme")
    graph.add_node(r, "service:acme", "service", name="Acme")
    graph.add_node(r, "person:ada_lovelace", "person", name="Ada Lovelace")
    graph.add_edge(r, "person:ada_lovelace", "concept:acme_corporation", "works_at", valid_at="2020-01-01")
    graph.add_node(r, "lesson:acme", "lesson", name="acme")
    return r


class FakeEmbedder:
    """Names map to fixed directions; unknown names get their own axis."""

    def __init__(self, groups):
        self.groups = groups
        self.calls = 0

    def embed(self, texts, *, query):
        self.calls += 1
        out = []
        for text in texts:
            name = text.split(" / ")[0].split(" (")[0]
            axis = self.groups.get(name, 10 + len(name))
            out.append([1.0 if i == axis else 0.0 for i in range(40)])
        return out


def test_ngram_similarity_helpers():
    a, b = kg_similarity.ngrams("Acme Corp"), kg_similarity.ngrams("ACME corp.")
    assert kg_similarity.jaccard(a, b) == 1.0
    assert 0 < kg_similarity.jaccard(a, kg_similarity.ngrams("Acme Corporation")) < 1
    assert kg_similarity.jaccard(frozenset(), frozenset()) == 0.0
    assert kg_similarity.pairwise([[1.0, 0.0], [0.0, 1.0]]) == [[1.0, 0.0], [0.0, 1.0]]
    assert len(kg_similarity.embed(FakeEmbedder({}), ["a", "b"], query=False)) == 2

    class Bad:
        def __init__(self, rows):
            self.rows = rows

        def embed(self, texts, *, query):
            return self.rows

    for texts, rows in ((["a"], [[0.0, 0.0]]), (["a"], [[1.0]] * 2), (["a", "b"], [[1.0], [1.0, 0.0]])):
        with pytest.raises(ValueError):  # zero vector, wrong count, mixed dimensions
            kg_similarity.embed(Bad(rows), texts, query=False)
    assert kg_similarity.embed(Bad([[3.0, 4.0]]), ["a"], query=True) == [[0.6, 0.8]]


def test_ngram_candidates_with_scores_and_type_compatibility(root, monkeypatch):
    monkeypatch.delenv(kg_similarity.ENV, raising=False)
    found = entity_resolution.candidates(root, threshold=0.4)
    assert found["method"] == "ngram" and found["threshold"] == 0.4
    pairs = {(p["a"], p["b"]): p for p in found["pairs"]}
    acme = pairs[("concept:acme_corporation", "organization:acme_corp")]
    assert 0.4 <= acme["score"] < 1 and acme["compatible"]
    blocked = pairs[("place:acme", "service:acme")]
    assert blocked["score"] == 1.0 and not blocked["compatible"]
    assert not any("lesson:" in a or "lesson:" in b for a, b in pairs)
    assert found["pairs"] == sorted(found["pairs"], key=lambda p: (-p["score"], p["a"], p["b"]))


def test_apply_merges_only_strict_compatible_pairs_with_provenance(root, monkeypatch):
    monkeypatch.delenv(kg_similarity.ENV, raising=False)
    out = entity_resolution.resolve(root, threshold=0.4, apply=True, apply_threshold=0.45)
    assert [(m["kept"], m["merged"]) for m in out["merged"]] == [("organization:acme_corp", "concept:acme_corporation")]
    assert {s["reason"] for s in out["skipped"]} == {"incompatible ontology types"}
    nodes = graph.load_nodes(root)
    assert "place:acme" in nodes and not nodes["place:acme"].properties.get("merged_into")
    loser = nodes["concept:acme_corporation"].properties
    assert loser["merged_into"] == "organization:acme_corp" and loser["merge_evidence"]["method"] == "ngram"
    moved = [e for e in graph.load_edges(root) if e.relation == "works_at" and e.invalid_at is None]
    assert [e.target for e in moved] == ["organization:acme_corp"]
    records = provenance.list_provenance(root, "organization:acme_corp->concept:acme_corporation:supersedes")
    assert records and records[0]["source_path"] == "entity_resolution"
    assert records[0]["detail"]["score"] == out["merged"][0]["score"]


def test_report_only_without_apply_and_threshold_checks(root):
    out = entity_resolution.resolve(root, threshold=0.4)
    assert out["merged"] == [] and out["pairs"]
    assert not graph.load_nodes(root)["concept:acme_corporation"].properties.get("merged_into")
    with pytest.raises(ValueError, match="apply threshold"):
        entity_resolution.resolve(root, threshold=0.9, apply_threshold=0.5)
    with pytest.raises(ValueError):
        entity_resolution.candidates(root, threshold=0)


def test_embedding_resolution_finds_semantic_duplicates(root):
    graph.add_node(root, "organization:international_business_machines", "organization",
                   name="International Business Machines")
    graph.add_node(root, "concept:ibm", "concept", name="IBM")
    fake = FakeEmbedder({"International Business Machines": 1, "IBM": 1})
    out = entity_resolution.resolve(root, embedder=fake, apply=True)
    assert out["method"] == "embedding" and out["threshold"] == 0.85 and out["apply_threshold"] == 0.95
    assert [(m["kept"], m["merged"]) for m in out["merged"]] == [
        ("organization:international_business_machines", "concept:ibm")]
    assert fake.calls == 1  # one batch for every entity


def test_embedding_with_context_and_one_merge_per_entity(root):
    graph.add_node(root, "concept:acme_co", "concept", name="Acme Co")
    fake = FakeEmbedder({"Acme Corp": 2, "Acme Corporation": 2, "Acme Co": 2})
    out = entity_resolution.resolve(root, embedder=fake, apply=True, context=True)
    assert len(out["merged"]) == 1
    assert any(s["reason"] == "already merged in this run" for s in out["skipped"])


def test_compatibility_rules(root):
    from commontrace import ontology

    onto = ontology._from_mapping({"entity_types": {"database": {"parent": "service"}}}, "test")
    assert entity_resolution.compatible(onto, "database", "service")
    assert entity_resolution.compatible(onto, "concept", "person")
    assert not entity_resolution.compatible(onto, "person", "organization")


def test_merge_keeps_evidence(root):
    entities.merge(root, "organization:acme_corp", "concept:acme_corporation", evidence={"score": 0.9})
    supersedes = [e for e in graph.load_edges(root) if e.relation == "supersedes"]
    assert supersedes[0].properties["evidence"] == {"score": 0.9}


def test_cli_resolve(root, capsys, monkeypatch):
    monkeypatch.delenv(kg_similarity.ENV, raising=False)
    assert cli.main(["graph", "resolve", "--threshold", "0.4", "--json", "--dest", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["method"] == "ngram" and out["merged"] == []
    assert cli.main(["graph", "resolve", "--threshold", "0.4", "--apply", "--apply-threshold", "0.45",
                     "--dest", root]) == 0
    text = capsys.readouterr().out
    assert "merged concept:acme_corporation into organization:acme_corp" in text and "[blocked" in text
    assert cli.main(["graph", "resolve", "--embedder", "not a tag!", "--dest", root]) == 2
