"""Entity extraction and linking: patterns, canonical keys, aliases, the entity store,
lesson linking, retrieval boosting, duplicates and merge."""
import subprocess
import sys

import pytest

from commontrace import entities, graph, ontology
from tests.conftest import write_lesson


@pytest.fixture
def store(tmp_memory):
    write_lesson(tmp_memory, "lesson_pool-reset", description="Reset the Postgres pool on OperationalError",
                 rule="When PostgreSQL raises OperationalError in api/db.py, call reset_pool() first.")
    write_lesson(tmp_memory, "lesson_redis-timeouts", description="Redis timeouts behind nginx",
                 rule="Raise REDIS_TIMEOUT_MS when nginx returns HTTP 504 for cache reads.")
    write_lesson(tmp_memory, "lesson_unrelated", description="Format before committing",
                 rule="Run the formatter before you commit.")
    return str(tmp_memory.parent)


def keys(text, **kw):
    return [m.key for m in entities.extract(text, use_spacy=False, **kw)]


def test_technical_entities():
    found = keys("OperationalError in api/db.py: call reset_pool() and set DATABASE_URL; see `retry.backoff`, "
                 "HTTP 503 and E1101.")
    for key in ("error:operationalerror", "file:api/db.py", "symbol:reset_pool", "concept:database_url",
                "symbol:retry.backoff", "error:http_503", "error:e1101"):
        assert key in found


def test_people_organisations_places_and_builtin_aliases():
    found = keys("Dr. Sarah Chen from Acme Corp moved PostgreSQL and k8s to Berlin.")
    assert "person:sarah_chen" in found
    assert "organization:acme_corp" in found
    assert "service:postgres" in found and "tool:kubernetes" in found
    assert "concept:berlin" in found


def test_sentence_openers_and_common_words_are_not_entities():
    found = keys("When the build fails, check the logs. Then retry. Never skip tests.")
    assert found == []


def test_each_entity_once_and_overlaps_resolved():
    found = entities.extract("Redis, redis and REDIS again; ValueError and ValueError.", use_spacy=False)
    assert [m.key for m in found].count("service:redis") == 1
    assert [m.key for m in found].count("error:valueerror") == 1


def test_ontology_aliases_canonicalise(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "ontology.yaml").write_text("aliases:\n  service:postgres: [pg, the main db]\n",
                                                       encoding="utf-8")
    onto = ontology.load(str(tmp_path))
    assert keys("pg is down and the main db is slow", onto=onto) == ["service:postgres"]


def test_spacy_requested_but_missing(monkeypatch):
    monkeypatch.setattr(entities, "spacy_model", lambda: None)
    with pytest.raises(RuntimeError, match="spaCy"):
        entities.extract("x", use_spacy=True)


def test_link_lessons_builds_entity_store_and_boosts(store):
    out = entities.link_lessons(store, use_spacy=False)
    assert out["lessons"] == 3 and out["new_links"] > 0
    nbrs = {n["neighbor_id"] for n in graph.get_neighbors(store, "lesson:lesson_pool-reset", direction="out")}
    assert {"service:postgres", "error:operationalerror", "file:api/db.py"} <= nbrs
    known = {e["id"]: e for e in entities.entities(store)}
    assert known["service:postgres"]["mentions"] == 1
    boosts = graph.graph_boost_for_lessons(store, "postgres is throwing errors",
                                           ["lesson_pool-reset", "lesson_redis-timeouts", "lesson_unrelated"])
    assert boosts["lesson_pool-reset"] > 0 and boosts["lesson_unrelated"] == 0
    again = entities.link_lessons(store, use_spacy=False)
    assert again["new_links"] == 0 and again["retired_links"] == 0


def test_relinking_retires_entities_a_lesson_no_longer_names(store, tmp_memory):
    entities.link_lessons(store, use_spacy=False)
    write_lesson(tmp_memory, "lesson_redis-timeouts", description="Cache timeouts", rule="Raise the cache timeout.")
    out = entities.link_lessons(store, ["lesson_redis-timeouts"], use_spacy=False)
    assert out["retired_links"] >= 1
    nbrs = {n["neighbor_id"] for n in graph.get_neighbors(store, "lesson:lesson_redis-timeouts", direction="out")}
    assert "service:redis" not in nbrs


def test_duplicates_and_merge(store):
    graph.add_node(store, "organization:acme_corporation", "organization", "Acme Corporation")
    graph.add_node(store, "organization:acme_corp", "organization", "Acme Corp",
                   properties={"aliases": ["Acme Corporation"]})
    graph.add_edge(store, "person:ana", "organization:acme_corporation", "works_at")
    pairs = entities.duplicates(store)
    assert any({p["a"], p["b"]} == {"organization:acme_corp", "organization:acme_corporation"} for p in pairs)
    out = entities.merge(store, "organization:acme_corp", "organization:acme_corporation")
    assert out["edges_moved"] == 1
    nbrs = {n["neighbor_id"] for n in graph.get_neighbors(store, "person:ana", direction="out")}
    assert nbrs == {"organization:acme_corp"}
    with pytest.raises(ValueError):
        entities.merge(store, "organization:acme_corp", "organization:acme_corp")


def test_cli(store):
    def ct(*args):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "graph", *args, "--dest", store],  # nosec
                              capture_output=True, text=True, check=False)
    out = ct("extract", "Redis threw TimeoutError", "--spacy", "off")
    assert "service:redis" in out.stdout and "error:timeouterror" in out.stdout
    assert ct("link", "--spacy", "off").returncode == 0
    assert "service:postgres" in ct("entities").stdout
