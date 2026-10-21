import json

import pytest

from unimem import additive_extract, hierarchical


def test_observation_date_and_assistant_authorship(tmp_path):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return json.dumps({"facts": [{"statement": "The deployment was rolled back on 2026-10-09."}]}), {}

    result = additive_extract.extract(
        str(tmp_path),
        "Yesterday the deployment was rolled back.",
        speaker="assistant",
        observed_at="2026-10-10T00:00:00Z",
        complete=complete,
    )
    assert "2026-10-10" in prompts[0]
    assert result["facts"][0]["statement"].startswith("The assistant stated")
    assert additive_extract.extract(str(tmp_path), "echo", echo_of="echo", complete=complete)["calls"] == 0
    with pytest.raises(ValueError):
        additive_extract.extract(str(tmp_path), "text", speaker="unknown", complete=complete)


def test_transition_preserves_both_states_and_observation_date(tmp_path):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "Alice switched from the local cache to Redis on 2026-10-09 after local invalidation proved unreliable.",
                        "valid_from": "2026-10-09",
                    }
                ]
            }
        ), {}

    result = additive_extract.extract(
        str(tmp_path),
        "Yesterday Alice switched from the local cache to Redis after local invalidation proved unreliable.",
        observed_at="2026-10-10T00:00:00Z",
        complete=complete,
    )

    prompt = prompts[0]
    assert "current execution date" in prompt
    assert "bounded approximate period" in prompt
    assert "both its prior and new state" in prompt
    inputs = json.loads(prompt.split("Input follows:\n", 1)[1])
    assert inputs["observed_at"] == "2026-10-10T00:00:00Z"
    fact = result["facts"][0]
    assert fact["valid_from"] == "2026-10-09T00:00:00+00:00"
    assert "from the local cache to Redis" in fact["statement"]


def test_tool_extraction_keeps_tool_attribution_and_source_lineage(tmp_path):
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return json.dumps({"facts": [{"statement": "The lookup returned a 91% cache hit rate."}]}), {}

    result = additive_extract.extract(
        str(tmp_path),
        "Cache hit rate: 91%.",
        speaker="tool",
        source_trace_id="lookup-42",
        complete=complete,
    )

    fact = result["facts"][0]
    assert "unverified tool result" in additive_extract.PROMPT
    assert json.loads(prompts[0].split("Input follows:\n", 1)[1])["speaker"] == "tool"
    assert fact["statement"] == "Tool output: The lookup returned a 91% cache hit rate."
    assert fact["source_traces"] == ["lookup-42"]


def test_local_tool_extraction_also_keeps_tool_attribution(tmp_path):
    result = additive_extract.extract(
        str(tmp_path),
        "The search returned three results.",
        speaker="tool",
        local=True,
    )

    assert result["calls"] == 0
    assert result["facts"][0]["statement"] == "Tool output: The search returned three results."


def test_user_extraction_does_not_gain_assistant_or_tool_attribution(tmp_path):
    result = additive_extract.extract(
        str(tmp_path),
        "Alice prefers green tea.",
        complete=lambda _prompt: (json.dumps({"facts": [{"statement": "Alice prefers green tea."}]}), {}),
    )

    assert result["facts"][0]["statement"] == "Alice prefers green tea."


def test_verbatim_fast_path_skips_model(tmp_path):
    hierarchical.add_fact(str(tmp_path), "Parser validation uses pytest.")

    def unavailable(prompt):
        raise AssertionError("verbatim admission needs no model")

    result = additive_extract.extract(str(tmp_path), "Parser validation uses pytest.", complete=unavailable)
    assert result["calls"] == 0
    assert result["facts"][0]["action"] != "ADD"
