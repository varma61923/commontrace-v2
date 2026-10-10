"""Ontology-based classification of entity mentions: aliases and keywords, embeddings
(faked), an LLM (faked), inheritance, thresholds, extraction and the CLI."""
import json
import os

import pytest

from commontrace import cli, entities, kg_similarity, ontology, ontology_classify

ONTOLOGY = """
entity_types:
  database: {parent: service, description: A data store, keywords: [database, db, datastore]}
  team: {parent: organization, description: A group of people, keywords: [team, squad]}
  vehicle: {description: Something that moves people or goods, keywords: []}
aliases:
  service:postgres: [pg, postgresql]
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "memory").mkdir()
    with open(os.path.join(tmp_path, "memory", "ontology.yaml"), "w", encoding="utf-8") as fh:
        fh.write(ONTOLOGY)
    return str(tmp_path)


@pytest.fixture
def onto(root):
    return ontology.load(root)


class FakeEmbedder:
    """Unit vectors on fixed axes: a word in the text decides the direction."""

    AXES = ("truck", "store", "people")

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, query):
        self.calls.append((list(texts), query))
        out = []
        for text in texts:
            lowered = text.lower()
            vector = [1.0 if axis in lowered else 0.0 for axis in self.AXES]
            if "vehicle" in lowered or "moves" in lowered:
                vector[0] += 1.0
            out.append(vector if any(vector) else [0.01, 0.01, 0.01])
        return out


def test_type_keywords_load_with_defaults_and_overrides(onto):
    assert onto.entity_types["database"].keywords == ("database", "db", "datastore")
    assert "service" in onto.entity_types["service"].keywords  # built-in keywords stay
    assert onto.to_dict()["entity_types"]["team"]["keywords"] == ["team", "squad"]


def test_keywords_must_be_a_list(tmp_path):
    with pytest.raises(ontology.OntologyError, match="keywords"):
        ontology._from_mapping({"entity_types": {"x": {"keywords": "oops"}}}, "test")


def test_alias_names_the_type(onto):
    found = ontology_classify.classify("PG", onto=onto)
    assert (found.type, found.method, found.confidence) == ("service", "alias", 0.95)


def test_head_word_prefers_the_most_specific_type(onto):
    found = ontology_classify.classify("Orders Database", onto=onto)
    assert found.type == "database" and found.method == "keyword"
    assert found.ancestors == ("database", "service")
    assert ontology_classify.classify("Platform Team", onto=onto).type == "team"


def test_word_and_context_scores(onto):
    word = ontology_classify.classify("API Gateway Squad", onto=onto)
    assert word.type == "team" and word.confidence == pytest.approx(0.9)
    context = ontology_classify.classify("Lyon", "We opened an office in the city of Lyon last year.", onto=onto)
    assert (context.type, context.confidence) == ("place", pytest.approx(0.7))


def test_unknown_below_threshold(onto):
    found = ontology_classify.classify("Zorblax", "Nothing here says what Zorblax is.", onto=onto)
    assert not found.known and found.method == "none"
    strict = ontology_classify.classify("Lyon", "the city of Lyon", onto=onto, threshold=0.8)
    assert strict.type is None


def test_allowed_types_respect_inheritance(onto):
    assert "database" in ontology_classify.candidate_types(onto, allowed=["service"])
    assert "team" not in ontology_classify.candidate_types(onto, allowed=["service"])
    found = ontology_classify.classify("Orders Database", onto=onto, allowed=["organization"])
    assert found.type is None
    assert "lesson" not in ontology_classify.candidate_types(onto)


def test_embedding_method_with_a_faked_provider(onto):
    fake = FakeEmbedder()
    found = ontology_classify.classify("Cargo Hauler 9", "The truck delivered parts.", onto=onto, embedder=fake)
    assert found.type == "vehicle" and found.method == "embedding"
    assert found.confidence >= ontology_classify.EMBEDDING_THRESHOLD
    assert fake.calls[0][1] is False and fake.calls[-1][1] is True  # types as documents, mention as query
    before = len(fake.calls)
    ontology_classify.classify("Cargo Hauler 10", "The truck left.", onto=onto, embedder=fake)
    assert len(fake.calls) == before + 1  # type vectors are cached


def test_embedding_from_environment(monkeypatch, onto):
    fake = FakeEmbedder()
    monkeypatch.setenv(kg_similarity.ENV, "fake:model")
    monkeypatch.setattr(kg_similarity, "embedder", lambda spec=None: fake if spec in (None, "fake:model") else None)
    assert ontology_classify.classify("Hauler", "a truck", onto=onto).type == "vehicle"


def test_llm_method_validates_the_answer(onto):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return json.dumps({"type": "vehicle", "confidence": 0.8}), {}

    found = ontology_classify.classify("Zorblax", "Zorblax carried us home.", onto=onto, llm=complete)
    assert (found.type, found.method, found.confidence) == ("vehicle", "llm", 0.8)
    assert "- vehicle:" in prompts[0] and '"Zorblax"' in prompts[0]
    for reply in ('{"type": "spaceship", "confidence": 0.9}', "not json", '{"type": "vehicle", "confidence": 0.2}',
                  '{"type": "vehicle", "confidence": "high"}'):
        assert ontology_classify.classify("Zorblax", onto=onto, llm=lambda p, r=reply: (r, {})).type is None


def test_kg_similarity_embedder_resolution(monkeypatch):
    monkeypatch.delenv(kg_similarity.ENV, raising=False)
    assert kg_similarity.embedder() is None
    monkeypatch.setenv(kg_similarity.ENV, "none")
    assert kg_similarity.embedder() is None
    fake = FakeEmbedder()
    assert kg_similarity.embedder(fake) is fake
    with pytest.raises(TypeError):
        kg_similarity.embedder(object())
    assert kg_similarity.embedder("openai:text-embedding-3-small").spec.provider == "openai"  # built, not called


def test_extraction_types_untyped_mentions(onto):
    found = {m.name: m.key for m in entities.extract("We met at the Berlin Summit while Orders Database failed.",
                                                     onto=onto, use_spacy=False)}
    assert found["Berlin Summit"] == "event:berlin_summit"
    assert found["Orders Database"] == "database:orders_database"
    # Without an ontology nothing is reclassified.
    assert "concept:berlin_summit" in [m.key for m in entities.extract("the Berlin Summit", use_spacy=False)]


def test_cli_classify(root, capsys):
    assert cli.main(["ontology", "classify", "Orders Database", "--json", "--dest", root]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["type"] == "database" and row["ancestors"] == ["database", "service"]
    assert cli.main(["ontology", "classify", "--extract", "The Platform Team runs the Orders Database.",
                     "--dest", root]) == 0
    out = capsys.readouterr().out
    assert "'Platform Team': team" in out and "'Orders Database': database" in out
