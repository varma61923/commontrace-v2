"""Falsifiable original evidence contracts, without model/judge or gold leakage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks.memory_contracts import WORKFLOWS, Case, execute, execute_workflow, fixtures, main, score
from commontrace.conversation import Options, Store, recall


@pytest.mark.parametrize("case", fixtures(), ids=lambda case: case.name)
def test_persistent_production_memory_contract(case):
    outcome = execute(case)
    assert outcome.error is None, outcome
    assert outcome.passed, outcome


@pytest.mark.parametrize("name", WORKFLOWS)
def test_governed_production_memory_workflow(name):
    outcome = execute_workflow(name)
    assert outcome.error is None, outcome
    assert outcome.passed, outcome


def test_selected_id_without_rendered_evidence_does_not_pass(tmp_path):
    case = fixtures()[0]
    source = case.sources[1]
    with Store(str(tmp_path), case.space) as store:
        store.add(source.session, [{"id": source.ref, "speaker": source.speaker, "text": source.text}],
                  session_at=source.at)
        result = recall(store, case.question, now=case.now, options=Options(embedder=None, rerank=None))
        forged = replace(result, context="Unrelated text")
        metric = score(case, forged, tuple(store.turns(result.turns).values()), case.sources)
        assert metric.evidence_coverage == 0
        assert not metric.source_attribution
        assert not metric.passed


def test_correct_content_under_wrong_speaker_or_session_fails_attribution(tmp_path):
    case = fixtures()[0]
    source = case.sources[1]
    with Store(str(tmp_path), case.space) as store:
        store.add(source.session, [{"id": source.ref, "speaker": source.speaker, "text": source.text}],
                  session_at=source.at)
        result = recall(store, case.question, now=case.now, options=Options(embedder=None, rerank=None))
        turns = tuple(store.turns(result.turns).values())
        for forged in (replace(result, context=result.context.replace("Nia:", "Mira:")),
                       replace(result, context=result.context.replace("[earlier ·", "[another ·"))):
            metric = score(case, forged, turns, case.sources)
            assert metric.evidence_coverage == 1
            assert not metric.source_attribution
            assert not metric.passed


def test_forbidden_phrase_fails_even_without_a_forbidden_selected_id(tmp_path):
    case = fixtures()[0]
    source = case.sources[1]
    with Store(str(tmp_path), case.space) as store:
        store.add(source.session, [{"id": source.ref, "speaker": source.speaker, "text": source.text}],
                  session_at=source.at)
        result = recall(store, case.question, now=case.now, options=Options(embedder=None, rerank=None))
        forged = replace(result, context=result.context + "\nPorto")
        metric = score(case, forged, tuple(store.turns(result.turns).values()), case.sources)
        assert metric.forbidden_evidence == ("text:0",)
        assert not metric.passed


def test_unknown_observed_id_and_modified_source_fail_attribution(tmp_path):
    case = fixtures()[0]
    source = case.sources[1]
    with Store(str(tmp_path), case.space) as store:
        store.add(source.session, [{"id": source.ref, "speaker": source.speaker, "text": source.text}],
                  session_at=source.at)
        result = recall(store, case.question, now=case.now, options=Options(embedder=None, rerank=None))
        turns = tuple(store.turns(result.turns).values())
        altered = (replace(turns[0], text="I live in a different city."),)
        assert not score(case, result, altered, case.sources).source_attribution
        forged = replace(result, turns=result.turns + [999999])
        assert not score(case, forged, turns, case.sources).source_attribution


def test_state_correction_cannot_pass_with_superseded_live_fact(tmp_path):
    case = next(case for case in fixtures() if case.name == "current-update")
    with Store(str(tmp_path), case.space) as store:
        for source in case.sources:
            store.add(source.session, [{"id": source.ref, "speaker": source.speaker, "text": source.text}],
                      session_at=source.at)
        result = recall(store, case.question, now=case.now, options=Options(embedder=None, rerank=None))
        stale = score(case, result, tuple(store.turns(result.turns).values()), case.sources,
                      live_statements=(case.sources[1].text,))
        assert stale.evidence_coverage == 1
        assert not stale.state_matches
        assert not stale.passed


def test_reverse_event_order_fails_even_with_complete_evidence(tmp_path):
    case = next(case for case in fixtures() if case.name == "event-ordering")
    with Store(str(tmp_path), case.space) as store:
        for source in case.sources:
            store.add(source.session, [{"id": source.ref, "speaker": source.speaker, "text": source.text}],
                      session_at=source.at)
        result = recall(store, case.question, now=case.now, options=Options(embedder=None, rerank=None))
        blocks = result.context.split("\n\n")
        forged = replace(result, context="\n\n".join(reversed(blocks)))
        stale = score(case, forged, tuple(store.turns(result.turns).values()), case.sources)
        assert stale.evidence_coverage == 1
        assert stale.source_attribution
        assert stale.ordering_matches is False
        assert not stale.passed


def test_errors_remain_failures_and_do_not_expose_input_values():
    case = Case("invalid-time", "temporal-reasoning", "unused", (), now="sensitive-invalid-value")
    result = execute(case)
    assert not result.passed
    assert result.metrics is None
    assert result.error == "ConversationError"
    assert "sensitive-invalid-value" not in repr(result)


def test_empty_gold_is_not_reported_as_perfect_evidence_recall():
    outcome = execute(next(case for case in fixtures() if case.name == "unknown-subject"))
    assert outcome.passed and outcome.metrics is not None
    assert outcome.metrics.evidence_coverage is None
    assert outcome.metrics.abstention_matches is True


def test_gold_labels_never_reach_the_retriever(monkeypatch):
    import benchmarks.memory_contracts as contracts

    actual_recall = contracts.recall
    seen = []

    def observed(store, question, **kwargs):
        seen.append((question, kwargs))
        return actual_recall(store, question, **kwargs)

    monkeypatch.setattr(contracts, "recall", observed)
    case = fixtures()[0]
    assert execute(case).passed
    assert len(seen) == 2
    assert all(question == case.question and set(kwargs) == {"now", "options"} for question, kwargs in seen)
    assert all(kwargs["options"].embedder is None and kwargs["options"].rerank is None for _, kwargs in seen)
    assert all(not hasattr(kwargs["options"], "required") for _, kwargs in seen)


def test_cli_stdout_has_reproducible_manifest_and_no_answer_accuracy_claim(capsys):
    args = ["--case", "historical-cutoff", "--case", "only-poison-abstains"]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["fixture_sha256"] == second["fixture_sha256"]
    assert first["total"] == first["passed"] == 2
    assert first["failed"] == 0
    assert first["answer_accuracy_measured"] is False
    assert all("context" not in row for row in first["cases"])


def test_cli_counts_case_error_in_denominator(monkeypatch, capsys):
    import benchmarks.memory_contracts as contracts

    invalid = Case("broken", "temporal-reasoning", "unused", (), now="invalid")
    monkeypatch.setattr(contracts, "fixtures", lambda: (invalid,))
    monkeypatch.setattr(contracts, "WORKFLOWS", ())
    assert contracts.main([]) == 1
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["total"] == stdout["failed"] == 1
    assert stdout["passed"] == 0


def test_executable_contract_gate_runs_offline_without_output_artifacts(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    process = subprocess.run([sys.executable, "-m", "benchmarks.memory_contracts", "--case", "historical-cutoff"],
                             capture_output=True, text=True, check=True, cwd=tmp_path, env=environment)
    report = json.loads(process.stdout)
    assert report["passed"] == 1 and report["failed"] == 0
    assert process.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_cli_unknown_case_is_an_error():
    with pytest.raises(SystemExit) as exc:
        main(["--case", "undefined-case"])
    assert exc.value.code == 2
