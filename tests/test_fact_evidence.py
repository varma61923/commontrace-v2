"""Persisted source currency, scope and temporal contracts without model judgments."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from commontrace import frontmatter, hierarchical, lesson_admission, observations, recall
from commontrace.fact_evidence import EvidenceError, EvidenceResolver, FactEvidence, bind_evidence, claim_revision


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path)


def source(root: str, statement: str = "The Lumen database timeout is 5 seconds", **kwargs):
    return hierarchical.add_fact(root, statement, valid_from="2026-01-01T00:00:00Z", **kwargs)[0]


def assess(root: str, fact_id: str, **kwargs):
    return EvidenceResolver(root, hierarchical.load_facts(root), **kwargs).assess(fact_id)


def derived(root: str, sources, *, statement="The Lumen database timeout is 5 seconds", **kwargs):
    return hierarchical.add_fact(root, statement, evidence=[bind_evidence(root, "fact", s.id) for s in sources],
                                 valid_from="2026-01-01T00:00:00Z", **kwargs)[0]


def cli(root: str, *arguments: str):
    return subprocess.run([sys.executable, "-m", "commontrace.cli", *arguments, "--dest", root],
                          text=True, capture_output=True, check=False)


def test_receipt_replay_is_idempotent_and_second_source_admits(root):
    first = source(root, "The instrument logged a database timeout of 5 seconds")
    second = source(root, "The operator recorded the database timeout as 5 seconds")
    receipt = bind_evidence(root, "fact", first.id)
    target, _ = hierarchical.add_fact(root, "The Lumen timeout is 5 seconds", evidence=[receipt],
                                      min_support=2, confidence=0.61)
    before = target.to_dict()
    assert assess(root, target.id).status == "insufficient"
    assert target.id not in {f.id for f in hierarchical.list_facts(root)}
    replay, _ = hierarchical.add_fact(root, target.statement,
                                      evidence=[bind_evidence(root, "fact", first.id)], min_support=2)
    assert replay.to_dict() == before
    admitted, _ = hierarchical.add_fact(root, target.statement,
                                        evidence=[bind_evidence(root, "fact", second.id)], min_support=2)
    assert assess(root, target.id).supports == admitted.confirmations == 2
    assert admitted.confidence == 0.61
    assert admitted.id in {f.id for f in hierarchical.list_facts(root)}


def test_named_trace_replays_do_not_reinforce_legacy_but_new_traces_do(root):
    original = source(root, source_trace_id="trace-a")
    replay = source(root, source_trace_id="trace-a")
    assert replay.to_dict() == original.to_dict()
    second = source(root, source_trace_id="trace-b")
    assert second.confirmations == 2
    anonymous = source(root)
    assert anonymous.confirmations == 3


@pytest.mark.parametrize("mutation", ["update", "body", "provenance"])
def test_source_correction_propagates_without_trusting_stored_revision(root, mutation):
    premise = source(root, "A gauge says the Lumen timeout is 5 seconds")
    target = derived(root, [premise])
    old_receipt = target.evidence[0]
    if mutation == "update":
        hierarchical.update_fact(root, premise.id, statement="A gauge says the Lumen timeout is 9 seconds")
    else:
        with hierarchical.mutate_facts(root) as facts:
            if mutation == "body":
                facts[premise.id].statement = "A gauge says the Lumen timeout is 9 seconds"
            else:
                facts[premise.id].source_traces.append("changed-origin")
    assert assess(root, target.id).status == "stale"
    assert not recall.recall(root, target.statement, channels=("facts",)).items or all(
        item.id != "fact:" + target.id for item in recall.recall(root, target.statement, channels=("facts",)).items)
    with pytest.raises(EvidenceError):
        bind_evidence(root, "fact", premise.id, revision=old_receipt.revision)
    fresh_receipt = bind_evidence(root, "fact", premise.id)
    repaired, _ = hierarchical.add_fact(root, target.statement, evidence=[fresh_receipt])
    assert assess(root, target.id).status == "supported"
    assert repaired.confirmations == 1
    assert len(repaired.evidence) == 2
    assert repaired.evidence[0] == old_receipt


def test_derived_body_correction_requires_explicit_readmission(root):
    premise = source(root, "The test recorded a timeout of 5 seconds")
    target = derived(root, [premise])
    hierarchical.update_fact(root, target.id, statement="The Lumen timeout is 9 seconds")
    assert assess(root, target.id).status == "stale"
    hierarchical.add_fact(root, "The Lumen timeout is 9 seconds")
    assert assess(root, target.id).status == "stale"
    repaired, _ = hierarchical.add_fact(root, "The Lumen timeout is 9 seconds",
                                        evidence=[bind_evidence(root, "fact", premise.id)])
    assert assess(root, repaired.id).eligible


@pytest.mark.parametrize("operation", ["forget", "delete"])
def test_multihop_current_revocation_denies_even_historical_use(root, operation):
    premise = source(root, "An instrument measured the Lumen timeout")
    middle = derived(root, [premise], statement="The measured timeout is reliable")
    target = derived(root, [middle])
    assert assess(root, target.id).eligible
    if operation == "forget":
        hierarchical.forget_fact(root, premise.id)
    else:
        hierarchical.delete_fact(root, premise.id)
    assert not assess(root, target.id).eligible
    assert not assess(root, target.id, as_of="2026-05-01T00:00:00Z").eligible
    if operation == "forget":
        hierarchical.forget_fact(root, premise.id, undo=True)
        assert assess(root, target.id).eligible


def test_superseded_source_remains_available_only_in_its_historical_window(root):
    premise = source(root, "An instrument measured the Lumen timeout as 5 seconds")
    target = derived(root, [premise])
    replacement, _ = hierarchical.add_fact(root, "An instrument measured the Lumen timeout as 9 seconds",
                                           valid_from="2026-06-01T00:00:00Z")
    old, _ = hierarchical.resolve_contradiction(root, premise.id, replacement.id)
    assert old.valid_until == replacement.valid_from
    assert not assess(root, target.id).eligible
    assert assess(root, target.id, as_of="2026-05-01T00:00:00Z").eligible
    assert not assess(root, target.id, as_of="2026-06-01T00:00:00Z").eligible


def test_refutation_is_not_overridden_by_confidence_or_replay(root):
    premise = source(root, "The instrument contradicts the proposed Lumen timeout")
    target, _ = hierarchical.add_fact(root, "The Lumen timeout is 5 seconds", confidence=1,
                                      evidence=[bind_evidence(root, "fact", premise.id),
                                                bind_evidence(root, "fact", premise.id, polarity="refute")])
    assessment = assess(root, target.id)
    assert assessment.status == "refuted"
    assert assessment.supports == assessment.refutes == 1
    assert target.id not in {f.id for f in hierarchical.list_facts(root)}


def test_private_source_cannot_be_widened_but_can_be_narrowed(root):
    premise = source(root, scopes=["payments", "internal"])
    receipt = bind_evidence(root, "fact", premise.id)
    for scopes in ([], ["public"], ["payments", "public"]):
        with pytest.raises(EvidenceError):
            hierarchical.add_fact(root, "Derived private claim", evidence=[receipt], scopes=scopes)
    target, _ = hierarchical.add_fact(root, "Derived private claim", evidence=[receipt], scopes=["payments"])
    assert assess(root, target.id).eligible


@pytest.mark.parametrize("min_support", [0, -1, 257, True, 1.5])
def test_invalid_admission_policy_is_rejected_atomically(root, min_support):
    with pytest.raises(ValueError):
        hierarchical.add_fact(root, "No invalid policy persists", evidence=[], min_support=min_support)
    assert hierarchical.load_facts(root) == {}


def test_missing_source_and_receipt_shape_fail_closed(root):
    with pytest.raises(EvidenceError):
        bind_evidence(root, "fact", "missing")
    with pytest.raises(EvidenceError):
        FactEvidence("fact", "../escape", "0" * 64)
    with pytest.raises(EvidenceError):
        FactEvidence("fact", "valid", "0" * 64, recorded_at="2026-01-01")
    with pytest.raises(EvidenceError):
        hierarchical.add_fact(root, "Missing evidence", evidence=[FactEvidence("fact", "missing", "0" * 64)])
    assert not hierarchical.load_facts(root)


def test_recursive_depth_and_work_budgets_are_bounded(root):
    parent = source(root, "Leaf evidence")
    for index in range(5):
        parent = derived(root, [parent], statement=f"Derived statement number {index}")
    facts = hierarchical.load_facts(root)
    assert EvidenceResolver(root, facts).assess(parent.id).eligible
    assert not EvidenceResolver(root, facts, max_depth=3).assess(parent.id).eligible
    assert not EvidenceResolver(root, facts, max_checks=3).assess(parent.id).eligible


def test_self_evidence_is_rejected_without_mutating_source(root):
    premise = source(root)
    before = premise.to_dict()
    with pytest.raises(EvidenceError):
        hierarchical.add_fact(root, premise.statement, evidence=[bind_evidence(root, "fact", premise.id)])
    assert hierarchical.load_facts(root)[premise.id].to_dict() == before


def test_evidence_batch_failure_rolls_back_all_new_facts(root):
    premise = source(root, "The measured timeout is available")
    before = hierarchical.load_facts(root)
    with pytest.raises(EvidenceError):
        hierarchical.add_facts(root, [
            {"statement": "A valid derived claim", "evidence": [bind_evidence(root, "fact", premise.id)]},
            {"statement": "An unavailable derived claim", "evidence": [FactEvidence("fact", "missing", "0" * 64)]},
        ])
    assert hierarchical.load_facts(root) == before


@pytest.mark.parametrize("parameter,value", [("max_depth", 0), ("max_depth", 17),
                                             ("max_checks", 0), ("max_checks", 4097)])
def test_dependency_budget_configuration_has_hard_limits(root, parameter, value):
    with pytest.raises(EvidenceError):
        EvidenceResolver(root, {}, **{parameter: value})


def test_cycle_guard_terminates_with_corrupted_dependencies(root, monkeypatch):
    first = source(root, "Cycle first")
    second = source(root, "Cycle second")
    facts = {first.id: replace(first, evidence_bound=True, evidence_revision="0" * 64,
                               evidence=[FactEvidence("fact", second.id, "0" * 64)]),
             second.id: replace(second, evidence_bound=True, evidence_revision="0" * 64,
                                evidence=[FactEvidence("fact", first.id, "0" * 64)])}
    monkeypatch.setattr("commontrace.fact_evidence.claim_revision", lambda fact: "0" * 64)
    assert not EvidenceResolver(root, facts).assess(first.id).eligible


def test_future_and_expired_sources_cannot_be_admitted(root):
    future, _ = hierarchical.add_fact(root, "A future statement", valid_from="2999-01-01T00:00:00Z")
    expired, _ = hierarchical.add_fact(root, "An expired statement", valid_from="2020-01-01T00:00:00Z",
                                       expires_at="2021-01-01T00:00:00Z")
    for fact in (future, expired):
        with pytest.raises(EvidenceError):
            bind_evidence(root, "fact", fact.id)


def test_atomic_contradiction_rejects_utc_older_or_private_target_without_orphan(root):
    old, _ = hierarchical.add_fact(root, "The Lumen limit is 5", valid_from="2026-01-01T01:00:00+02:00",
                                   scopes=["payments"])
    newer, _ = hierarchical.add_fact(root, "The Lumen limit is 9", valid_from="2026-01-01T00:00:00Z",
                                     scopes=["payments"])
    closed, _ = hierarchical.resolve_contradiction(root, old.id, newer.id)
    assert closed.valid_until == newer.valid_from
    with pytest.raises(ValueError):
        hierarchical.resolve_contradiction(root, newer.id, "The Lumen limit is 99", scopes=["other"])
    assert len(hierarchical.load_facts(root)) == 2


def test_competing_contradiction_writers_commit_one_transition(root):
    old = source(root, "The Lumen limit is 5")
    def resolve(statement):
        try:
            return hierarchical.resolve_contradiction(root, old.id, statement)
        except ValueError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(resolve, ["The Lumen limit is 9", "The Lumen limit is 12"]))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert len(hierarchical.load_facts(root)) == 2


def test_bound_supersession_does_not_silently_drop_admission(root):
    premise = source(root, "An instrument measured the Lumen limit")
    target = derived(root, [premise])
    _, replacement = hierarchical.resolve_contradiction(root, target.id, "The Lumen timeout is 9 seconds")
    assert replacement.evidence_bound
    assert assess(root, replacement.id).status == "insufficient"


def test_consolidation_quotes_real_sources_and_revocation_hides_persisted_observation(root):
    first = source(root, "Instrument A logged a timeout of 5 seconds")
    second = source(root, "Instrument B logged a timeout of 5 seconds")
    target = derived(root, [first, second], min_support=2)
    produced = observations.consolidate_facts(root)
    observation = next(o for o in produced if o.source_fact_ids == [target.id])
    assert observation.proof_count == 2
    assert {row["quote"] for row in observation.evidence} == {first.statement, second.statement}
    assert observation.source_revisions == {target.id: claim_revision(target)}
    assert observations.get_observation(root, observation.id) is not None
    hierarchical.delete_fact(root, first.id)
    assert observations.get_observation(root, observation.id) is None


def test_persisted_observation_cannot_inflate_or_forge_bound_proof(root):
    target = derived(root, [source(root, "Instrument A"), source(root, "Instrument B")], min_support=2)
    observation = next(o for o in observations.consolidate_facts(root) if o.source_fact_ids == [target.id])
    observations.save_observations(root, {observation.id: replace(observation, proof_count=999)})
    assert observations.get_observation(root, observation.id) is None
    evidence = [dict(row) for row in observation.evidence]
    evidence[0]["quote"] = target.statement
    observations.save_observations(root, {observation.id: replace(observation, evidence=evidence)})
    assert observations.get_observation(root, observation.id) is None


def test_lesson_canonical_identity_and_signed_revocation_propagate(root):
    path = Path(root, "memory", "lessons", "lesson_lantern.md")
    path.parent.mkdir(parents=True)
    fm = {"name": "lesson_lantern", "status": "active", "scopes": ["payments"],
          "source_traces": ["trace-a"], "description": "Timeout guidance"}
    body = "Use a 5 second timeout for the Lumen database.\n"
    fm[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(root, str(path), fm, body, actor="reviewer")
    frontmatter.write(str(path), fm, body)
    receipt = bind_evidence(root, "lesson", "lesson_lantern")
    assert receipt.source_id == "lantern"
    target, _ = hierarchical.add_fact(root, "The Lumen timeout is 5 seconds", scopes=["payments"],
                                      evidence=[receipt])
    assert assess(root, target.id).eligible
    lesson_admission.revoke(root, str(path), actor="reviewer")
    assert not assess(root, target.id).eligible
    assert not assess(root, target.id, as_of="2026-05-01T00:00:00Z").eligible


def test_lesson_quote_rechecks_revision_after_dependency_snapshot(root):
    path = Path(root, "memory", "lessons", "lesson_lantern.md")
    path.parent.mkdir(parents=True)
    fm = {"name": "lesson_lantern", "status": "active"}
    frontmatter.write(str(path), fm, "The original measured timeout is 5 seconds.\\n")
    receipt = bind_evidence(root, "lesson", "lesson_lantern")
    resolver = EvidenceResolver(root, {})
    assert resolver.source(receipt)[0] == receipt.revision
    frontmatter.write(str(path), fm, "The corrected measured timeout is 9 seconds.\\n")
    assert resolver.source_quote(receipt) is None


def test_cli_evidence_and_temporal_resolution_flow(root):
    assert cli(root, "init", "--agent-type", "coding").returncode == 0
    first = source(root, "Instrument A logged 5 seconds")
    second = source(root, "Instrument B logged 5 seconds")
    statement = "The Lumen database timeout is 5 seconds"
    flags = ("--evidence", f"fact:{first.id}", "--evidence", f"fact:{second.id}", "--min-support", "2")
    added = cli(root, "fact", "add", statement, *flags)
    assert added.returncode == 0, added.stderr
    replay = cli(root, "fact", "add", statement, *flags)
    assert replay.returncode == 0
    assert "Retained source-bound fact" in replay.stdout
    packed = recall.recall(root, statement, channels=("facts",))
    bound_item = next(item for item in packed.items if item.text == statement)
    assert bound_item.to_dict()["provenance"]["assessment"]["supports"] == 2
    old = source(root, "The Lumen limit is 5")
    new, _ = hierarchical.add_fact(root, "The Lumen limit is 9", valid_from="2026-06-01T00:00:00Z")
    result = cli(root, "fact", "resolve", old.id, new.id)
    assert result.returncode == 0, result.stderr
    current = recall.recall(root, "Lumen limit", channels=("facts",))
    historical = recall.recall(root, "Lumen limit", channels=("facts",), as_of="2026-05-01T00:00:00Z")
    assert old.statement not in {item.text for item in current.items}
    assert new.statement in {item.text for item in current.items}
    assert old.statement in {item.text for item in historical.items}
    assert new.statement not in {item.text for item in historical.items}


def test_real_mcp_admits_queries_and_revokes_bound_facts(root):
    pytest.importorskip("mcp")
    from commontrace import mcp_server
    assert cli(root, "init", "--agent-type", "coding").returncode == 0
    premise = source(root, "Instrument logged a Lumen timeout of 5 seconds")
    server = mcp_server.build_server(root)
    def call(name, **arguments):
        result = asyncio.run(server.call_tool(name, arguments))
        if getattr(result, "structured_content", None):
            return result.structured_content.get("result", result.structured_content)
        return json.loads(result.content[0].text)
    out = call("record_fact", statement="The Lumen timeout is 5 seconds",
               evidence=[{"kind": "fact", "source_id": premise.id}])
    assert out["ok"], out
    target_id = out["fact"]["id"]
    assert out["evidence_assessment"]["status"] == "supported"
    results = call("query_facts", query="Lumen timeout")
    assert any(row["fact"]["id"] == target_id for row in results["facts"])
    bad = call("record_fact", statement="Reject caller-controlled receipt time",
               evidence=[{"kind": "fact", "source_id": premise.id, "recorded_at": "1900-01-01"}])
    assert not bad["ok"]
    hierarchical.delete_fact(root, premise.id)
    assert not call("query_facts", query="Lumen timeout")["facts"]
