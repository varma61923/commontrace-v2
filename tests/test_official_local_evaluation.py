"""The reproduction helper must preserve evidence and keep inference on loopback."""
from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from research.run_official_local import balanced, local_endpoint, main, sha256, subset


@pytest.mark.parametrize("url", ["https://127.0.0.1/v1", "http://api.example.com/v1",
                                "http://localhost.example.com/v1", "http://local:secret@localhost/v1",
                                "http://127.0.0.1/v1?key=secret", "http://127.0.0.1/v1#x"])
def test_provider_endpoint_rejects_remote_and_credentialed_urls(url):
    with pytest.raises(argparse.ArgumentTypeError):
        local_endpoint(url)


@pytest.mark.parametrize("url", ["http://localhost:8080/v1", "http://127.0.0.1:18081/v1",
                                "http://[::1]:8080/v1/"])
def test_provider_endpoint_accepts_only_explicit_loopback(url):
    assert local_endpoint(url) == url.rstrip("/")


def test_balanced_selection_is_reproducible_and_preserves_source():
    items = [{"category": category, "id": index} for category in ("a", "b", "c") for index in range(5)]
    original = json.dumps(items)
    result = balanced(items, "category", 2, 2026)
    assert result == balanced(items, "category", 2, 2026)
    assert [q["category"] for q in result] == ["a", "a", "b", "b", "c", "c"]
    assert json.dumps(items) == original


def test_locomo_subset_keeps_complete_source_conversation(tmp_path):
    source = tmp_path / "source.json"
    conversation = {"session_1": [{"text": "Exact raw evidence", "dia_id": "D1:1"}],
                    "session_1_date_time": "2026-01-01"}
    source.write_text(json.dumps([{"sample_id": "sample", "conversation": conversation,
                                   "qa": [{"category": category, "question": f"Question {category}-{index}",
                                           "answer": "gold", "evidence": ["D1:1"]}
                                          for category in (1, 2, 3, 4, 5) for index in range(3)]}]))
    original_hash = sha256(source)
    output = tmp_path / "subset.json"
    args = SimpleNamespace(data=str(source), dataset="locomo", seed=2026,
                           per_category=1, conversation_index=0)
    metadata = subset(args, output)
    selected = json.loads(output.read_text())[0]
    assert sha256(source) == original_hash == metadata["source_sha256"]
    assert selected["conversation"] == conversation
    assert len(selected["qa"]) == metadata["questions"] == 4
    assert {q["category"] for q in selected["qa"]} == {1, 2, 3, 4}
    assert all(q["evidence"] == ["D1:1"] for q in selected["qa"])
    original_questions = json.loads(source.read_text())[0]["qa"]
    assert [original_questions[i] for i in metadata["selected_original_indices"]] == selected["qa"]


def test_existing_results_are_never_overwritten(tmp_path):
    output = tmp_path / "results"
    output.mkdir()
    recorded = output / "existing.json"
    recorded.write_text("recorded result")
    with pytest.raises(SystemExit) as error:
        main(["--dataset", "locomo", "--data", "unavailable", "--checkout", "unavailable",
              "--output", str(output)])
    assert error.value.code == 2
    assert recorded.read_text() == "recorded result"
