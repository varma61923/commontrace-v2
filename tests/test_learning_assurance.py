"""Security boundaries and independent experimental estimands, not implementation mirrors."""
from __future__ import annotations

import json
import math
import random

import pytest

from commontrace import (
    agent_hooks,
    assurance,
    compression,
    federation,
    hierarchical,
    holdout_io,
    memory_authority,
    origin,
    policy,
    trace_io,
    wrap_openai,
)
from commontrace.client import MemoryClient


def test_cjk_native_text_and_derived_forgetting(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0, salt="test")
    memory = MemoryClient(root)
    parent = memory.add("我的办公室在东京。")["facts"][0]
    assert memory.search("东京")[0]["text"] == "我的办公室在东京。"
    child = hierarchical.append_facts(root, [{"statement": "东京 office child", "source_trace_id": parent["id"]}])[0][0]
    grandchild = hierarchical.append_facts(root, [{"statement": "东京 office grandchild", "source_trace_id": child.id}])[0][0]
    assert memory.search("grandchild")
    hierarchical.forget_fact(root, parent["id"])
    assert not memory.search("child") and not memory.profile("office")["dynamic"]
    certificate = memory_authority.forgetting_certificate(root, parent["id"])
    assert {parent["id"], child.id, grandchild.id} <= set(certificate["blocked_records"])
    assert certificate["history_erased"] is False
    assert memory_authority.verify(root, certificate["origin"], {k: v for k, v in certificate.items() if k != "origin"})
    hierarchical.forget_fact(root, parent["id"], undo=True)
    assert memory.search("grandchild")


def test_wrappers_and_hooks_cannot_launder_external_evidence(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0, salt="test")
    with memory_authority.writer("inbound-mail", "external"):
        MemoryClient(root, agent_id="a").add("Release secret is alpha")
    def create(**kwargs):
        return {"choices": [{"message": {"content": "alpha copied"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 3}}
    wrap_openai(create, root=root, agent_id="a", prices={"input": .01, "output": .02})(
        messages=[{"role": "user", "content": "Release secret"}], occasion_id="wrap")
    agent_hooks.handle(root, "start", {"occasion_id": "hook", "query": "Release secret"}, agent_id="a")
    agent_hooks.handle(root, "end", {"occasion_id": "hook", "context": "Release secret",
                       "solution": "alpha copied", "execution_valid": False, "plan_valid": True}, agent_id="a")
    receipts = [trace_io.read(str(p))[0]["extensions"]["profile"]["origin"]
                for p in (tmp_path/"memory"/"traces").glob("*.md")]
    assert len(receipts) == 2 and all(r["authority"] == "external" for r in receipts)
    report = assurance.cost_report(root)
    assert report["calls"] == 1 and report["cost"] == pytest.approx(.46)


def test_public_store_verifier_cannot_sign(tmp_path):
    pytest.importorskip("cryptography")
    root = str(tmp_path)
    legacy = memory_authority.bind(root, {"id": "old", "text": "old"})
    config = memory_authority.configure_signing(root)
    receipt = memory_authority.bind(root, {"id": "new", "text": "new"})
    assert receipt["algorithm"] == "ed25519"
    assert memory_authority.verify(root, legacy)
    (tmp_path/"memory"/".origin-ed25519-key").unlink()
    assert memory_authority.verify(root, receipt)
    with pytest.raises(PermissionError, match="signing key"):
        memory_authority.bind(root, {"id": "cannot-sign"})
    public = origin.Principal("local", "local-store", "operator", b"", "ed25519", bytes.fromhex(config["public_key"]))
    with pytest.raises(ValueError):
        origin.bind({"id": "fake"}, public)
    bad = {**receipt, "record": {"id": "new", "text": "tampered"}}
    assert not memory_authority.verify(root, bad)


def test_legacy_trace_receipt_admits_only_an_empty_new_lineage_field(tmp_path):
    root = str(tmp_path)
    trace = {"id": "legacy", "context_text": "task", "solution_text": "answer"}
    current = memory_authority.trace_record(trace)
    old = {k: v for k, v in current.items() if k != "source_traces"}
    receipt = memory_authority.bind(root, old)
    assert memory_authority.permits_record(root, receipt, current)
    assert not memory_authority.permits_record(root, receipt, {**current, "source_traces": ["new-parent"]})


def _policy_log(root, count=250):
    rng = random.Random(5)
    for i in range(count):
        delivered = rng.random() < .5
        policy.record_assignment(root, str(i), [{"memory_id": "a", "delivered": delivered,
            "treatment_probability": .5, "source_sha256": "fixed"}], baseline=[], scopes=[])
        policy.outcome(root, str(i), float(delivered))


def test_policy_gate_recovers_known_held_out_effect_with_joint_support(tmp_path):
    root = str(tmp_path)
    candidate = {"kind": "exploration-delivery", "default_probability": .9}
    harmful = {"kind": "exploration-delivery", "default_probability": .1}
    policy.preregister(root, candidate, evaluation_samples=250)
    policy.preregister(root, harmful, evaluation_samples=250)
    _policy_log(root)
    report = policy.evaluate(root, candidate)
    assert report["safe_to_release"] and report["ci_low"] > 0
    assert report["ips"] == pytest.approx(.9, abs=.08)
    assert report["snips"] == pytest.approx(.9, abs=.04)
    assert report["ips_ci"][0] <= .9 <= report["ips_ci"][1]
    assert report["snips_ci"][0] <= .9 <= report["snips_ci"][1]
    assert not policy.evaluate(root, harmful)["safe_to_release"]
    with pytest.raises(ValueError, match="joint support"):
        policy.evaluate(root, {"kind": "ranker", "model": "different"})
    policy.install(root, candidate)
    assert policy.delivery_probabilities(root)["default_probability"] == .9
    with pytest.raises(ValueError, match="immutable"):
        policy.outcome(root, "0", .5)
    file = tmp_path/"memory"/"policy_events.jsonl"
    rows = [json.loads(line) for line in file.read_text().splitlines()]
    rows[-1]["outcome"] = .5
    file.write_text("\n".join(json.dumps(r) for r in rows)+"\n")
    with pytest.raises(PermissionError):
        policy.evaluate(root, candidate)


def test_policy_missing_outcomes_not_zero_and_occasions_not_memory_rows(tmp_path):
    root = str(tmp_path)
    for i in range(10):
        arms = [{"memory_id": str(j), "delivered": True, "treatment_probability": .5,
                 "source_sha256": "fixed"} for j in range(40)]
        policy.record_assignment(root, str(i), arms, baseline=[], scopes=[])
        policy.outcome(root, str(i), 1.)
    assert not policy.evaluate(root, {"kind": "exploration-delivery"})["identified"]


def test_forensics_and_action_ablation_bind_the_actual_recall(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0, salt="test")
    memory = MemoryClient(root)
    fact = memory.add("release is approved")["facts"][0]
    receipt = memory.reflect("release", occasion_id="incident")
    report = assurance.forensics(root, receipt["occasion_id"], lambda items: float(bool(items)), seed=2)
    assert report["results"][0]["memory_id"] == fact["id"]
    assert report["results"][0]["sensitivity"] == 1.
    assert not report["causal_live_proof"] and memory_authority.verify(root, report["origin"])
    vote = assurance.action_vote(root, receipt["occasion_id"], {"tool": "release"}, lambda items, action: bool(items))
    assert not vote["allowed"]  # Requires the full memory; empty-context vote fails.
    assert assurance.action_vote(root, receipt["occasion_id"], {"tool": "release"}, lambda *a: True)["allowed"]
    hierarchical.forget_fact(root, fact["id"])
    with pytest.raises(PermissionError):
        assurance.action_vote(root, receipt["occasion_id"], {"tool": "release"}, lambda *a: True)
    with memory_authority.writer("untrusted-agent", "agent"), pytest.raises(PermissionError):
        assurance.forensics(root, receipt["occasion_id"], lambda _: 1.)


def test_randomized_response_charges_budget_and_imports_review_only(tmp_path, monkeypatch):
    root = str(tmp_path)
    federation.configure_privacy(root, epsilon=2*math.log(3))
    monkeypatch.setattr(federation.secrets, "randbelow", lambda n: 1)
    steps = [{"tool_category": "read", "has_guard": True, "has_branch": False}]
    payload = federation.randomized_response(root, [True, False, True], public_cohort_id="public-1",
                                            public_cohort_size=3, public_steps=steps)
    assert payload["randomized_positive"] == 2 and payload["privacy"]["delta"] == 0
    signer = origin.Principal("publisher", "fleet-a", "federation-export", b"a"*32)
    approval = origin.bind({"export_digest": payload["export_digest"]}, signer)
    imported = federation.receive(root, payload, approval=approval, principals={signer.id: signer})
    assert imported["data"]["status"] == "review"
    federation.randomized_response(root, [True, False, True], public_cohort_id="public-1",
                                  public_cohort_size=3, public_steps=steps)
    with pytest.raises(PermissionError, match="budget exhausted"):
        federation.randomized_response(root, [True, False, True], public_cohort_id="public-1",
                                      public_cohort_size=3, public_steps=steps)


def test_replicated_lift_authenticates_orgs_and_rejects_mixed_evidence():
    principals = {str(i): origin.Principal(str(i), "fleet-"+str(i), "fleet-effect", bytes([i+1])*32) for i in range(3)}
    record = {"artifact_sha256": "a"*64, "comparison": "raw", "metric": "resolved",
              "effect": .2, "standard_error": .04, "simulated": False}
    receipts = [origin.bind(record, p) for p in principals.values()]
    signer = origin.Principal("referee", "referee", "certificate", b"s"*32)
    certificate = federation.replicated_lift(receipts, principals=principals, signer=signer)
    assert origin.verify(certificate, {signer.id: signer})
    assert certificate["record"]["pooled"]["organizations"] == 3
    with pytest.raises(ValueError, match="independent"):
        federation.replicated_lift([receipts[0], receipts[0]], principals=principals, signer=signer)
    receipts[-1] = origin.bind({**record, "simulated": True}, principals["2"])
    with pytest.raises(ValueError, match="simulated/real"):
        federation.replicated_lift(receipts, principals=principals, signer=signer)


def test_compression_real_experiment_promotion_and_raw_control(tmp_path):
    root = str(tmp_path)
    trace = trace_io.write_new(root, title="case", context="release", solution="step one", tags=["test"])
    tid = trace_io.read(trace)[0]["id"]
    row = compression.propose(root, "Follow verified release steps", level="episode", sources=[tid],
                              actor="author", applies_when="release", do_not_apply_when="other")
    experiment = compression.register(root, row["id"], trials=300, seed="fixed")
    assert compression.evaluate(root, experiment["id"])["verdict"] == "UNIDENTIFIED"
    for _ in range(300):
        trial = compression.next_trial(root, experiment["id"])
        compression.outcome(root, trial["id"], float(trial["arm"] == "candidate"))
    assert compression.evaluate(root, experiment["id"])["verdict"] == "HELPS"
    with pytest.raises(PermissionError):
        compression.review(root, row["id"], experiment["id"], actor="author", expected_revision=row["revision"])
    admitted = compression.review(root, row["id"], experiment["id"], actor="reviewer", expected_revision=row["revision"])
    assert compression.active(root)[0]["id"] == admitted["id"]
    with pytest.raises(ValueError, match="adjacent"):
        compression.propose(root, "invalid skip", level="skill", sources=[tid], parent=admitted["id"],
                            actor="a", applies_when="x", do_not_apply_when="y")
    observation = compression.propose(root, "summarized lesson", level="observation", sources=[tid], parent=admitted["id"],
                                    actor="a", applies_when="release", do_not_apply_when="other")
    assert observation["data"]["parent_sha256"] == policy.digest(admitted)
    harmful = compression.register(root, admitted["id"], trials=300, seed="fixed")
    for _ in range(300):
        trial = compression.next_trial(root, harmful["id"])
        compression.outcome(root, trial["id"], float(trial["arm"] != "candidate"))
    assert compression.evaluate(root, harmful["id"])["verdict"] == "HURTS"
    assert not compression.active(root)


def test_scoped_directives_and_mutating_evaluators_cannot_bypass_vote(tmp_path):
    from commontrace import memory_control

    root = str(tmp_path)
    holdout_io.configure(root, rate=0, salt="test")
    memory = MemoryClient(root, agent_id="a")
    memory.add("release context")
    memory_control.directive(root, "No release", labels=["agent:a"], deny_tools=["release"])
    receipt = memory.reflect("release", occasion_id="scoped")
    with pytest.raises(PermissionError):
        assurance.action_vote(root, receipt["occasion_id"], {"tool": "release"}, lambda *a: True)
    memory_control.directive(root, "No destroy", deny_tools=["destroy"])
    action = {"tool": "read"}
    def mutate(items, proposed):
        proposed["tool"] = "destroy"
        items.clear()
        return True
    voted = assurance.action_vote(root, receipt["occasion_id"], action, mutate)
    assert voted["action_sha256"] == policy.digest({"tool": "read"})
    assert action == {"tool": "read"}
    assert assurance.recall(root, receipt["occasion_id"])["evidence"]


def test_hook_invalid_receipt_and_delayed_final_capture(tmp_path):
    root = str(tmp_path)
    payload = {"occasion_id": "session", "context": "task", "solution": "in progress", "final": False}
    assert not agent_hooks.handle(root, "end", payload, agent_id="a")["captured"]
    payload.update(final=True, succeeded=True)
    assert agent_hooks.handle(root, "end", payload, agent_id="a")["captured"]
    assert trace_io.read(str(next((tmp_path/"memory"/"traces").glob("*.md"))))[0]["outcome"]["resolved"]
    agent_hooks.handle(root, "start", {"occasion_id": "corrupt", "query": "task"}, agent_id="a")
    file = tmp_path/"memory"/"recall-receipts.jsonl"
    rows = [json.loads(line) for line in file.read_text().splitlines()]
    rows[-1]["context"] = "tampered"
    file.write_text("\n".join(json.dumps(row) for row in rows)+"\n")
    with pytest.raises(ValueError, match="authenticated"):
        agent_hooks.handle(root, "end", {"occasion_id": "corrupt", "context": "task", "solution": "done"}, agent_id="a")


def test_origin_ids_cannot_cross_record_kinds(tmp_path):
    root = str(tmp_path)
    with memory_authority.writer("email", "external"):
        fact = MemoryClient(root).add("External assertion")["facts"][0]
    with memory_authority.writer("agent", "agent"), pytest.raises(ValueError, match="different record kind"):
        trace_io.write_new(root, trace_id=fact["id"], title="spoof", context="different", solution="different", tags=[])
    child = hierarchical.append_facts(root, [{"statement": "derived", "source_trace_id": fact["id"]}])[0][0]
    assert child.origin["authority"] == "external"


def test_retrospective_policy_and_fake_compression_status_never_admit(tmp_path):
    from commontrace import memory_control

    root = str(tmp_path)
    _policy_log(root, 50)
    candidate = {"kind": "exploration-delivery", "default_probability": .9}
    assert not policy.evaluate(root, candidate)["safe_to_release"]
    policy.preregister(root, candidate, evaluation_samples=50)
    assert not policy.evaluate(root, candidate)["identified"]
    trace = trace_io.write_new(root, title="case", context="x", solution="y", tags=[])
    proposal = compression.propose(root, "abstract", level="episode", sources=[trace_io.read(trace)[0]["id"]],
                                  actor="author", applies_when="x", do_not_apply_when="y")
    memory_control.put(root, "compression", proposal["text"], record_id=proposal["id"],
                       data={**proposal["data"], "status": "active"})
    assert not compression.active(root)


def test_unscoped_generated_occasions_are_distinct_and_signed(tmp_path):
    memory = MemoryClient(str(tmp_path))
    a = memory.reflect("first")
    b = memory.reflect("second")
    assert a["occasion_id"] != b["occasion_id"]
    assert assurance.recall(str(tmp_path), a["occasion_id"])["id"] == a["occasion_id"]


def test_action_vote_rechecks_revocation_after_judges(tmp_path):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0, salt="test")
    memory = MemoryClient(root)
    fact = memory.add("release context")["facts"][0]
    occasion = memory.reflect("release")["occasion_id"]
    def judge(*args):
        hierarchical.forget_fact(root, fact["id"])
        return True
    with pytest.raises(PermissionError):
        assurance.action_vote(root, occasion, {"tool": "release"}, judge)


def test_rebuildable_markdown_store_does_not_serve_revoked_index(tmp_path):
    from commontrace.store import SQLiteStore

    root = str(tmp_path)
    store = SQLiteStore(root)
    record = store.append({"id": "stored", "text": "Native 東京 evidence"})
    assert store.rebuild() == 1
    assert store.search("東京")[0]["origin"] == record["origin"]
    memory_authority.record_forgetting(root, "stored", forgotten=True)
    assert not store.search("東京")  # Stale SQLite is a candidate index only.
    assert store.rebuild() == 0


def test_provider_sync_pinned_origin_authority_and_failure_watermark(tmp_path):
    from commontrace.connectors.knowledge import sync

    root = str(tmp_path)
    payload = json.dumps({"messages": [{"text": "Office is 東京"}],
                          "response_metadata": {"next_cursor": "next"}})
    calls = []
    def fetch(url):
        calls.append(url)
        return payload if len(calls) == 1 else json.dumps({"messages": [{"text": "Office is 大阪"}]})
    report = sync(root, "slack", "channel", token="secret", context=["agent:a"], fetch=fetch)
    assert report["pages"] == 2 and not report["pending"]
    facts = hierarchical.list_facts(root)
    assert len(facts) == 2 and all(f.origin["authority"] == "external" for f in facts)
    state = next((tmp_path/"memory"/"connectors").glob("knowledge-*.json"))
    before = state.read_bytes()
    def crossed(url):
        return json.dumps({"next_page": "https://evil.example/steal"})
    with pytest.raises(ValueError, match="pinned origin"):
        sync(root, "slack", "channel", token="secret", context=["agent:a"], fetch=crossed)
    assert state.read_bytes() == before


def test_query_plan_rejects_executable_or_unbounded_output(tmp_path):
    from commontrace.query_plan import plan, retrieve

    root = str(tmp_path)
    MemoryClient(root).add("Tokyo office")
    assert retrieve(root, "Tokyo and office")
    with pytest.raises(ValueError):
        plan("Tokyo", lambda prompt: '{"sql":"DROP TABLE facts"}')
    with pytest.raises(ValueError):
        plan("Tokyo", lambda prompt: json.dumps({"subqueries": ["Tokyo"], "category": "", "hops": 100, "entity_ids": []}))
    for limit in (-1, True, 1001):
        with pytest.raises(ValueError):
            retrieve(root, "Tokyo", limit=limit)


@pytest.mark.parametrize("provider", ["drive", "onedrive"])
def test_provider_document_json_is_content_not_an_api_envelope(tmp_path, provider):
    from commontrace.connectors.knowledge import sync

    text = '{"configuration":"Tokyo office", "error":"example field"}'
    report = sync(str(tmp_path), provider, "document", token="secret", context=["agent:alice"],
                  fetch=lambda _: text)
    assert report["facts_written"] > 0
    snapshots = list((tmp_path/"memory"/"traces").glob("*.md"))
    assert any(text in p.read_text() for p in snapshots)
    assert all(f.origin["authority"] == "external" for f in hierarchical.list_facts(str(tmp_path)))


def test_native_provider_rich_text_and_graphql_pagination(tmp_path):
    from commontrace.connectors.knowledge import _text, sync

    assert "Tokyo" in _text({"body": {"storage": {"value": "<p>Tokyo office</p>"}}})
    assert "Tokyo" in _text({"fields": {"summary": "Tokyo office"}})
    calls = []
    def fetch(url):
        calls.append(url)
        return json.dumps({"data": {"issues": {"nodes": [{"title": "Tokyo office"}],
            "pageInfo": {"hasNextPage": len(calls) == 1, "endCursor": "next"}}}})
    report = sync(str(tmp_path), "linear", "team", token="secret", context=["agent:alice"], fetch=fetch)
    assert report["pages"] == 2 and not report["pending"] and "cursor=next" in calls[-1]


@pytest.mark.parametrize("context", [[" "], ["agent:a\n"], [], [""]])
def test_provider_rejects_empty_or_control_owner_scope_before_fetch(tmp_path, context):
    from commontrace.connectors.knowledge import sync

    def fetch(_):
        pytest.fail("an invalid owner scope reached the provider")
    with pytest.raises(ValueError, match="explicit owner scope"):
        sync(str(tmp_path), "slack", "channel", token="secret", context=context, fetch=fetch)
    assert not hierarchical.list_facts(str(tmp_path))


def test_dr_same_joint_model_and_real_pre_registration_training(tmp_path):
    root = str(tmp_path)
    policy.record_assignment(root, "training", [{"memory_id": "a", "delivered": True,
        "treatment_probability": .5, "source_sha256": "fixed"}], baseline=[], scopes=[])
    policy.outcome(root, "training", 0.)
    candidate = {"kind": "exploration-delivery", "training_occasions": ["training"],
        "predictions": {str(i): {"memory_ids": ["a"], "values": {"0": 1., "1": 1.}} for i in range(30)}}
    policy.preregister(root, candidate, evaluation_samples=30)
    for i in range(30):
        policy.record_assignment(root, str(i), [{"memory_id": "a", "delivered": bool(i%2),
            "treatment_probability": .5, "source_sha256": "fixed"}], baseline=[], scopes=[])
        policy.outcome(root, str(i), 0.)
    report = policy.evaluate(root, candidate)
    assert report["ips"] == report["dr"] == 0.
    assert report["dr_ci"][0] <= 0. <= report["dr_ci"][1]
