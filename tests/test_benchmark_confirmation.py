"""Excluded-question confirmation keeps source identity and refuses loose caps."""
from __future__ import annotations

import copy
import json

import pytest

from benchmarks import confirmation
from benchmarks import conversation_bench as bench
from tests.test_benchmark_measurement import _args


def test_exclusions_precede_sampling_and_preserve_question_identity(tmp_path):
    args = _args(tmp_path, limit=2)
    path = tmp_path / "excluded.json"
    path.write_text(json.dumps(["c0-0"]))
    args.question_exclusions = str(path)
    cases = bench._load_cases(args)
    assert [q["id"] for *_, qs in cases for q in qs] == ["c1-0", "c2-0"]
    assert cases[0][1][0][2][0]["text"] == "The orchid launch completed on Tuesday."
    first = bench.run(args)[1500]
    assert first["provenance"]["sampling"]["question_exclusions_sha256"]
    path.write_text(json.dumps(["c1-0"]))
    second = bench.run(args)[1500]
    assert {r["id"] for r in second["rows"]} == {"c0-0", "c2-0"}
    assert first["chunk_set"]["path"] != second["chunk_set"]["path"]


@pytest.mark.parametrize("identities", [["c0-0", "c0-0"], ["unknown"], [None], "c0-0"])
def test_invalid_or_foreign_exclusion_file_refused(tmp_path, identities):
    args = _args(tmp_path)
    path = tmp_path / "excluded.json"
    path.write_text(json.dumps(identities))
    args.question_exclusions = str(path)
    with pytest.raises(ValueError, match="exclusions"):
        bench._load_cases(args)


def test_exclusion_drift_during_retrieval_invalidates_run(tmp_path, monkeypatch):
    args = _args(tmp_path)
    path = tmp_path / "excluded.json"
    path.write_text(json.dumps(["c0-0"]))
    args.question_exclusions = str(path)
    native = bench.recall

    def recall(*a, **kw):
        path.write_text(json.dumps(["c1-0"]))
        return native(*a, **kw)

    monkeypatch.setattr(bench, "recall", recall)
    with pytest.raises(RuntimeError, match="exclusions changed"):
        bench.run(args)


def test_development_exclusions_require_bound_unique_identities():
    selection = {"input_sha256": confirmation.INPUTS["locomo"][1], "question_ids": ["q1"]}
    manifest = {"selection": {"locomo": selection}}
    assert confirmation.development_exclusions(manifest, "locomo") == ["q1"]
    selection["question_ids"] = ["q1", "q1"]
    with pytest.raises(ValueError, match="unique"):
        confirmation.development_exclusions(manifest, "locomo")
    selection.update(question_ids=["q1"], input_sha256="foreign")
    with pytest.raises(ValueError, match="dataset bytes"):
        confirmation.development_exclusions(manifest, "locomo")


@pytest.mark.parametrize("fault", ["estimated", "counter", "attempts", "exclusions", "reference", "excess", "boolean", "disagreement"])
def test_completed_confirmation_requires_actual_fitting_text_count(fault):
    payload = {"1000": {"budget": 1000,
        "overall": {"n": 1, "accuracy": None}, "by_type": {}, "bootstrap_95ci": {}, "retrieval_profile": {},
        "provenance": {"evaluation": {"context_budget": {"unit": "selected-tokenizer-context-text"}}},
        "rows": [{"context_text_tokens": 900, "context_budget": {"text_tokens": 900}}]}}
    contract = payload["1000"]["provenance"]["evaluation"]["context_budget"]
    payload["1000"]["provenance"]["evaluation"].update(context_text_tokenizer="tiktoken:cl100k_base", answer_enabled=False)
    payload["1000"]["provenance"]["sampling"] = {"question_exclusions_sha256": "bound"}
    payload["1000"]["modes_detail"] = {"memory": copy.deepcopy(payload["1000"]), "full-context": copy.deepcopy(payload["1000"])}
    confirmation.validate_strict(payload, contract, "bound")
    broken = copy.deepcopy(payload)
    if fault == "estimated":
        broken["1000"]["modes_detail"]["memory"]["provenance"]["evaluation"]["context_budget"]["unit"] = "estimated"
    elif fault == "counter":
        broken["1000"]["modes_detail"]["memory"]["provenance"]["evaluation"]["context_text_tokenizer"] = "other"
    elif fault == "attempts":
        broken["1000"]["modes_detail"]["memory"]["provenance"]["evaluation"]["context_budget"]["max_attempts"] = 100
    elif fault == "exclusions":
        broken["1000"]["modes_detail"]["memory"]["provenance"]["sampling"]["question_exclusions_sha256"] = "foreign"
    elif fault == "reference":
        broken["1000"]["modes_detail"]["full-context"]["provenance"]["evaluation"]["context_text_tokenizer"] = "other"
    elif fault == "excess":
        broken["1000"]["rows"][0]["context_text_tokens"] = 1001
    elif fault == "boolean":
        broken["1000"]["rows"][0]["context_text_tokens"] = True
    else:
        broken["1000"]["rows"][0]["context_budget"]["text_tokens"] = 899
    with pytest.raises(ValueError):
        confirmation.validate_strict(broken, contract, "bound")


@pytest.mark.parametrize("missing", ["overall", "by_type", "bootstrap_95ci", "retrieval_profile"])
def test_incomplete_scorecard_refused_before_marking_attempt_completed(missing):
    payload = {"1000": {"overall": {}, "by_type": {}, "bootstrap_95ci": {}, "retrieval_profile": {}}}
    del payload["1000"][missing]
    with pytest.raises(ValueError, match="scorecard fields"):
        confirmation.validate_strict(payload, {}, "bound")
