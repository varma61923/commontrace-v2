from commontrace.client import MemoryClient
from commontrace.memory_record import rerank_profile_results


def test_only_scoped_user_dynamic_preferences_boost_matching_results():
    results = [
        {"id": "unrelated", "text": "A long and detailed answer", "score": 0.8},
        {"id": "preferred", "text": "Give concise answers", "score": 0.75},
    ]
    preferences = [
        {"bucket": "dynamic", "author": "user", "text": "I prefer concise answers", "weight": 1},
        {"bucket": "dynamic", "author": "assistant", "text": "I prefer concise answers", "weight": 10},
        {"bucket": "custom", "author": "user", "text": "I prefer concise answers", "weight": 10},
        {"bucket": "dynamic", "author": "user", "text": "I prefer not concise answers", "weight": 10},
    ]

    ranked = rerank_profile_results(results, preferences)

    assert [row["id"] for row in ranked] == ["preferred", "unrelated"]
    assert ranked[0]["score"] == 0.75  # Search's base evidence is left intact.


def test_effective_weight_changes_whether_a_matching_result_can_move_up():
    results = [
        {"id": "stronger-search-match", "text": "A long detailed answer", "score": 0.8},
        {"id": "preferred", "text": "Give concise answers", "score": 0.72},
    ]

    low_weight = rerank_profile_results(
        results,
        [{"bucket": "dynamic", "author": "user", "text": "I prefer concise answers", "weight": 0.01}],
    )
    high_weight = rerank_profile_results(
        results,
        [{"bucket": "dynamic", "author": "user", "text": "I prefer concise answers", "weight": 10}],
    )

    assert [row["id"] for row in low_weight] == ["stronger-search-match", "preferred"]
    assert [row["id"] for row in high_weight] == ["preferred", "stronger-search-match"]


def test_order_only_backend_uses_at_most_one_fifth_of_existing_window():
    results = [
        {"id": f"fact-{index:02}", "payload": {"text": "Give concise answers" if index == 1 else "Other fact"}}
        for index in range(20)
    ]
    preferences = [
        {"bucket": "dynamic", "author": "user", "text": "I prefer concise answers", "weight": 1}
    ]

    ranked = rerank_profile_results(results, preferences)

    assert ranked[0]["id"] == "fact-01"
    assert len(ranked) == len(results)


def test_preference_with_a_negative_clause_does_not_boost_either_side():
    results = [
        {"id": "positive", "text": "Concise answers", "score": 0.7},
        {"id": "negative", "text": "Verbose replies", "score": 0.69},
    ]

    ranked = rerank_profile_results(
        results,
        [{
            "bucket": "dynamic",
            "author": "user",
            "text": "I prefer concise answers, not verbose replies",
            "weight": 10,
        }],
    )

    assert ranked == results


def test_profile_preference_reranking_respects_decay_and_scope(tmp_path, monkeypatch):
    from commontrace import search_recipes

    results = [
        {"id": "unrelated", "text": "A long and detailed answer", "score": 0.8},
        {"id": "preferred", "text": "Give concise answers", "score": 0.75},
    ]
    monkeypatch.setattr(search_recipes, "search", lambda *args, **kwargs: list(results))

    owner = MemoryClient(str(tmp_path), agent_id="writer", context={"user": "alice"})
    owner.turn("capture", "", text="I prefer concise answers.")
    result = owner.turn("next", "draft a response")
    assert [row["id"] for row in result["results"]] == ["preferred", "unrelated"]
    assert result["profile"]["dynamic"][0]["weight"] > 0

    other = MemoryClient(str(tmp_path), agent_id="writer", context={"user": "bob"})
    private = other.turn("next", "draft a response")
    assert not private["profile"]["dynamic"]
    assert [row["id"] for row in private["results"]] == ["unrelated", "preferred"]
