"""Evidence-fusion typing: shapes, name lists, stated types, context cues, the store's
exemplars, sibling back-off, batching, and extraction that keeps weak candidates only
when they can be typed."""
import json
import os

import pytest

from benchmarks import ontology_typing_eval
from commontrace import entities, graph, ontology, ontology_classify

ONTO = ontology.Ontology.default()


def typed(name, context=""):
    return ontology_classify.classify(name, context or name, onto=ONTO, methods=("keyword",))


@pytest.mark.parametrize("name, expected", [
    ("src/app/main.py", "file"), ("/etc/passwd", "file"), ("Dockerfile", "file"), (".env", "file"),
    ("parse_header", "symbol"), ("handleSubmit", "symbol"), ("std::vector", "symbol"), ("get_user()", "symbol"),
    ("MAX_RETRIES", "symbol"), ("KeyError", "error"), ("ECONNRESET", "error"), ("ERR_TOKEN_MALFORMED", "error"),
    ("404 Not Found", "error"), ("RFC 7231", "document"), ("us-east-1", "place"), ("Sprint 14", "event"),
    ("Initech Corp", "organization"), ("ledger-svc", "service"),
])
def test_mention_shapes(name, expected):
    assert typed(name).type == expected


def test_shapes_do_not_misread_prose():
    assert typed("and/or", "Use one and/or the other.").type is None  # not a path
    assert typed("Node.js", "We run Node.js 20.").type == "tool"  # a framework, not a file


@pytest.mark.parametrize("name, context, expected", [
    ("Velmora", "Velmora, a small town in the hills, hosts the retreat.", "place"),
    ("Pellucid", "Pellucid is a library for parsing PDFs.", "tool"),
    ("Wexley", "Wexley is our largest customer.", "organization"),
    ("Jaxon Vale", "The lead architect for this project is Jaxon Vale.", "person"),
    ("Thistle", "Frameworks such as Thistle make agents easy.", "tool"),
])
def test_stated_types(name, context, expected):
    found = typed(name, context)
    assert found.type == expected and found.method == "pattern"


def test_the_type_noun_must_end_its_noun_phrase():
    # "a standard error code" names a code, not a standard (document) or the theme of a quarter (event)
    assert typed("404 Not Found", "The 404 Not Found is a standard error code.").type == "error"
    assert typed("Observability", "Observability was the theme of the quarter.").type is None


@pytest.mark.parametrize("name, context, expected", [
    ("Brannigan", "Brannigan approved the change yesterday.", "person"),
    ("Tarnhold", "She lives in Tarnhold and commutes by train.", "place"),
    ("Quorvex", "Quorvex signed a two-year contract with us.", "organization"),
    ("flumectl", "Run flumectl sync to refresh the local mirror.", "tool"),
    ("Halyard", "Every write goes to Halyard, our internal key-value store.", "service"),
    ("GlimmerError", "The loader throws GlimmerError on malformed input.", "error"),
    ("Driftcon", "We demoed the agent during Driftcon.", "event"),
    ("Brindle Charter", "The Brindle Charter forbids force pushes.", "document"),
    ("frobnicate", "Call `frobnicate` before saving.", "symbol"),
])
def test_context_cues(name, context, expected):
    assert typed(name, context).type == expected


def test_independent_evidence_combines_by_noisy_or():
    found = typed("Lyon", "Lyon, a city in France, hosts our office.")
    sources = {source for source, _type, _score in found.evidence}
    assert found.type == "place" and {"lexicon", "pattern"} <= sources
    assert found.confidence == pytest.approx(1 - (1 - 0.88) * (1 - 0.88))


def test_a_personal_name_needs_a_cue():
    assert typed("Kaelen Voss", "Kaelen Voss.").type is None  # the name form alone is not enough
    assert typed("Kaelen Voss", "Kaelen Voss should review the pull request.").type == "person"
    assert typed("Aetheria Systems", "Aetheria Systems should review it.").type == "organization"


def test_abstract_nouns_stay_untyped():
    for name, context in (("Latency", "Latency matters more than throughput here."),
                          ("Kindness", "Kindness goes a long way in code review."),
                          ("Caching", "Caching hides the slow query.")):
        assert typed(name, context).type is None


def test_near_tied_siblings_back_off_to_their_parent(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "ontology.yaml").write_text(
        "entity_types:\n  database: {parent: service, keywords: [database]}\n"
        "  cache: {parent: service, keywords: [cache]}\n", encoding="utf-8")
    onto = ontology.load(str(tmp_path))
    found = ontology_classify.classify("Orders Database Cache", onto=onto, methods=("keyword",))
    assert found.type == "cache"  # the head word decides
    tied = ontology_classify.select(onto, {"database": 0.8, "cache": 0.79}, ["cache", "database", "service"], 0.6)
    assert tied == ("service", 0.8)
    assert ontology_classify.select(onto, {"database": 0.8, "cache": 0.7}, ["cache", "database", "service"],
                                    0.6) == ("database", 0.8)


def test_exemplars_learn_the_store_vocabulary(tmp_path):
    root = str(tmp_path)
    graph.add_node(root, "service:ledger_relay", "service", name="Ledger Relay")
    graph.add_node(root, "service:billing_relay", "service", name="Billing Relay")
    graph.add_node(root, "service:audit_relay", "service", name="Audit Relay")
    exemplars = ontology_classify.Exemplars.from_graph(root)
    assert ontology_classify.Exemplars.from_graph(root) is exemplars  # cached until the graph changes
    known = ontology_classify.classify("Ledger Relay", "", onto=ONTO, methods=("keyword",), exemplars=exemplars)
    assert (known.type, known.method) == ("service", "exemplar")
    new = ontology_classify.classify("Payments Relay", "", onto=ONTO, methods=("keyword",), exemplars=exemplars)
    assert (new.type, new.method) == ("service", "exemplar")  # three relays were services: so is a fourth
    graph.add_node(root, "service:x_relay", "organization", name="X Relay")
    graph.add_node(root, "service:y_relay", "organization", name="Y Relay")
    mixed = ontology_classify.Exemplars.from_graph(root)
    assert mixed.scores("Payments Relay", ["service", "organization"]) == {}  # 3 of 5 is not 80%


class CountingEmbedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts, *, query):
        self.calls.append((len(texts), query))
        return [[1.0, 0.0] if "warehouse" in t.lower() or "store" in t.lower() else [0.0, 1.0] for t in texts]


def test_classify_many_embeds_undecided_mentions_in_one_call():
    fake = CountingEmbedder()
    mentions = [("KeyError", "It raised a KeyError."), ("Zorblax", "the zorblax warehouse"),
                ("Qux", "qux warehouse")]
    found = ontology_classify.classify_many(mentions, onto=ONTO, embedder=fake, methods=("keyword", "embedding"))
    assert found[0].type == "error"
    assert [call for call in fake.calls if call[1]] == [(2, True)]  # one query batch for the two undecided


def test_embedding_tier_abstains_without_a_margin():
    class Flat:
        def embed(self, texts, *, query):
            return [[1.0, 1.0] for _ in texts]

    found = ontology_classify.classify("Zorblax", "Zorblax did a thing.", onto=ONTO, embedder=Flat(),
                                       methods=("embedding",))
    assert found.type is None


def test_extraction_keeps_sentence_initial_names_only_when_typed():
    found = {m.name: m.type for m in entities.extract(
        "Marcus approved the rollback. Quorvex signed a contract with us. Latency matters. "
        "Run flumectl sync, then raise MAX_RETRIES in config/settings.yaml.", onto=ONTO, use_spacy=False)}
    assert found["Marcus"] == "person" and found["Quorvex"] == "organization"
    assert found["flumectl"] == "tool" and found["MAX_RETRIES"] == "symbol"
    assert found["config/settings.yaml"] == "file"
    assert "Latency" not in found and "Run" not in found


def test_extraction_trims_sentence_verbs_and_finds_compound_names():
    found = {m.name: m.type for m in entities.extract(
        "Follow the Style Guide. The Platform team owns the March outage review. We demoed at KubeCon.",
        onto=ONTO, use_spacy=False)}
    assert found.get("Style Guide") == "document"
    assert found.get("Platform team") == "organization"
    assert found.get("KubeCon") == "event"


def test_typing_benchmark_reports_slices():
    report = ontology_typing_eval.run()
    assert report["classifier"]["examples"] == 124
    assert set(report["classifier"]["by_slice"]) == {"abstract", "known-name", "novel-name"}
    assert report["pipeline"]["typed_precision"] >= 0.95  # regression gate on the hand-written set
    here = os.path.dirname(ontology_typing_eval.DEFAULT_FIXTURE)
    dev = ontology_typing_eval.run(os.path.join(here, "ontology_typing_gemma_dev.json"))
    assert dev["classifier"]["accuracy"] >= 0.85  # regression gate on the model-written dev set


def test_fixture_rows_are_self_consistent():
    for path in ("ontology_typing_v1.json", "ontology_typing_gemma_dev.json"):
        with open(os.path.join(os.path.dirname(ontology_typing_eval.DEFAULT_FIXTURE), path), encoding="utf-8") as fh:
            rows = json.load(fh)["examples"]
        assert all(r["name"] in r["context"] for r in rows)
        assert len({r["id"] for r in rows}) == len(rows)
