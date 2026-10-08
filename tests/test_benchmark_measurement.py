"""Real-store evaluation fidelity and source/cache provenance regressions."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path

import pytest

from benchmarks import conversation_bench as bench
from benchmarks.compare import compare_runs
from benchmarks.measurement import (
    adapter_digest,
    canonical_digest,
    dataset_digest,
    gold_sessions,
    gold_turns,
    question_digest,
    ranking_fields,
    shared_source_clusters,
)
from commontrace import llm
from commontrace.conversation import Store


def _args(tmp_path: Path, **changes: object) -> argparse.Namespace:
    source = tmp_path / "dataset.json"
    if not source.exists():
        source.write_text(json.dumps([
            {"sample_id": f"c{i}", "conversation": {
                "session_1_date_time": "2024-01-01", "session_1": [
                    {"dia_id": "D1:1", "speaker": "user", "text": "The orchid launch completed on Tuesday."},
                    {"dia_id": "D1:2", "speaker": "assistant", "text": "The launch is complete."},
                ]}, "qa": [{"question": "When did the orchid launch complete?", "answer": "Tuesday",
                            "category": 2, "evidence": ["D1:1", "missing"]}]}
            for i in range(3)
        ]), encoding="utf-8")
    values: dict[str, object] = {
        "dataset": "locomo", "data": str(source), "root": str(tmp_path / "store"), "budget": "1500,4000",
        "embedder": "none", "rerank": "none", "neighbours": 1, "profile_facts": 4, "rerank_blend": 1.,
        "limit": 0, "seed": 0, "answer": False, "personas": "", "bootstrap": False,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def test_unresolved_labels_remain_misses_in_real_store_and_both_budgets(tmp_path: Path) -> None:
    args = _args(tmp_path, bootstrap=True)
    result = bench.run(args)
    for budget in (1500, 4000):
        summary = result[budget]
        assert summary["overall"]["n"] == 3
        assert summary["overall"]["evidence"] == .5
        assert summary["overall"]["complete"] == 0.
        assert summary["overall"]["unresolved_gold_turn_references"] == 3
        assert summary["bootstrap_95ci"]["sample_unit"] == "conversation"
        assert summary["bootstrap_95ci"]["overall"]["evidence"]["ci_lower"] == .5
        for row in summary["rows"]:
            assert row["gold_turn_ids"] == ["D1:1", "missing"]
            assert row["unresolved_gold_turn_ids"] == ["missing"]
            assert row["turn_recall_at_5"] == .5
            assert row["recall_latency_s"] >= 0
        # Pairing is independent of a warmed cache and keeps exact gold identities.
        second = bench.run(args)[budget]
        assert compare_runs(summary, second)["overall"]["evidence"]["delta"] == 0.


def test_every_budget_runs_public_recall_with_question_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    calls = []
    original = bench.recall

    def observed(store, question, *, now=None, options=None):
        calls.append((question, now, options.budget))
        return original(store, question, now=now, options=options)

    monkeypatch.setattr(bench, "recall", observed)
    bench.run(args)
    assert [budget for _q, _now, budget in calls] == [1500, 4000] * 3


def test_true_full_history_and_budgeted_reference_have_distinct_contracts(tmp_path: Path) -> None:
    args = _args(tmp_path, budget="20", modes="memory,full-context,budgeted-history,no-memory")
    result = bench.run(args)[20]["modes_detail"]
    assert result["full-context"]["context_reference"] == "entire-raw-history"
    assert result["full-context"]["overall"]["tokens"] > 20
    assert result["budgeted-history"]["overall"]["tokens"] <= 20
    assert result["no-memory"]["overall"]["tokens"] == 0
    for mode in ("full-context", "budgeted-history", "no-memory"):
        compared = compare_runs(result[mode], result[mode])
        assert compared["overall"]["turn_recall_at_5"]["n_questions"] == 0
        assert result[mode]["overall"]["recall_latency"] is None


def test_same_size_same_mtime_source_edit_invalidates_parsed_cache(tmp_path: Path) -> None:
    args = _args(tmp_path)
    cases, first = bench.prepare_chunk_set(args)
    old = os.stat(args.data)
    data = Path(args.data).read_text(encoding="utf-8")
    Path(args.data).write_text(data.replace("Tuesday", "Sundays"), encoding="utf-8")
    os.utime(args.data, ns=(old.st_atime_ns, old.st_mtime_ns))
    updated, info = bench.prepare_chunk_set(args)
    assert not info["reused"]
    assert cases != updated
    assert info["fingerprint"]["dataset_sha256"] != first["fingerprint"]["dataset_sha256"]


def test_adapter_changes_invalidate_named_parsed_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, chunk_set="stable")
    bench.prepare_chunk_set(args)
    monkeypatch.setattr(bench, "adapter_digest", lambda _p: "a" * 64)
    _cases, info = bench.prepare_chunk_set(args)
    assert not info["reused"]
    assert info["fingerprint"]["adapter_sha256"] == "a" * 64


def test_corrupt_parse_cache_is_rebuilt_from_authoritative_input(tmp_path: Path) -> None:
    args = _args(tmp_path)
    cases, info = bench.prepare_chunk_set(args)
    chunks = Path(info["path"]) / "chunks.jsonl"
    chunks.write_text("{}\n", encoding="utf-8")
    reparsed, fresh = bench.prepare_chunk_set(args)
    assert not fresh["reused"]
    assert reparsed == cases


def test_same_size_edited_corpus_cannot_reuse_source_store(tmp_path: Path) -> None:
    args = _args(tmp_path)
    bench.run(args)
    data = Path(args.data).read_text(encoding="utf-8")
    Path(args.data).write_text(data.replace("Tuesday", "Sundays"), encoding="utf-8")
    with pytest.raises(ValueError, match="different source corpus"):
        bench.run(args)


@pytest.mark.parametrize("sql", [
    "UPDATE sessions SET started_at='2025-01-01'",
    "UPDATE turns SET speaker='another speaker'",
    "UPDATE turns SET text='changed source'",
])
def test_canonical_edits_fail_even_without_unit_journal_change(tmp_path: Path, sql: str) -> None:
    args = _args(tmp_path)
    bench.run(args)
    with Store(args.root, "c0") as store:
        store.db.execute(sql)
        store.db.commit()
    with pytest.raises(ValueError, match="source store was modified"):
        bench.run(args)


def test_split_passage_rankings_collapse_to_distinct_raw_turns_and_sessions(tmp_path: Path) -> None:
    with Store(str(tmp_path), "raw") as store:
        store.add("s1", [{"id": "raw1", "speaker": "user", "text": "Orchid launch complete."},
                               {"id": "raw2", "speaker": "user", "text": "Followed by daisy launch."}])
        ids = [row[0] for row in store.db.execute("SELECT id FROM turns ORDER BY id")]
        # Repeated candidate IDs count once at the raw-turn and session levels.
        rows = ranking_fields([ids[0], ids[0], ids[1]], store.turns(ids), ["raw1"], ["s1"])
        assert rows["ranked_turn_ids"] == ["raw1", "raw2"]
        assert rows["ranked_session_ids"] == ["s1"]
        with pytest.raises(ValueError, match="absent"):
            ranking_fields([99999], {}, ["raw1"], ["s1"])


def test_session_annotations_expand_fragments_and_unknown_gold_stays_missing() -> None:
    sessions = [("day", None, [{"id": "source#0"}, {"id": "source#1"}])]
    assert gold_turns(["source", "source", "unknown"], sessions) == {"source#0", "source#1", "unknown"}
    resolved = gold_sessions(["source", "unknown", "unknown"], [], sessions)
    assert "day" in resolved and len(resolved) == 2


def test_shared_answer_sessions_cluster_across_cases_but_filler_does_not() -> None:
    cases = [("a", [], None, [{"sessions": {"s1"}}]),
             ("b", [], None, [{"sessions": {"s1", "s2"}}]),
             ("c", [], None, [{"sessions": {"s2"}}]),
             ("d", [], None, [{"sessions": set()}])]
    assert shared_source_clusters(cases) == {"a": "a", "b": "a", "c": "a", "d": "d"}
    assert shared_source_clusters(list(reversed(cases))) == shared_source_clusters(cases)


def test_adapter_hash_changes_only_when_its_contract_changes(tmp_path: Path) -> None:
    source = tmp_path / "adapters.py"
    source.write_text("def locomo_cases(path):\n    return 1\ndef unrelated():\n    return 1\n")
    original = adapter_digest(str(source))
    source.write_text("def locomo_cases(path):\n    return 1\ndef unrelated():\n    return 2\n")
    assert adapter_digest(str(source)) == original
    source.write_text("def locomo_cases(path):\n    return 2\ndef unrelated():\n    return 2\n")
    assert adapter_digest(str(source)) != original


def test_digests_pin_content_and_source_metadata_without_question_leakage(tmp_path: Path) -> None:
    source = tmp_path / "a.json"
    source.write_text("abc")
    before = dataset_digest(str(source))
    source.write_text("abd")
    assert dataset_digest(str(source)) != before
    assert question_digest({"evidence": {"b", "a"}}) == question_digest({"evidence": {"a", "b"}})
    with Store(str(tmp_path), "raw") as store:
        store.add("s", [{"id": "r", "speaker": "user", "text": "orchid launch"}])
        old = canonical_digest(store)
        store.db.execute("UPDATE sessions SET started_at='2024-01-01'")
        assert canonical_digest(store) != old


@pytest.mark.parametrize("changes", [{"budget": "0"}, {"budget": "1500,1500"},
                                    {"modes": "memory,memory"}, {"limit": -1}, {"seed": -1}])
def test_invalid_configuration_refused_before_source_ingestion(tmp_path: Path, changes: dict) -> None:
    args = _args(tmp_path, **changes)
    with pytest.raises(ValueError):
        bench.run(args)
    assert not Path(args.root).exists()


def test_longmemeval_limit_is_exact_even_for_unequal_categories(tmp_path: Path) -> None:
    data = []
    for category, count in (("single-session-user", 2), ("multi-session", 7), ("knowledge-update", 1)):
        for n in range(count):
            data.append({"question_type": category, "question_id": f"{category}{n}", "question": "What?",
                         "answer": "answer", "question_date": "2024-01-01", "haystack_session_ids": ["s"],
                         "haystack_dates": ["2024-01-01"], "haystack_sessions": [[
                             {"role": "user", "content": "orchid", "has_answer": True}]], "answer_session_ids": ["s"]})
    source = tmp_path / "lme.json"
    source.write_text(json.dumps(data))
    assert len(list(bench.longmemeval_cases(str(source), 10, 0))) == 10
    assert len(list(bench.longmemeval_cases(str(source), 9, 0))) == 9
    assert len(list(bench.longmemeval_cases(str(source), 1, 0))) == 1
    assert list(bench.longmemeval_cases(str(source), 9, 0)) == list(bench.longmemeval_cases(str(source), 9, 0))


def test_comparison_cli_cannot_silently_ignore_legacy_rows(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    args = _args(tmp_path)
    result = bench.run(args)
    legacy = copy.deepcopy(result)
    for summary in legacy.values():
        summary.pop("provenance")
    baseline = tmp_path / "legacy.json"
    baseline.write_text(json.dumps(legacy))
    rc = bench.main(["--dataset", "locomo", "--data", args.data, "--root", args.root,
                     "--budget", "1500,4000", "--embedder", "none", "--rerank", "none",
                     "--compare", str(baseline)])
    assert rc == 2
    assert "Comparison error" in capsys.readouterr().err


def test_judged_provenance_binds_endpoint_without_exposing_credentials(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path, answer=True, answer_model="fixture-reader", judge_model="fixture-judge")
    endpoint = "https://user:private-password@service.invalid/v1?token=private-token"

    def config(model):
        return llm.Config(provider="openai-compatible", model=model, api_key="private-api-key",
                          base_url=endpoint, region="private-region", project="private-project")

    def grade(**kwargs):
        return {"correct": True, "score": 1.0}

    monkeypatch.setattr(bench, "_get_llm_config", config)
    monkeypatch.setattr(bench, "grade_answer", grade)
    first = bench.run(args)[1500]
    encoded = json.dumps(first)
    for secret in (endpoint, "private-password", "private-token", "private-api-key", "private-project", "private-region"):
        assert secret not in encoded
    binding = first["provenance"]["evaluation"]["reader"]
    assert len(binding["configuration_sha256"]) == 64
    assert binding["provider"] == "openai-compatible"
    endpoint = "https://other-service.invalid/v1"
    second = bench.run(args)[1500]
    assert second["provenance"]["evaluation"]["reader"] != binding
    with pytest.raises(ValueError, match="provenance"):
        compare_runs(first, second)
