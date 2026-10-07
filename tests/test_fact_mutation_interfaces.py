"""Canonical fact mutations must preserve exact warm/cold public contracts.

The legacy overlap oracle scans authoritative rows. BM25 and recalled evidence
are compared with a forced cold rebuild, without asserting host timing ratios
or relying on private incremental-index counters.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from benchmarks.fact_search import original_search
from commontrace import fact_index, hierarchical, recall
from commontrace.fact_evidence import bind_evidence

QUERY = "Aster gateway timeout"
SCORERS = ("overlap-v1", "bm25-v1")


@dataclass(frozen=True)
class Corpus:
    root: str
    target: hierarchical.AtomicFact
    source: hierarchical.AtomicFact
    bound: hierarchical.AtomicFact
    private: hierarchical.AtomicFact


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    root = str(tmp_path)
    target, _ = hierarchical.add_fact(root, "Aster gateway timeout is five seconds.", category="constraint",
                                      scopes=["alpha"], valid_from="2020-01-01", source_trace_id="instrument-1",
                                      stability="stable")
    source, _ = hierarchical.add_fact(root, "An instrument measured the Aster gateway timeout at five seconds.",
                                      scopes=["alpha"], valid_from="2020-01-01")
    bound, _ = hierarchical.add_fact(root, "Verified Aster gateway timeout is five seconds.", scopes=["alpha"],
                                     valid_from="2020-01-01", evidence=[bind_evidence(root, "fact", source.id)])
    private, _ = hierarchical.add_fact(root, "BETA_SECRET Aster gateway timeout is one second.", scopes=["beta"],
                                       valid_from="2020-01-01", confidence=1)
    hierarchical.add_fact(root, "Aster gateway retry policy applies a nine second timeout.", valid_from="2020-01-01")
    return Corpus(root, target, source, bound, private)


def rows(root: str, scorer: str, **options: Any) -> list[tuple[dict[str, Any], float]]:
    return [(fact.to_dict(), score) for fact, score in hierarchical.search_facts(root, QUERY, scorer=scorer,
                                                                               limit=20, **options)]


def packed(root: str, scorer: str, as_of: str | None = None) -> dict[str, Any]:
    result = recall.recall(root, QUERY, channels=("facts",), scope="alpha", as_of=as_of,
                           fact_scorer=scorer, per_channel=20, budget=4000, evidence_budget=512).to_dict()
    assert not result["errors"]
    assert "BETA_SECRET" not in json.dumps(result)
    return result


def assert_equivalent(corpus: Corpus, scorer: str) -> None:
    # The first reads retain the process's pre-mutation hot state. Cold reads
    # then reconstruct the same generation exclusively from canonical bytes.
    views = ({"scope": "alpha"}, {"scope": "alpha", "category": "constraint", "stability": "stable"},
             {"scope": "alpha", "as_of": "2022-01-01"})
    warm = [rows(corpus.root, scorer, **view) for view in views]
    warm_context = [packed(corpus.root, scorer, date) for date in (None, "2022-01-01")]
    for view, actual in zip(views, warm):
        assert all(fact["id"] != corpus.private.id for fact, _score in actual)
        if scorer == "overlap-v1":
            expected = [(fact.to_dict(), score) for fact, score in original_search(corpus.root, QUERY, limit=20, **view)]
            assert actual == expected
    fact_index.clear_cache()
    assert warm == [rows(corpus.root, scorer, **view) for view in views]
    assert warm_context == [packed(corpus.root, scorer, date) for date in (None, "2022-01-01")]


def cli(corpus: Corpus, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "commontrace.cli", "fact", *arguments, "--dest", corpus.root],
                          capture_output=True, text=True, check=False, timeout=30)


@pytest.mark.parametrize("scorer", SCORERS)
def test_canonical_text_metadata_and_reinforcement_deltas_match_fresh_public_results(corpus: Corpus, scorer: str) -> None:
    assert_equivalent(corpus, scorer)
    added, _ = hierarchical.add_fact(corpus.root, "Aster gateway timeout calibration needs a second instrument.",
                                     scopes=["alpha"], valid_from="2020-01-01")
    assert_equivalent(corpus, scorer)
    hierarchical.update_fact(corpus.root, added.id, statement="Aster gateway timeout calibration now uses three instruments.")
    assert_equivalent(corpus, scorer)
    hierarchical.update_fact(corpus.root, added.id, category="constraint", confidence=1, stability="stable")
    assert_equivalent(corpus, scorer)
    hierarchical.update_fact(corpus.root, added.id, expires_at="2021-01-01")
    assert_equivalent(corpus, scorer)
    assert added.id not in {fact["id"] for fact, _score in rows(corpus.root, scorer, scope="alpha")}
    hierarchical.update_fact(corpus.root, added.id, expires_at=None)
    assert_equivalent(corpus, scorer)
    hierarchical.update_fact(corpus.root, added.id, valid_until="2021-01-01")
    assert_equivalent(corpus, scorer)
    hierarchical.update_fact(corpus.root, added.id, valid_until="2035-01-01")
    assert_equivalent(corpus, scorer)
    before = hierarchical.load_facts(corpus.root)[corpus.target.id].to_dict()
    replay, _ = hierarchical.add_fact(corpus.root, corpus.target.statement, scopes=["alpha"], source_trace_id="instrument-1")
    assert replay.to_dict() == before
    assert_equivalent(corpus, scorer)
    reinforced, _ = hierarchical.add_fact(corpus.root, corpus.target.statement, scopes=["alpha"], source_trace_id="instrument-2")
    assert reinforced.confirmations == before["confirmations"] + 1
    assert reinforced.confidence > before["confidence"]
    assert_equivalent(corpus, scorer)


@pytest.mark.parametrize("scorer", SCORERS)
def test_private_only_delta_cannot_change_another_tenants_scores_or_context(corpus: Corpus, scorer: str) -> None:
    before = rows(corpus.root, scorer, scope="alpha")
    context = packed(corpus.root, scorer)
    hierarchical.update_fact(corpus.root, corpus.private.id,
                             statement="BETA_SECRET " + "Aster gateway timeout " * 64,
                             confidence=0.01, category="constraint", stability="dynamic")
    assert rows(corpus.root, scorer, scope="alpha") == before
    assert packed(corpus.root, scorer) == context
    assert_equivalent(corpus, scorer)


@pytest.mark.parametrize("scorer", SCORERS)
def test_canonical_reinforcement_scope_expansion_updates_a_warm_filtered_corpus(corpus: Corpus, scorer: str) -> None:
    before = rows(corpus.root, scorer, scope="gamma")
    assert corpus.target.id not in {fact["id"] for fact, _score in before}
    reinforced, _ = hierarchical.add_fact(corpus.root, corpus.target.statement, scopes=["alpha", "gamma"],
                                          source_trace_id="instrument-3")
    assert set(reinforced.scopes) == {"alpha", "gamma"}
    warm = rows(corpus.root, scorer, scope="gamma")
    assert corpus.target.id in {fact["id"] for fact, _score in warm}
    assert corpus.private.id not in {fact["id"] for fact, _score in warm}
    fact_index.clear_cache()
    assert rows(corpus.root, scorer, scope="gamma") == warm
    assert_equivalent(corpus, scorer)


@pytest.mark.parametrize("scorer", SCORERS)
def test_cli_external_lifecycle_preserves_warm_readers_and_historical_windows(corpus: Corpus, scorer: str) -> None:
    assert_equivalent(corpus, scorer)
    assert cli(corpus, "forget", corpus.target.id).returncode == 0
    assert_equivalent(corpus, scorer)
    assert corpus.target.id not in {fact["id"] for fact, _score in rows(corpus.root, scorer, scope="alpha")}
    assert cli(corpus, "forget", corpus.target.id, "--undo").returncode == 0
    assert_equivalent(corpus, scorer)
    replacement, _ = hierarchical.add_fact(corpus.root, "Aster gateway timeout is nine seconds.", scopes=["alpha"],
                                           valid_from="2024-01-01")
    assert_equivalent(corpus, scorer)
    assert cli(corpus, "supersede", corpus.target.id, replacement.id).returncode == 0
    assert_equivalent(corpus, scorer)
    assert corpus.target.id not in {fact["id"] for fact, _score in rows(corpus.root, scorer, scope="alpha")}
    historical = rows(corpus.root, scorer, scope="alpha", as_of="2022-01-01")
    assert corpus.target.id in {fact["id"] for fact, _score in historical}
    assert replacement.id not in {fact["id"] for fact, _score in historical}
    assert cli(corpus, "delete", replacement.id).returncode == 0
    assert_equivalent(corpus, scorer)
    assert replacement.id not in {fact["id"] for fact, _score in rows(corpus.root, scorer, scope="alpha")}


@pytest.mark.parametrize("scorer", SCORERS)
def test_source_only_deltas_withdraw_unchanged_bound_claims_and_quotes(corpus: Corpus, scorer: str) -> None:
    assert_equivalent(corpus, scorer)
    original = hierarchical.load_facts(corpus.root)[corpus.bound.id].to_dict()
    hierarchical.forget_fact(corpus.root, corpus.source.id)
    assert_equivalent(corpus, scorer)
    for date in (None, "2022-01-01"):
        result = packed(corpus.root, scorer, date)
        assert "fact:" + corpus.bound.id not in {item["id"] for item in result["items"]}
        assert corpus.source.statement not in result["context"]
    hierarchical.forget_fact(corpus.root, corpus.source.id, undo=True)
    assert_equivalent(corpus, scorer)
    assert "fact:" + corpus.bound.id in {item["id"] for item in packed(corpus.root, scorer)["items"]}
    hierarchical.update_fact(corpus.root, corpus.source.id, statement="The Aster gateway instrument now measures nine seconds.")
    assert_equivalent(corpus, scorer)
    assert hierarchical.load_facts(corpus.root)[corpus.bound.id].to_dict() == original
    for date in (None, "2022-01-01"):
        assert "fact:" + corpus.bound.id not in {item["id"] for item in packed(corpus.root, scorer, date)["items"]}


@pytest.mark.parametrize("scorer", SCORERS)
def test_failed_batch_and_cross_tenant_cli_mutations_do_not_publish_partial_state(corpus: Corpus, scorer: str) -> None:
    assert_equivalent(corpus, scorer)
    path = Path(corpus.root) / "memory" / "facts" / "facts.jsonl"
    original = path.read_bytes()
    before = packed(corpus.root, scorer)
    with pytest.raises(ValueError):
        hierarchical.add_facts(corpus.root, [{"statement": "Aster gateway timeout partial phantom row", "scopes": ["alpha"]},
                                            {"statement": "Aster gateway timeout invalid evidence policy", "min_support": 0}])
    assert path.read_bytes() == original
    assert packed(corpus.root, scorer) == before
    failed = cli(corpus, "resolve", corpus.target.id, corpus.private.id)
    assert failed.returncode != 0 and "Traceback" not in failed.stderr
    assert path.read_bytes() == original
    assert_equivalent(corpus, scorer)


def tool(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    response = asyncio.run(server.call_tool(name, arguments))
    structured = getattr(response, "structured_content", None)
    return structured.get("result", structured) if structured else json.loads(response.content[0].text)


@pytest.mark.parametrize("scorer", SCORERS)
def test_persistent_public_mcp_reads_native_and_external_mutations_without_stale_claims(corpus: Corpus, scorer: str) -> None:
    pytest.importorskip("mcp")
    from commontrace import mcp_server

    server = mcp_server.build_server(corpus.root)
    def query() -> dict[str, Any]:
        result = tool(server, "query_facts", query=QUERY, scope="alpha", scorer=scorer, limit=20)
        assert result["ok"] and "BETA_SECRET" not in json.dumps(result)
        return result
    query()
    inserted = tool(server, "record_fact", statement="Aster gateway timeout delivery needs corroborated measurement.",
                    scope="alpha", evidence=[{"kind": "fact", "source_id": corpus.source.id}])
    assert inserted["ok"] and inserted["action"] == "ADD"
    identity = inserted["fact"]["id"]
    warm = query()
    assert identity in {row["fact"]["id"] for row in warm["facts"]}
    fact_index.clear_cache()
    assert query() == warm
    assert cli(corpus, "delete", corpus.source.id).returncode == 0
    warm = query()
    assert not {identity, corpus.bound.id} & {row["fact"]["id"] for row in warm["facts"]}
    recalled = tool(server, "memory_recall", question=QUERY, channels=["facts"], scope="alpha",
                    fact_scorer=scorer, evidence_budget=512)
    assert recalled["ok"] and not recalled["errors"]
    assert not {"fact:" + identity, "fact:" + corpus.bound.id} & {item["id"] for item in recalled["items"]}
    assert corpus.source.statement not in recalled["context"]
    fact_index.clear_cache()
    assert query() == warm
