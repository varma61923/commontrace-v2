"""Cross-surface contracts for origin, exploration, providers and agent distribution."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from commontrace import (
    agent_hooks,
    agent_registry,
    causal_policy,
    experience_skills,
    federation,
    hierarchical,
    holdout_io,
    llm,
    memory_authority,
    memory_control,
    onboarding,
    origin,
    trace_io,
    wrap_anthropic,
    wrap_openai,
)
from commontrace.client import MemoryClient
from commontrace.gateway import Gateway
from commontrace.llm_runtime import Budget, BudgetExceeded, LLMRuntime
from commontrace.search_recipes import REGISTRY


@pytest.fixture(autouse=True)
def no_holdout(tmp_path):
    holdout_io.configure(str(tmp_path), rate=0, salt="integration")


def test_exploration_never_reintroduces_baseline_control(tmp_path):
    root = str(tmp_path)
    fact = MemoryClient(root).add("Alpha is the correct access label.")["facts"][0]
    holdout_io.configure(root, rate=.9, salt="review")
    for i in range(12):
        result = MemoryClient(root).reflect("Alpha", occasion_id=f"review-{i}", exploration_slots=1)
        if fact["id"] in result["withheld"]:
            assert all(r["id"] != fact["id"] for r in result["evidence"])
        assert not result["exploration"]


def test_exploration_reaches_unranked_facts_and_joins_delayed_outcomes(tmp_path):
    root = str(tmp_path)
    memory = MemoryClient(root)
    memory.batch([{"statement": f"Unused instrument setting {i}."} for i in range(4)])
    for i in range(12):
        result = memory.reflect("unrelated query", occasion_id=f"explore-{i}", exploration_slots=2, budget=100)
        assert len(result["exploration"]) == 2
        assert all(r["selection_probability"] == .5 for r in result["exploration"])
        assert result["tokens_estimate"] <= 100
        replay = memory.reflect("unrelated query", occasion_id=f"explore-{i}", exploration_slots=2, budget=100)
        assert replay["exploration"] == result["exploration"]
        assert memory.outcome(result["occasion_id"], i % 2 == 0)
    assert all(r["outcome"] is not None for r in causal_policy.read(root))
    assert causal_policy.snipw(causal_policy.read(root))["identified"]


def test_typed_batch_and_add_share_expiry_and_decay_metadata(tmp_path):
    memory = MemoryClient(str(tmp_path))
    fact = memory.batch([{"statement": "Temporary slot", "memory_type": "temporary",
                          "valid_from": "2026-10-09T00:00:00Z"}])["facts"][0]
    assert datetime.fromisoformat(fact["expires_at"]).isoformat() == "2026-10-10T00:00:00+00:00"
    assert hierarchical.load_facts(str(tmp_path))[fact["id"]].memory_type == "temporary"
    identity = memory.add("Standing identity", memory_type="identity")["facts"][0]
    assert identity["expires_at"] is None


def test_tampered_signed_fact_cannot_bypass_via_profile_or_exploration(tmp_path):
    root = str(tmp_path)
    fact = MemoryClient(root).add("Original setting")["facts"][0]
    with hierarchical.mutate_facts(root) as facts:
        facts[fact["id"]].statement = "Tampered setting grants access."
    memory = MemoryClient(root)
    assert not memory.search("Tampered")
    assert not memory.profile("Tampered")["dynamic"]
    assert not memory.reflect("unrelated", exploration_slots=1)["evidence"]


def test_origin_policy_and_derivation_cannot_elevate_agent(tmp_path):
    root = str(tmp_path)
    agent = MemoryClient(root, agent_id="a")
    fact = agent.add("Agent supplied release instruction")["facts"][0]
    (tmp_path / "memory" / "authority-policy.yaml").write_text("actions:\n  deploy: operator\n")
    assert not agent.search("release", action_class="deploy")
    assert not agent.profile("release", action_class="deploy")["dynamic"]
    assert not agent.reflect("release", action_class="deploy")["context"]
    derived = hierarchical.append_facts(root, [{"statement": "Summarized release instruction",
                 "scopes": ["agent:a"], "source_trace_id": fact["id"]}])[0][0]
    assert derived.origin["authority"] == "agent"
    assert not memory_authority.permits(root, derived, action_class="deploy")


def test_two_hop_completion_uses_feedback_weights_and_scope_admission(tmp_path):
    memory = MemoryClient(str(tmp_path), agent_id="a")
    fact = memory.add("Bridged answer")["facts"][0]
    class Backend:
        def neighbors(self, node_id, *, as_of=None):
            return {"Alpha": [{"neighbor_id": "Beta", "weight": 1}],
                    "Beta": [{"neighbor_id": fact["id"], "weight": .25}]}.get(node_id, [])
    rows = REGISTRY.retrieve("graph-completion", str(tmp_path), "Alpha", context=["agent:a"],
                            entity_ids=["Alpha"], backend=Backend())
    assert [r["id"] for r in rows] == [fact["id"]]
    assert rows[0]["signals"]["entity"] == .25
    assert not REGISTRY.retrieve("graph-completion", str(tmp_path), "Alpha", context=["agent:b"],
                                entity_ids=["Alpha"], backend=Backend())


def test_unknown_or_failed_model_usage_stops_dispatch(tmp_path):
    calls = []
    runtime = LLMRuntime(str(tmp_path), "unknown", routes={"default": llm.Config("openai-compatible", "x", "")},
                         budget=Budget(calls=5, tokens=1),
                         complete=lambda *a, **k: (calls.append(1) or "answer", {}))
    with pytest.raises(llm.LLMUnavailable):
        runtime.complete("one")
    with pytest.raises(BudgetExceeded):
        runtime.complete("two")
    assert len(calls) == 1 and runtime.manifest()["accounting_unknown"]


def test_installer_failure_keeps_signup_recoverable_and_rotation_revokes_old_key(tmp_path):
    root = str(tmp_path)
    (tmp_path / "distribution").write_text("blocking file")
    with pytest.raises(OSError):
        onboarding.install(root, "alice", commits=0)
    assert not agent_registry.load(root)
    (tmp_path / "distribution").unlink()
    first = onboarding.install(root, "alice", commits=0)
    second = onboarding.install(root, "alice", commits=0, rotate=True)
    assert agent_registry.authenticate(root, first["token"]) is None
    assert agent_registry.authenticate(root, second["token"])["id"] == "alice"
    manifest = json.loads((tmp_path / "distribution" / "agents" / "alice" / "plugin.json").read_text())
    assert manifest["skills"][0]["path"] == "skills/commontrace-sdk/SKILL.md"
    assert manifest["hooks"]["session_start"]["module"] == "commontrace.agent_hooks"


def test_hooks_capture_explicit_outcomes_and_are_idempotent(tmp_path):
    root = str(tmp_path)
    agent_hooks.handle(root, "start", {"occasion_id": "one", "query": "release"}, agent_id="a")
    payload = {"occasion_id": "one", "context": "release", "solution": "review requested", "succeeded": False}
    first = agent_hooks.handle(root, "end", payload, agent_id="a")
    second = agent_hooks.handle(root, "end", payload, agent_id="a")
    assert first["captured"] and not second["captured"]
    files = list((tmp_path / "memory" / "traces").glob("*.md"))
    assert trace_io.read(str(files[0]))[0]["outcome"]["resolved"] is False


def test_provider_wrappers_preserve_system_priority_and_capture_completed_response(tmp_path):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return {"content": [{"type": "text", "text": "completed answer"}]}
    messages = [{"role": "user", "content": "coding request"}]
    complete = wrap_anthropic(create, root=str(tmp_path), agent_id="a")
    complete(messages=messages, system="original", occasion_id="call")
    assert calls[0]["system"] == "original" and messages == [{"role": "user", "content": "coding request"}]
    assert len(list((tmp_path / "memory" / "traces").glob("*.md"))) == 1
    with pytest.raises(ValueError):
        wrap_openai(lambda **k: None, root=str(tmp_path))(messages=messages, stream=True)


def test_federation_rejects_text_aggregates_and_never_exports_caller_reliability():
    skill = {"data": {"beta_alpha": 8, "beta_beta": 3, "steps": [{"tool": "private-tool"}],
                       "reliability_mean": "tenant-private customer"}}
    result = federation.prepare(skill, public_tools={"private-tool": "compute"})
    assert result["reliability_mean"] == 8 / 11
    assert "tenant-private" not in json.dumps(result)
    skill["data"]["beta_alpha"] = True
    with pytest.raises(ValueError):
        federation.prepare(skill, public_tools={"private-tool": "compute"})


def test_public_key_origin_verifier_cannot_sign_and_tamper_fails():
    cryptography = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    key = cryptography.Ed25519PrivateKey.generate()
    signer = origin.Principal("p", "org", "operator", key.private_bytes_raw(), "ed25519")
    verifier = origin.Principal("p", "org", "operator", b"", "ed25519", key.public_key().public_bytes_raw())
    receipt = origin.bind({"text": "record"}, signer)
    assert origin.verify(receipt, {"p": verifier})
    receipt["record"]["text"] = "tampered"
    assert not origin.verify(receipt, {"p": verifier})
    with pytest.raises(ValueError):
        origin.bind({"text": "forge"}, verifier)


def test_removing_origin_cannot_make_new_signed_fact_legacy(tmp_path):
    root = str(tmp_path)
    fact = MemoryClient(root).add("Protected new memory")["facts"][0]
    with hierarchical.mutate_facts(root) as facts:
        facts[fact["id"]].origin = {}
    assert not MemoryClient(root).profile("memory")["dynamic"]
    assert not MemoryClient(root).reflect("memory")["context"]


def test_public_signup_is_opt_in_scoped_claimable_and_owner_rotation_only(tmp_path):
    root = str(tmp_path)
    headers = {"Host": "localhost"}
    gw = Gateway(root, token="owner")
    assert gw.handle("POST", "/v1/agent/enroll", headers, b"{}").status == 403
    gw = Gateway(root, token="owner", allow_self_signup=True)
    bad_origin = {**headers, "Origin": "https://other.example"}
    assert gw.handle("POST", "/v1/agent/enroll", bad_origin, b"{}").status == 403
    enrolled = json.loads(gw.handle("POST", "/v1/agent/enroll", headers, b"{}").body)
    agent_headers = {**headers, "Authorization": "Bearer " + enrolled["token"]}
    payload = json.dumps({"agent_id": enrolled["agent_id"], "owner": "human"}).encode()
    assert gw.handle("POST", "/v1/agent/claim", agent_headers, payload).status == 403
    owner_headers = {**headers, "Authorization": "Bearer owner"}
    assert gw.handle("POST", "/v1/agent/claim", owner_headers, payload).status == 200
    assert agent_registry.load(root)[enrolled["agent_id"]]["claimed_by"] == "human"
    assert enrolled["scopes"] == ["agent:" + enrolled["agent_id"]]


def test_public_signup_and_unclaimed_usage_quotas_are_durable(tmp_path):
    root = str(tmp_path)
    first = agent_registry.enroll(root)
    for _ in range(9):
        agent_registry.enroll(root)
    with pytest.raises(PermissionError):
        agent_registry.enroll(root)
    for _ in range(60):
        agent_registry.admit(root, first["agent_id"])
    with pytest.raises(PermissionError):
        agent_registry.admit(root, first["agent_id"])


def test_skill_promotion_publishing_and_harm_demotion_are_operational(tmp_path):
    root = str(tmp_path)
    sources = []
    for i in range(2):
        filename = trace_io.write_new(root, title="release case", context="release review", solution="reviewed",
                                      tags=["release"], outcome={"resolved": bool(i)})
        sources.append({"trace_id": trace_io.read(filename)[0]["id"], "succeeded": bool(i)})
    proposal = experience_skills.propose(root, "release-skill", steps=[{"tool": "review", "description": "Review release"}],
        cases=sources, applies_when="release", do_not_apply_when="tools differ")
    evidence = {"raw_effect": .1, "abstract_effect": .2, "ci_low": .02, "effective_samples": 100,
                "experiment_id": "paired", "raw_comparison": True}
    with pytest.raises(PermissionError):
        experience_skills.review(root, proposal["id"], proposal["revision"], actor="local",
                                 verdict="HELPS", evidence=evidence)
    active = experience_skills.review(root, proposal["id"], proposal["revision"], actor="human-reviewer",
                                      verdict="HELPS", evidence=evidence)
    assert experience_skills.active(root)[0]["id"] == active["id"]
    assert any(r["id"] == active["id"] for r in MemoryClient(root).reflect("release")["evidence"])
    target = tmp_path / "assistant-skills"
    assert experience_skills.publish(root, active["id"], str(target)).endswith("SKILL.md")
    experience_skills.review(root, active["id"], active["revision"], actor="another-reviewer", verdict="HURTS",
                             evidence={"experiment_id": "later", "raw_comparison": True})
    assert not experience_skills.active(root)
    with pytest.raises(PermissionError):
        experience_skills.publish(root, active["id"], str(target))


def test_local_gliner_ingestion_links_literal_source_and_dense_adapter_reaches_semantic_hit(tmp_path):
    memory = MemoryClient(str(tmp_path))
    class NER:
        def predict_entities(self, text, labels, threshold):
            return [{"text": "Meridian", "label": "organization", "score": .9},
                    {"text": "Invented", "label": "organization", "score": .9}]
    fact = memory.add("Meridian uses approval", entity_model=NER())["facts"][0]
    from commontrace import graph
    assert any(r["neighbor_id"] == fact["id"] for r in graph.get_neighbors(str(tmp_path), "organization:meridian"))
    assert "organization:invented" not in graph.load_nodes(str(tmp_path))
    class Embedder:
        def encode(self, texts, normalize_embeddings=True):
            return [[1., 0.] for _ in texts]
    assert memory.search("semantically unrelated wording", embedder=Embedder())[0]["id"] == fact["id"]


def test_private_federation_accounts_budget_and_imports_as_review_only(tmp_path):
    root = str(tmp_path)
    skill = {"data": {"beta_alpha": 8, "beta_beta": 3, "steps": [{"tool": "internal"}]}}
    federation.configure_privacy(root, epsilon=1)
    payload = federation.private_prepare(root, skill, public_tools={"internal": "compute"}, epsilon=.6)
    assert "case_count" not in payload and "reliability_mean" not in payload
    with pytest.raises(PermissionError):
        federation.private_prepare(root, skill, public_tools={"internal": "compute"}, epsilon=.6)
    principal = origin.Principal("exporter", "organization", "federation-export", b"k" * 32)
    approval = origin.bind({"export_digest": payload["export_digest"]}, principal)
    received = federation.receive(root, payload, approval=approval, principals={"exporter": principal})
    assert received["data"]["status"] == "review" and received["data"]["proof_count"] == 0
    assert federation.receive(root, payload, approval=approval, principals={"exporter": principal}) == received


def test_offline_anticipation_requires_review_and_rechecks_source_liveness(tmp_path):
    root = str(tmp_path)
    fact = MemoryClient(root).add("Release window is scheduled")["facts"][0]
    MemoryClient(root).reflect("Release window", occasion_id="source-request")
    result = memory_control.offline_pass(root)
    assert result["foresight_proposals"] == 1
    note = memory_control.records(root, "foresight")[0]
    assert all(r["id"] != note["id"] for r in MemoryClient(root).reflect("Release")["evidence"])
    active = memory_control.review_foresight(root, note["id"], note["revision"], actor="reviewer", approve=True)
    assert any(r["id"] == active["id"] for r in MemoryClient(root).reflect("Release")["evidence"])
    hierarchical.forget_fact(root, fact["id"])
    assert not MemoryClient(root).reflect("Release")["context"]


def test_prescribed_property_schemas_validate_actual_graph_writes(tmp_path):
    from commontrace import graph, ontology, ontology_learning

    ontology_learning.prescribe(str(tmp_path), {"$defs": {"Device": {"type": "object",
        "properties": {"serial": {"type": "integer"}}, "required": ["serial"], "additionalProperties": False}}})
    with pytest.raises(ontology.OntologyError):
        graph.add_node(str(tmp_path), "device:one", "device", properties={"serial": "invalid"})
    assert not graph.load_nodes(str(tmp_path))
    graph.add_node(str(tmp_path), "device:one", "device", properties={"serial": 42})
    assert graph.load_nodes(str(tmp_path))["device:one"].properties == {"serial": 42}


def test_public_enrollment_cannot_read_owner_global_memory(tmp_path):
    root = str(tmp_path)
    MemoryClient(root).add("Private Orion acquisition target")
    trace_io.write_new(root, title="Orion", context="Private Orion", solution="Acquisition", tags=[])
    memory_control.directive(root, "Global actions require review", deny_tools=["deploy"])
    gw = Gateway(root, token="owner", allow_self_signup=True)
    headers = {"Host": "localhost"}
    enrolled = json.loads(gw.handle("POST", "/v1/agent/enroll", headers, b"{}").body)
    headers["Authorization"] = "Bearer " + enrolled["token"]
    for operation in ("reflect", "profile", "search"):
        response = gw.handle("POST", "/v1/memory/" + operation, headers, b'{"query":"Orion"}')
        assert response.status == 200
        assert b"Orion" not in response.body
    assert gw.handle("POST", "/v1/memory/add", headers, b'{"text":"Own Orion note"}').status == 200
    response = gw.handle("POST", "/v1/memory/search", headers, b'{"query":"Orion"}')
    assert b"Own Orion" in response.body and b"Private Orion" not in response.body
    assert gw.handle("POST", "/v1/memory/check-action", headers, b'{"tool":"deploy"}').status == 403


def test_skill_cannot_change_original_author_or_source_outcome(tmp_path):
    root = str(tmp_path)
    filename = trace_io.write_new(root, title="release", context="release", solution="done", tags=[],
                                  outcome={"resolved": False})
    tid = trace_io.read(filename)[0]["id"]
    options = dict(steps=[{"tool": "review"}], applies_when="release", do_not_apply_when="other")
    with pytest.raises(ValueError, match="outcomes"):
        experience_skills.propose(root, "release", cases=[{"trace_id": tid, "succeeded": True}], **options)
    row = experience_skills.propose(root, "release", cases=[{"trace_id": tid, "succeeded": False}], **options)
    evidence = {"raw_effect": .1, "abstract_effect": .2, "ci_low": .02, "effective_samples": 100,
                "experiment_id": "paired", "raw_comparison": True}
    row = experience_skills.review(root, row["id"], row["revision"], actor="reviewer", verdict="HELPS",
                                   evidence=evidence)
    with pytest.raises(PermissionError):
        experience_skills.review(root, row["id"], row["revision"], actor="local", verdict="HELPS", evidence=evidence)


def test_hook_occasion_outcomes_are_isolated_between_agents(tmp_path):
    root = str(tmp_path)
    for agent, succeeded in (("a", True), ("b", False)):
        result = agent_hooks.handle(root, "start", {"occasion_id": "session-1", "query": "release"}, agent_id=agent)
        assert result["occasion_id"] == agent + ":session-1"
        result = agent_hooks.handle(root, "end", {"occasion_id": "session-1", "context": "release",
            "solution": "done", "succeeded": succeeded}, agent_id=agent)
        assert result["outcome_recorded"]


def test_skill_gate_rejects_contradictory_bounds():
    result = experience_skills.causal_gate(raw_effect=.9, abstract_effect=-.9, ci_low=.1,
                                          independent_review=True, effective_samples=100)
    assert result["admit"] is False


def test_gateway_outcome_accepts_every_valid_returned_occasion(tmp_path):
    root = str(tmp_path)
    agent = agent_registry.signup(root, "agent-with-prefix")
    gw = Gateway(root, token="owner")
    headers = {"Host": "localhost", "Authorization": "Bearer " + agent["token"]}
    for operation in ("reflect", "profile"):
        result = gw.handle("POST", "/v1/memory/" + operation, headers,
                           json.dumps({"query": "release", "occasion_id": "x" * 200}).encode())
        assert result.status == 200
        occasion = json.loads(result.body)["occasion_id"]
        response = gw.handle("POST", "/v1/memory/outcome", headers,
                             json.dumps({"occasion_id": occasion, "succeeded": True}).encode())
        assert response.status == 200
    for invalid in ("x" * 256, 1, {}, "bad\ncontrol"):
        response = gw.handle("POST", "/v1/memory/reflect", headers,
                             json.dumps({"occasion_id": invalid}).encode())
        assert response.status == 400


@pytest.mark.parametrize("change", ["outcome", "expires", "procedure"])
def test_signed_trace_metadata_tampering_cannot_feed_skills_or_recall(tmp_path, change):
    from commontrace import frontmatter

    root = str(tmp_path)
    filename = trace_io.write_new(root, title="release", context="release context", solution="reviewed", tags=[],
        outcome={"resolved": False}, extra={"expires": "2099-01-01T00:00:00Z"})
    fm, body = frontmatter.read(filename)
    if change == "outcome":
        fm["outcome"]["resolved"] = True
    elif change == "expires":
        del fm["expires"]
    else:
        fm["extensions"]["profile"]["procedure"] = {"steps": [{"tool": "deploy"}]}
    frontmatter.write(filename, fm, body)
    trace = trace_io.read(filename)[0]
    assert not memory_authority.verify(root, trace["extensions"]["profile"]["origin"],
                                       memory_authority.trace_record(trace))
    assert not MemoryClient(root).reflect("release")["evidence"]
    with pytest.raises(ValueError):
        experience_skills.propose(root, "forged", steps=[{"tool": "review"}],
            cases=[{"trace_id": trace["id"], "succeeded": trace["outcome"]["resolved"]}],
            applies_when="release", do_not_apply_when="other")


def test_trace_outcome_credentials_are_redacted_before_origin_binding(tmp_path):
    secret = "review_dummy_secret_credential_123456789"
    filename = trace_io.write_new(str(tmp_path), title="review", context="context", solution="solution", tags=[],
                                  outcome={"resolved": False, "api_key": secret})
    assert secret not in open(filename).read()
    assert secret not in (tmp_path / "memory" / "origins.jsonl").read_text()


@pytest.mark.parametrize("field,value", [("expires", "2000-01-01T00:00:00Z"),
    ("expires_at", "2000-01-01T00:00:00Z"), ("valid_until", "2000-01-01T00:00:00Z"),
    ("valid_from", "2099-01-01T00:00:00Z")])
def test_trace_evidence_respects_all_expiry_and_validity_fields(tmp_path, field, value):
    root = str(tmp_path)
    filename = trace_io.write_new(root, title="release", context="release", solution="reviewed", tags=[],
                                  outcome={"resolved": True}, extra={field: value})
    assert not MemoryClient(root).reflect("release")["evidence"]
    with pytest.raises(ValueError, match="ineligible"):
        experience_skills.propose(root, "expired", steps=[{"tool": "review"}],
            cases=[{"trace_id": trace_io.read(filename)[0]["id"], "succeeded": True}],
            applies_when="release", do_not_apply_when="other")


def test_frozen_installer_hooks_use_the_native_cli(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    root = str(tmp_path)
    onboarding.install(root, "native")
    manifest = json.loads((tmp_path / "distribution" / "agents" / "native" / "plugin.json").read_text())
    assert " evolve hook start " in manifest["hooks"]["session_start"]["command"]
    assert " -m " not in manifest["hooks"]["session_start"]["command"]
