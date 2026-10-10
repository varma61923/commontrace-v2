import json

import pytest

from commontrace import additive_extract, hierarchical


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


def test_verbatim_fast_path_skips_model(tmp_path):
    hierarchical.add_fact(str(tmp_path), "Parser validation uses pytest.")

    def unavailable(prompt):
        raise AssertionError("verbatim admission needs no model")

    result = additive_extract.extract(str(tmp_path), "Parser validation uses pytest.", complete=unavailable)
    assert result["calls"] == 0
    assert result["facts"][0]["action"] != "ADD"
