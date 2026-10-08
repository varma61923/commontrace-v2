"""Report claim boundaries, source consistency and untrusted-label escaping."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from benchmarks.phase0 import INPUTS
from benchmarks.phase0_report import reviewed_experiment, write_report


def dump(path, value):
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def fixture(path, *, development=None, verified=False):
    path.mkdir()
    strict = development is not None
    identity = "q2" if strict else "q1"
    contract = {"unit": "selected-tokenizer-context-text", "counter": {"name": "tiktoken:cl100k_base"},
                "max_attempts": 16, "full_context": "uncapped"}
    manifest = {"scope": ("unjudged excluded-question strict-budget confirmation" if strict
                           else "unjudged development retrieval evidence"),
                "budgets_text_tokens" if strict else "budgets_estimated": [1000, 2000],
                "exact_text_counter": "tiktoken:cl100k_base", "attempts": [],
                "selection": {"locomo": {"input_sha256": INPUTS["locomo"][1], "question_ids": [identity]}}}
    if strict:
        manifest.update(context_budget=contract, development_manifest_sha256=development["source_manifest_sha256"])
        manifest["selection"]["locomo"].update(excluded_ids=["q1"], question_exclusions_sha256="bound")
    report = {"answer_accuracy": None, "conditions": {},
              "comparisons": {"lexical-vs-dense": {}, "lexical-vs-graphiti-episodic": {}}}
    for label, profile in (('<img src=x onerror=alert(1)>', {"profile": "commontrace"}),
                           ("graphiti", {"profile": "graphiti-episodic"})):
        if verified and label == "graphiti":
            profile["group_namespace"] = "sha256-canonical-space-utf8"
        budgets, sources = {}, {}
        for budget in ("1000", "2000"):
            summary = {"overall": {"n": 1, "accuracy": None, "evidence": .5,
                       "context_text_tokens": 900, "exact_budget_exceedances": 0},
                       "by_type": {}, "bootstrap_95ci": {}, "profile": profile}
            evaluation = {"answer_enabled": False, "context_text_tokenizer": "tiktoken:cl100k_base"}
            if strict:
                evaluation["context_budget"] = contract
            source = {"overall": summary["overall"], "by_type": {}, "bootstrap_95ci": {},
                      "retrieval_profile": profile, "dataset": "locomo",
                      "rows": [{"id": identity, "context_text_tokens": 900}],
                      "provenance": {"evaluation": evaluation, "sampling": {"question_exclusions_sha256": "bound"}}}
            budgets[budget], sources[budget] = summary, source
        report["conditions"][label] = budgets
        sha = dump(path / (label + ".json"), sources)
        manifest["attempts"].append({"label": label, "status": "completed", "result_sha256": sha})
    dump(path / "scorecard.json", report)
    dump(path / "manifest.json", manifest)
    return report


def test_unverified_graphiti_excluded_without_editing_original_evidence(tmp_path):
    path = tmp_path / "raw"
    fixture(path)
    before = (path / "scorecard.json").read_bytes()
    report = reviewed_experiment(path)
    assert "graphiti" not in report["conditions"]
    assert "graphiti" in report["excluded_artifacts"]
    assert "lexical-vs-graphiti-episodic" not in report["comparisons"]
    assert "lexical-vs-dense" in report["comparisons"]
    assert len(report["source_scorecard_sha256"]) == 64
    assert (path / "scorecard.json").read_bytes() == before


def test_verified_graphiti_namespace_retained(tmp_path):
    path = tmp_path / "raw"
    fixture(path, verified=True)
    assert "graphiti" in reviewed_experiment(path)["conditions"]


def test_reader_accuracy_cannot_be_mislabeled_as_unjudged(tmp_path):
    path = tmp_path / "raw"
    raw = fixture(path)
    raw["conditions"]["graphiti"]["1000"]["overall"]["accuracy"] = .9
    dump(path / "scorecard.json", raw)
    with pytest.raises(ValueError, match="reader accuracy"):
        reviewed_experiment(path)


def test_report_escapes_source_labels_and_refuses_existing_output(tmp_path):
    dev, confirm, out = (tmp_path / name for name in ("dev", "confirm", "site"))
    fixture(dev)
    fixture(confirm, development=reviewed_experiment(dev), verified=True)
    write_report(dev, confirm, out)
    markup = (out / "index.html").read_text()
    assert '<img src=x' not in markup
    assert '&lt;img src=x' in markup
    assert 'Reader accuracy' in markup and 'Not run' in markup
    assert 'different questions and budget contracts' in markup
    before = (out / "reviewed-scorecard.json").read_bytes()
    with pytest.raises(FileExistsError):
        write_report(dev, confirm, out)
    assert (out / "reviewed-scorecard.json").read_bytes() == before


def test_missing_accuracy_does_not_hide_enabled_reader_calls(tmp_path):
    path = tmp_path / "raw"
    fixture(path)
    raw = json.loads((path / "graphiti.json").read_text())
    raw["1000"]["provenance"]["evaluation"]["answer_enabled"] = True
    sha = dump(path / "graphiti.json", raw)
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["attempts"][1]["result_sha256"] = sha
    dump(path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="disabled reader"):
        reviewed_experiment(path)


def test_condition_artifact_cannot_escape_report_directory(tmp_path):
    path = tmp_path / "raw"
    report = fixture(path)
    report["conditions"] = {"../elsewhere": report["conditions"]["graphiti"]}
    dump(path / "scorecard.json", report)
    with pytest.raises(ValueError, match="outside"):
        reviewed_experiment(path)


@pytest.mark.parametrize("fault", ["namespace", "metric", "manifest-hash", "tokenizer"])
def test_edited_summary_source_or_contract_cannot_misstate_results(tmp_path, fault):
    path = tmp_path / "raw"
    report = fixture(path)
    if fault in ("namespace", "metric"):
        key = "profile" if fault == "namespace" else "overall"
        field = "group_namespace" if fault == "namespace" else "evidence"
        report["conditions"]["graphiti"]["1000"][key][field] = "sha256-canonical-space-utf8" if fault == "namespace" else .99
        dump(path / "scorecard.json", report)
    elif fault == "manifest-hash":
        (path / "graphiti.json").write_text('{}')
    else:
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["exact_text_counter"] = "other"
        dump(path / "manifest.json", manifest)
    with pytest.raises(ValueError):
        reviewed_experiment(path)


def test_development_directory_cannot_be_labeled_as_strict_confirmation(tmp_path):
    dev = tmp_path / "dev"
    fixture(dev)
    with pytest.raises(ValueError, match="experiment contract"):
        write_report(dev, dev, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_confirmation_must_be_disjoint_and_bound_to_consumed_development(tmp_path):
    dev, confirm = tmp_path / "dev", tmp_path / "confirm"
    fixture(dev)
    original = reviewed_experiment(dev)
    fixture(confirm, development=original)
    reviewed_experiment(confirm, development=original)
    foreign = copy.deepcopy(original)
    foreign["source_manifest_sha256"] = "foreign"
    with pytest.raises(ValueError, match="development"):
        reviewed_experiment(confirm, development=foreign)
    manifest = json.loads((confirm / "manifest.json").read_text())
    manifest["selection"]["locomo"]["question_ids"] = ["q1"]
    dump(confirm / "manifest.json", manifest)
    with pytest.raises(ValueError, match="overlaps"):
        reviewed_experiment(confirm, development=original)
