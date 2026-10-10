"""The development runner rejects foreign input and protects existing outputs."""
from __future__ import annotations

import argparse

import pytest

from benchmarks import phase0


def test_changed_official_input_refused_before_creating_output(tmp_path):
    (tmp_path / 'locomo10.json').write_text('[]')
    output = tmp_path / 'out'
    args = argparse.Namespace(data_dir=str(tmp_path), output=str(output), seed=19, baseline_commit='HEAD')
    with pytest.raises(ValueError, match='pinned official bytes'):
        phase0.run(args)
    assert not output.exists()


def test_existing_output_refused_without_overwriting_evidence(tmp_path, monkeypatch):
    output = tmp_path / 'out'
    output.mkdir()
    marker = output / 'evidence.json'
    marker.write_text('{"historical":true}')
    monkeypatch.setattr(phase0, 'dataset_digest', lambda path: phase0.INPUTS[
        'locomo' if path.endswith('locomo10.json') else 'longmemeval'][1])
    monkeypatch.setattr(phase0, 'locomo_cases', lambda path: [])
    monkeypatch.setattr(phase0, 'longmemeval_cases', lambda *args: [])
    args = argparse.Namespace(data_dir=str(tmp_path), output=str(output), seed=19, baseline_commit='HEAD')
    with pytest.raises(FileExistsError):
        phase0.run(args)
    assert marker.read_text() == '{"historical":true}'


@pytest.mark.parametrize("fault", ["budget", "source", "selection", "fallback"])
def test_completed_run_rejects_packing_source_selection_and_model_faults(fault):
    summary = {"rows": [{"id": "q1", "tokens": 100}], "mode": "memory", "budget": 100,
               "effective_embedders": ["minilm"],
               "provenance": {"product_sha256": "product", "harness_sha256": "harness"}}
    phase0.validate_completed(summary, {"q1"}, "product", "harness", "minilm", "none")
    if fault == "budget":
        summary["rows"][0]["tokens"] = 101
    elif fault == "source":
        summary["provenance"]["product_sha256"] = "foreign"
    elif fault == "selection":
        summary["rows"].append(dict(summary["rows"][0]))
    else:
        summary["effective_embedders"] = []
    with pytest.raises(RuntimeError):
        phase0.validate_completed(summary, {"q1"}, "product", "harness", "minilm", "none")


@pytest.mark.parametrize("fault", ["empty", "budget", "mode", "partial-rerank", "dataset"])
def test_payload_refuses_missing_conditions_and_partial_model_failure(fault):
    import copy

    def target(budget, mode):
        return {"rows": [{"id": "q1", "tokens": 10, "ranked_turn_ids": ["ref"],
                          "effective_rerank": "cross-encoder"}],
                "mode": mode, "budget": budget, "effective_embedders": ["minilm"],
                "provenance": {"product_sha256": "product", "harness_sha256": "harness",
                               "dataset_sha256": "dataset"}}

    payload = {}
    for budget in (1000, 2000):
        primary = target(budget, "memory")
        primary["modes_detail"] = {mode: target(budget, mode)
                                   for mode in ("memory", "full-context", "no-memory")}
        payload[str(budget)] = primary
    phase0.validate_payload(payload, {"q1"}, "product", "harness", "minilm", "cross-encoder", "dataset")
    broken = copy.deepcopy(payload)
    if fault == "empty":
        broken = {}
    elif fault == "budget":
        del broken["2000"]
    elif fault == "mode":
        del broken["1000"]["modes_detail"]["no-memory"]
    elif fault == "dataset":
        broken["1000"]["provenance"]["dataset_sha256"] = "foreign"
    else:
        broken["1000"]["rows"][0]["effective_rerank"] = None
    with pytest.raises(RuntimeError):
        phase0.validate_payload(broken, {"q1"}, "product", "harness", "minilm", "cross-encoder", "dataset")
