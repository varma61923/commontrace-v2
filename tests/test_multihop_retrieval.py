"""Multi-hop retrieval, query expansion and triplet search over governed facts."""
from __future__ import annotations

import asyncio
import json
import math

import pytest

from commontrace import fact_store, graph, hierarchical, multihop, query_expansion, search_recipes, triplet_search
from commontrace.client import MemoryClient


@pytest.fixture(params=["jsonl", "sqlite"])
def root(tmp_path, request):
    (tmp_path / "commontrace.yaml").write_text(
        "profiles:\n  local:\n    embedding: none\n    reranker: none\n"
    )
    if request.param == "sqlite":
        fact_store.migrate(str(tmp_path))
    return str(tmp_path)


def add(root, text, **options):
    fact, _ = hierarchical.add_fact(root, text, valid_from="2025-01-01T00:00:00Z", **options)
    return fact


def corpus(root, **options):
    return {
        "bridge": add(root, "Microsoft acquired GitHub in 2018.", **options),
        "answer": add(root, "Bill Gates founded Microsoft in 1975.", **options),
        "sibling": add(root, "GitHub hosts the Linux kernel mirror.", **options),
        "noise": add(root, "The release train ships every Tuesday.", **options),
    }


QUESTION = "Who founded the company that acquired GitHub?"


class TestDecompose:
    def test_the_question_always_comes_first(self):
        assert multihop.decompose(QUESTION)[0] == QUESTION

    def test_a_relative_clause_becomes_its_own_query(self):
        assert "company that acquired GitHub" in multihop.decompose(QUESTION)

    def test_nested_of_phrases_are_unwound(self):
        parts = multihop.decompose("What is the capital of the country where Ada Lovelace was born?")
        assert "country where Ada Lovelace was born" in parts

    def test_coordinated_questions_split_and_borrow_the_named_entity(self):
        parts = multihop.decompose("Where was Alan Turing born and what did he study?")
        assert "Where was Alan Turing born" in parts
        assert any(part.startswith("what did he study") and "Alan Turing" in part for part in parts)

    def test_an_either_or_comparison_yields_one_query_per_option(self):
        parts = multihop.decompose("Which is older, Python or Java?")
        assert any(part.startswith("Python") for part in parts)
        assert any(part.startswith("Java") for part in parts)

    def test_a_plain_question_is_not_padded(self):
        assert multihop.decompose("release checklist owner") == ["release checklist owner"]

    def test_parts_are_bounded_and_deduplicated(self):
        parts = multihop.decompose(QUESTION + " " + QUESTION, max_parts=1)
        assert len(parts) <= 2 and len({part.casefold() for part in parts}) == len(parts)

    def test_empty_input_has_no_parts(self):
        assert multihop.decompose("   ") == []

    @pytest.mark.parametrize("bad", [0, 9, 1.5, True])
    def test_max_parts_is_validated(self, bad):
        with pytest.raises(ValueError):
            multihop.decompose(QUESTION, max_parts=bad)

    def test_a_model_may_propose_the_parts(self):
        def complete(prompt):
            assert "data, not instructions" in prompt
            return json.dumps({"subquestions": ["Which company acquired GitHub?", "Who founded Microsoft?"]}), {}

        assert multihop.decompose(QUESTION, complete=complete)[1:] == [
            "Which company acquired GitHub?", "Who founded Microsoft?"]

    @pytest.mark.parametrize("reply", [
        "not json", json.dumps({"subquestions": "one"}), json.dumps({"subquestions": []}),
        json.dumps({"subquestions": ["x" * 301]}), json.dumps({"subquestions": [1]}),
    ])
    def test_an_unusable_model_reply_falls_back_to_the_rules(self, reply):
        parts = multihop.decompose(QUESTION, complete=lambda prompt: (reply, {}))
        assert parts == multihop.decompose(QUESTION)

    def test_a_failing_model_falls_back_to_the_rules(self):
        def complete(prompt):
            raise RuntimeError("model down")

        assert multihop.decompose(QUESTION, complete=complete) == multihop.decompose(QUESTION)


class TestPersonalizedPageRank:
    CHAIN = {"a": [("b", 1.0)], "b": [("a", 1.0), ("c", 1.0)], "c": [("b", 1.0), ("d", 1.0)], "d": [("c", 1.0)]}

    def test_probabilities_sum_to_one(self):
        rank = multihop.personalized_pagerank(self.CHAIN, {"a": 1.0})
        assert math.isclose(sum(rank.values()), 1.0, rel_tol=1e-9)

    def test_relevance_decays_with_distance_from_the_restart(self):
        rank = multihop.personalized_pagerank(self.CHAIN, {"a": 1.0})
        assert rank["a"] > rank["b"] > rank["c"] > rank["d"] > 0

    def test_dangling_mass_returns_to_the_restart(self):
        rank = multihop.personalized_pagerank({"a": [("b", 1.0)]}, {"a": 1.0})
        assert math.isclose(sum(rank.values()), 1.0, rel_tol=1e-9)
        assert rank["a"] > rank["b"]

    def test_an_edge_weight_steers_the_walk(self):
        graph_ = {"s": [("x", 3.0), ("y", 1.0)], "x": [("s", 1.0)], "y": [("s", 1.0)]}
        rank = multihop.personalized_pagerank(graph_, {"s": 1.0})
        assert rank["x"] > rank["y"]

    def test_no_restart_mass_ranks_nothing(self):
        assert multihop.personalized_pagerank(self.CHAIN, {"a": 0.0}) == {}

    @pytest.mark.parametrize("restart", [{"a": float("nan")}, {"a": -1.0}, {"a": float("inf")}])
    def test_restart_weights_must_be_finite_and_non_negative(self, restart):
        with pytest.raises(ValueError):
            multihop.personalized_pagerank(self.CHAIN, restart)

    @pytest.mark.parametrize("weight", [float("nan"), -1.0])
    def test_edge_weights_must_be_finite_and_non_negative(self, weight):
        with pytest.raises(ValueError):
            multihop.personalized_pagerank({"a": [("b", weight)]}, {"a": 1.0})

    @pytest.mark.parametrize("damping", [0.0, 1.0, -0.1, 1.5])
    def test_damping_must_be_a_probability_strictly_inside_zero_and_one(self, damping):
        with pytest.raises(ValueError):
            multihop.personalized_pagerank(self.CHAIN, {"a": 1.0}, damping=damping)


class TestReason:
    def test_the_bridge_and_the_answer_are_retrieved_together(self, root):
        facts = corpus(root)
        out = MemoryClient(root).reason(QUESTION)
        top = [row["id"] for row in out["results"][:2]]
        assert set(top) == {facts["bridge"].id, facts["answer"].id}

    def test_the_evidence_chain_names_its_bridge_entity(self, root):
        facts = corpus(root)
        chain = MemoryClient(root).reason(QUESTION)["chains"][0]
        assert chain["facts"] == [facts["bridge"].id, facts["answer"].id]
        assert chain["via"] == ["microsoft"]
        assert "--[microsoft]-->" in chain["path"] and chain["delivered"]

    def test_the_bridge_chain_outranks_same_entity_distractors(self, root):
        # Every "X founded Company7" matches the whole question's words better than
        # the true first link does; the chain must still start from the inner clause.
        for person in ("Ann", "Ben", "Cal", "Dee", "Eve", "Fay", "Ida", "Jon", "Kim", "Lea", "Max", "Ned"):
            add(root, f"{person} founded Company7.")
            add(root, f"{person} founded Company{len(person) * 11}.")
        bridge = add(root, "Company80 acquired Company7.")
        answer = add(root, "Gus founded Company80.")
        add(root, "Hal founded Company55.")
        out = MemoryClient(root).reason("Who founded the company that acquired Company7?")
        assert out["chains"][0]["facts"] == [bridge.id, answer.id]
        assert [row["id"] for row in out["results"][:2]] == [bridge.id, answer.id]

    def test_chain_starts_lead_with_each_blocks_best_not_the_consensus(self):
        surface = [{"id": f"s{n}"} for n in range(8)]
        blocks = [
            ("question", surface, 1.0),
            ("part:1", [{"id": "bridge"}, *surface], 0.8),
            ("ppr", surface, 1.0),
        ]
        starts = [row["id"] for row in multihop._chain_starts(blocks)]
        # A fused ranking puts the rows every block agrees on first and pushes the
        # inner clause's answer past the hop frontier.
        assert starts[:multihop.HOP_FRONTIER] == ["bridge", "s0", "s1", "s2", "s3"]
        assert len(starts) == len(set(starts)) == 9


    def test_a_three_fact_chain_follows_two_bridges(self, root):
        start = add(root, "Ada Lovelace was born in London.")
        middle = add(root, "London is the capital of England.")
        add(root, "The release train ships every Tuesday.")
        out = MemoryClient(root).reason("What is the capital of the country where Ada Lovelace was born?")
        assert {start.id, middle.id} <= {row["id"] for row in out["results"][:3]}
        assert any(chain["via"] == ["london"] for chain in out["chains"])

    def test_results_carry_their_channels_and_hops_are_traced(self, root):
        corpus(root)
        out = MemoryClient(root).reason(QUESTION)
        assert all(row["channels"] for row in out["results"])
        assert out["hops"] and out["hops"][0]["bridges"]
        assert out["subquestions"] == ["company that acquired GitHub"]

    def test_the_registry_retriever_returns_the_same_ranking(self, root):
        corpus(root)
        client = MemoryClient(root)
        assert ([row["id"] for row in client.search(QUESTION, retriever="multi-hop", reranker="none")]
                == [row["id"] for row in client.reason(QUESTION, reranker="none")["results"]])

    def test_a_private_fact_is_never_a_bridge(self, root):
        # The answer shares no words with the question; only the private fact links them.
        add(root, "Acme acquired GitHub in 2018.", scopes=["agent:owner"])
        answer = add(root, "Wile Coyote leads Acme.")  # unscoped: everyone may see it
        add(root, "GitHub hosts the Linux kernel mirror.")
        reader = MemoryClient(root, agent_id="reader")
        for retriever in ("multi-hop", "ppr"):
            ids = {row["id"] for row in reader.search(QUESTION, retriever=retriever, reranker="none")}
            assert answer.id not in ids, retriever
        out = reader.reason(QUESTION)
        assert answer.id not in {row["id"] for row in out["results"]}
        assert all("acme" not in chain["via"] for chain in out["chains"])
        owner = MemoryClient(root, agent_id="owner").reason(QUESTION)
        assert answer.id in {row["id"] for row in owner["results"]}

    def test_a_forgotten_fact_is_neither_walked_nor_returned(self, root):
        facts = corpus(root)
        hierarchical.forget_fact(root, facts["bridge"].id)
        out = MemoryClient(root).reason(QUESTION)
        assert facts["bridge"].id not in {row["id"] for row in out["results"]}
        assert all(facts["bridge"].id not in chain["facts"] for chain in out["chains"])

    def test_valid_time_applies_to_every_hop(self, root):
        facts = corpus(root)
        later, _ = hierarchical.add_fact(root, "Bill Gates also founded Cascade in 2030.",
                                         valid_from="2030-01-01T00:00:00Z")
        out = MemoryClient(root).reason(QUESTION, as_of="2026-01-01T00:00:00Z")
        assert later.id not in {row["id"] for row in out["results"]}
        assert facts["answer"].id in {row["id"] for row in out["results"]}

    def test_a_single_hop_makes_no_chains(self, root):
        corpus(root)
        out = MemoryClient(root).reason(QUESTION, max_hops=1)
        assert out["chains"] == [] and out["hops"] == []

    @pytest.mark.parametrize("bad", [0, 4, "2", True])
    def test_max_hops_is_validated(self, root, bad):
        with pytest.raises(ValueError):
            multihop.reason(root, QUESTION, max_hops=bad)

    @pytest.mark.parametrize("question", ["", "   ", "x" * 4001, None])
    def test_the_question_is_validated(self, root, question):
        with pytest.raises(ValueError):
            multihop.reason(root, question)

    def test_an_empty_store_returns_nothing_rather_than_failing(self, root):
        out = multihop.reason(root, QUESTION)
        assert out["results"] == [] and out["chains"] == []

    def test_model_follow_up_queries_are_searched_and_traced(self, root):
        corpus(root)
        late = add(root, "Microsoft is headquartered in Redmond.")
        calls = []

        def complete(prompt):
            calls.append(prompt)
            if "sub-questions" in prompt:
                return json.dumps({"subquestions": ["Which company acquired GitHub?"]}), {}
            if len(calls) == 2:
                return json.dumps({"answerable": False, "next_query": "Microsoft headquarters Redmond"}), {}
            return json.dumps({"answerable": True}), {}

        out = multihop.reason(root, QUESTION, complete=complete)
        assert out["subquestions"] == ["Which company acquired GitHub?"]
        assert out["followups"][0]["query"] == "Microsoft headquarters Redmond"
        assert late.id in out["followups"][0]["found"]
        assert "untrusted reference data" in calls[1]

    def test_an_invalid_model_follow_up_is_refused(self, root):
        corpus(root)

        def complete(prompt):
            if "sub-questions" in prompt:
                return json.dumps({"subquestions": ["x?"]}), {}
            return json.dumps({"answerable": "maybe"}), {}

        with pytest.raises(ValueError):
            multihop.reason(root, QUESTION, complete=complete)


class TestPprRetriever:
    def test_walk_scores_carry_the_ppr_signal(self, root):
        facts = corpus(root)
        rows = MemoryClient(root).search(QUESTION, retriever="ppr", reranker="none")
        by_id = {row["id"]: row for row in rows}
        assert facts["answer"].id in by_id
        assert by_id[facts["answer"].id]["signals"]["ppr"] > 0

    def test_a_fact_with_no_shared_words_is_reached_through_an_entity(self, root):
        add(root, "Microsoft acquired GitHub in 2018.")
        reached = add(root, "Satya Nadella leads Microsoft.")
        scores, walk = multihop.walk_scores(root, "GitHub purchase", {})
        assert reached.id in walk.facts and scores[reached.id] > 0

    def test_graph_hops_zero_leaves_out_the_knowledge_graph(self, root):
        answer = add(root, "the registered office is in bristol")
        graph.add_node(root, "person:alice", "person", "Alice")
        graph.add_node(root, answer.id, "memory", answer.statement)
        graph.add_edge(root, "person:alice", answer.id, "mentions")
        with_graph, _ = multihop.walk_scores(root, "Alice", {})
        without, _ = multihop.walk_scores(root, "Alice", {}, graph_hops=0)
        assert answer.id in with_graph and answer.id not in without

    def test_an_unreadable_entity_index_degrades_to_the_graph_and_search(self, root, monkeypatch):
        facts = corpus(root)
        monkeypatch.setattr(multihop, "_entity_index", lambda root: ({}, {}))
        rows = MemoryClient(root).search(QUESTION, retriever="ppr", reranker="none")
        assert facts["bridge"].id in {row["id"] for row in rows}


class TestQueryExpansion:
    def test_feedback_terms_exclude_the_query_and_prefer_recurring_words(self):
        terms = query_expansion.prf_terms("billing owner", [
            ("Dana runs the billing service and invoices", 1.0),
            ("Dana approves billing refunds", 0.8),
            ("Unrelated zebra fact", 0.1),
        ])
        assert "billing" not in terms and terms[0] == "dana"
        assert "zebra" not in terms or terms.index("dana") < terms.index("zebra")

    def test_no_positive_feedback_means_no_terms(self):
        assert query_expansion.prf_terms("q", [("text", 0.0), ("more", float("nan"))]) == []

    @pytest.mark.parametrize("kwargs", [{"max_terms": -1}, {"max_terms": 65}, {"max_docs": 0}, {"max_docs": 51}])
    def test_bounds_are_validated(self, kwargs):
        with pytest.raises(ValueError):
            query_expansion.prf_terms("q", [("text", 1.0)], **kwargs)

    def test_expansion_reaches_a_fact_the_query_alone_misses(self, root):
        add(root, "Dana owns the billing service.")
        add(root, "Dana approves every billing refund.")
        target = add(root, "Dana is on call this weekend.")
        client = MemoryClient(root)
        plain = {row["id"] for row in client.search("billing", reranker="none", graph_hops=0)}
        explain = {}
        grown = client.search("billing", retriever="expanded", reranker="none", graph_hops=0, explain=explain)
        assert target.id not in plain and target.id in {row["id"] for row in grown}
        assert "dana" in explain["expansion_terms"]

    def test_the_original_results_are_kept_ahead_of_expansions(self, root):
        first = add(root, "billing invoices are due monthly")
        add(root, "monthly reports are due friday")
        rows = MemoryClient(root).search("billing invoices", retriever="expanded", reranker="none", graph_hops=0)
        assert rows[0]["id"] == first.id

    def test_hyde_searches_the_hypothetical_answer(self, root):
        target = add(root, "Dana owns the billing service.")
        add(root, "The release train ships every Tuesday.")
        explain = {}
        rows = query_expansion.hyde(
            root, "Who should I ask about invoices?", explain=explain,
            complete=lambda prompt: (json.dumps({"answer": "Dana owns the billing service."}), {}))
        assert rows[0]["id"] == target.id
        assert explain["retriever"] == "hyde" and "hypothesis" in rows[0]["channels"]

    def test_hyde_without_a_model_falls_back_to_feedback_expansion(self, root, monkeypatch):
        add(root, "Dana owns the billing service.")
        monkeypatch.delenv("COMMONTRACE_LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        explain = {}
        rows = MemoryClient(root).search("billing", retriever="hyde", reranker="none", explain=explain)
        assert rows and explain["retriever"] == "expanded" and explain["hyde_fallback"]

    def test_a_bad_hypothesis_reply_falls_back_too(self, root):
        add(root, "Dana owns the billing service.")
        explain = {}
        rows = query_expansion.hyde(root, "billing", explain=explain, complete=lambda prompt: ("{}", {}))
        assert rows and explain["hyde_fallback"] == "LLMDraftRejected"

    def test_expansion_respects_scope(self, root):
        add(root, "Dana owns the billing service.", scopes=["agent:other"])
        assert MemoryClient(root, agent_id="reader").search("billing", retriever="expanded") == []
        assert MemoryClient(root, agent_id="reader").search("billing", retriever="hyde") == []


def _relation(root, subject, relation, obj, **properties):
    graph.add_edge(root, subject, obj, relation, properties=properties or None)


class TestTripletSearch:
    def test_a_triplet_matching_two_elements_outranks_one_matching_one(self, root):
        for node, kind, name in (("person:dana", "person", "Dana"), ("org:meridian", "organization", "Meridian"),
                                 ("org:acme", "organization", "Acme"), ("place:bristol", "location", "Bristol")):
            graph.add_node(root, node, kind, name)
        _relation(root, "person:dana", "works_at", "org:meridian")
        _relation(root, "org:acme", "located_in", "place:bristol")
        _relation(root, "org:meridian", "located_in", "place:bristol")
        rows = triplet_search.search(root, "who works at Meridian")
        assert rows[0]["text"] == "Dana works at Meridian"
        assert rows[0]["score"] > rows[1]["score"] if len(rows) > 1 else True
        assert 0 < rows[0]["score"] <= 1 and rows[0]["similarity"]["relation"] > 0.9

    def test_a_relation_from_a_private_fact_is_withheld(self, root):
        private = add(root, "Dana works at Meridian.", scopes=["agent:owner"])
        graph.add_node(root, "person:dana", "person", "Dana")
        graph.add_node(root, "org:meridian", "organization", "Meridian")
        _relation(root, "person:dana", "works_at", "org:meridian", source=f"fact:{private.id}")
        reader = MemoryClient(root, agent_id="reader")
        owner = MemoryClient(root, agent_id="owner")
        assert reader.triplets("Dana works at Meridian") == []
        assert owner.triplets("Dana works at Meridian")[0]["source"] == f"fact:{private.id}"

    @pytest.mark.parametrize("prefix", ["", "memory:"])
    def test_an_edge_touching_a_private_fact_node_is_withheld(self, root, prefix):
        private = add(root, "the meridian office lease", scopes=["agent:owner"])
        graph.add_node(root, "org:meridian", "organization", "Meridian")
        graph.add_node(root, prefix + private.id, "memory", private.statement)
        _relation(root, prefix + private.id, "mentions", "org:meridian")
        assert MemoryClient(root, agent_id="reader").triplets("meridian office lease") == []
        assert MemoryClient(root, agent_id="owner").triplets("meridian office lease")

    def test_valid_time_selects_the_triplets(self, root):
        graph.add_node(root, "person:dana", "person", "Dana")
        graph.add_node(root, "org:meridian", "organization", "Meridian")
        graph.add_edge(root, "person:dana", "org:meridian", "works_at", valid_at="2030-01-01T00:00:00Z")
        assert triplet_search.search(root, "Dana works at Meridian", as_of="2026-01-01T00:00:00Z") == []
        assert triplet_search.search(root, "Dana works at Meridian", as_of="2031-01-01T00:00:00Z")

    def test_relations_filter_and_bounds(self, root):
        graph.add_node(root, "person:dana", "person", "Dana")
        graph.add_node(root, "org:meridian", "organization", "Meridian")
        _relation(root, "person:dana", "works_at", "org:meridian")
        assert triplet_search.search(root, "Dana Meridian", relations=["located_in"]) == []
        assert triplet_search.search(root, "Dana Meridian", relations=["works_at"])
        for bad in ({"top_k": 0}, {"top_k": 101}, {"relations": "works_at"}):
            with pytest.raises(ValueError):
                triplet_search.search(root, "Dana", **bad)
        with pytest.raises(ValueError):
            triplet_search.search(root, "  ")

    def test_an_injected_embedder_drives_semantic_matching(self, root):
        graph.add_node(root, "person:dana", "person", "Dana")
        graph.add_node(root, "team:billing", "team", "Billing")
        _relation(root, "person:dana", "owned_by", "team:billing")

        class Embedder:
            def embed(self, texts, *, query):
                vocab = ["invoices", "billing", "dana", "owned"]
                out = []
                for text in texts:
                    words = text.lower().split()
                    vector = [1.0 if term in words or (term == "billing" and "invoices" in words) else 0.0
                              for term in vocab]
                    out.append(vector if any(vector) else [0.0, 0.0, 0.0, 1e-3])
                return out

        explain = {}
        rows = triplet_search.search(root, "invoices", embedder=Embedder(), explain=explain)
        assert explain["method"] == "embedding" and rows and rows[0]["object"]["id"] == "team:billing"


class TestSurfaces:
    def test_the_new_retrievers_are_registered(self):
        assert {"multi-hop", "ppr", "expanded", "hyde"} <= set(search_recipes.REGISTRY.names())

    def test_mcp_memory_reason_and_triplet_search(self, root):
        pytest.importorskip("mcp")
        from commontrace import mcp_server

        facts = corpus(root, scopes=["agent:reader"])
        graph.add_node(root, "person:dana", "person", "Dana")
        graph.add_node(root, "org:meridian", "organization", "Meridian")
        _relation(root, "person:dana", "works_at", "org:meridian")
        server = mcp_server.build_server(root)

        def call(tool, **arguments):
            result = asyncio.run(server.call_tool(tool, arguments))
            structured = getattr(result, "structured_content", None)
            return structured.get("result", structured) if structured else json.loads(result.content[0].text)

        reasoned = call("memory_reason", question=QUESTION, context=["agent:reader"])
        assert reasoned["ok"] and reasoned["chains"][0]["facts"][-1] == facts["answer"].id
        assert call("memory_reason", question=QUESTION, context=["agent:other"])["results"] == []
        assert call("memory_reason", question=QUESTION, max_hops=9)["ok"] is False
        triplets = call("graph_triplet_search", query="who works at Meridian")
        assert triplets["ok"] and triplets["triplets"][0]["relation"] == "works_at"

    def test_the_cli_reason_and_triplets_commands(self, root, capsys):
        from commontrace.cli import main

        corpus(root)
        assert main(["evolve", "reason", QUESTION, "--dest", root]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["chains"] and out["results"]
        assert main(["evolve", "triplets", "anything", "--dest", root]) == 0
        assert json.loads(capsys.readouterr().out) == []

    def test_reason_and_triplets_are_local_only(self):
        client = MemoryClient(".", url="https://memory.example.com", token="t")
        with pytest.raises(ValueError, match="local store"):
            client.reason(QUESTION)
        with pytest.raises(ValueError, match="local store"):
            client.triplets("x")


@pytest.mark.parametrize("prefix", ["", "memory:"])
def test_a_question_naming_a_private_fact_node_cannot_walk_through_it(root, prefix):
    # The question matches the private node's name, so the graph proposes the node
    # itself as a seed. It must be classified as an inadmissible fact before the
    # walk expands from it, not mistaken for an ordinary entity.
    private = add(root, "zephyr ledger reconciliation", scopes=["agent:other"])
    target = add(root, "a hidden route to public evidence", scopes=["agent:reader"])
    graph.add_node(root, prefix + private.id, "memory", private.statement)
    graph.add_node(root, target.id, "memory", target.statement)
    graph.add_edge(root, prefix + private.id, target.id, "derived_from")
    scores, walk = multihop.walk_scores(root, "zephyr ledger reconciliation", {},
                                        context=["agent:reader"])
    assert private.id not in walk.facts and target.id not in scores
    rows = MemoryClient(root, agent_id="reader").search(
        "zephyr ledger reconciliation", retriever="ppr", reranker="none")
    assert target.id not in {row["id"] for row in rows}
