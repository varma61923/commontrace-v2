"""Behavioral contracts for the integrated memory evolution path."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from benchmarks.evolution import LocalAdapter, NoMemoryAdapter, fixture, run, stable_metrics
from commontrace import (
    additive_extract,
    agent_registry,
    causal_policy,
    decay,
    experience_skills,
    federation,
    frontmatter,
    graph,
    hierarchical,
    holdout_io,
    ingestion_contract,
    jobs,
    llm,
    memfs,
    memory_control,
    observations,
    onboarding,
    ontology,
    origin,
    trace_io,
    ttl,
)
from commontrace.cli import main
from commontrace.client import MemoryClient, wrap
from commontrace.gateway import Gateway
from commontrace.llm_runtime import Budget, BudgetExceeded, LLMRuntime
from commontrace.local_extraction import gliner_entities
from commontrace.search_recipes import REGISTRY, search


@pytest.fixture(autouse=True)
def no_default_holdout(tmp_path):
    holdout_io.configure(str(tmp_path), rate=0)


def add(root, text, **options):
    return hierarchical.append_facts(str(root), [{"statement": text, **options}])[0][0]


def request(gateway, path, body, token="owner"):
    response = gateway.handle("POST", path, {"Authorization": "Bearer " + token, "Host": "localhost"},
                              json.dumps(body).encode())
    return response.status, json.loads(response.body)


def test_add_only_replay_preserves_all_metadata(tmp_path):
    fact = add(tmp_path, "Meridian deploy requires approval", stability="stable")
    before = hierarchical.load_facts(str(tmp_path))[fact.id].to_dict()
    result = hierarchical.append_facts(str(tmp_path), [{"statement": fact.statement, "stability": "dynamic"}])
    assert result[0][1] == "NOOP"
    assert hierarchical.load_facts(str(tmp_path))[fact.id].to_dict() == before
    add(tmp_path, "Meridian deploy does not require approval")
    assert len(hierarchical.list_facts(str(tmp_path))) == 2


def test_invalid_append_batch_has_no_partial_write(tmp_path):
    with pytest.raises(ValueError):
        hierarchical.append_facts(str(tmp_path), [{"statement": "valid fact"}, {"statement": "x" * 2001}])
    assert not hierarchical.load_facts(str(tmp_path))


def test_single_pass_model_cannot_update_existing_records(tmp_path):
    add(tmp_path, "release uses gatekeeper")
    calls = []
    def complete(prompt):
        calls.append(prompt)
        return json.dumps({"facts": [{"statement": "release uses gatekeeper"},
                                    {"statement": "release now uses another approver"}]}), {}
    result = additive_extract.extract(str(tmp_path), "input", complete=complete)
    assert len(calls) == result["calls"] == 1
    assert [f["action"] for f in result["facts"]] == ["NOOP", "ADD"]
    with pytest.raises(ValueError, match="unsupported"):
        additive_extract.extract(str(tmp_path), "input", complete=lambda _: (
            '{"facts":[{"statement":"x", "operation":"DELETE"}]}', {}))


def test_recipe_filters_before_sparse_statistics_and_supports_dense(tmp_path):
    first = add(tmp_path, "gatekeeper release approval", scopes=["agent:a"])
    other = add(tmp_path, "private gatekeeper release", scopes=["agent:b"])
    rows = search(str(tmp_path), "release", context=["agent:a"], dense_scores={first.id: 0.8, other.id: 1})
    assert [r["id"] for r in rows] == [first.id]
    assert rows[0]["signals"]["dense"] == 0.8
    with pytest.raises(ValueError):
        search(str(tmp_path), "release", recipe="unknown")
    with pytest.raises(ValueError):
        search(str(tmp_path), "release", recipe="nearby")


def test_graph_recipes_and_feedback_are_replay_safe(tmp_path):
    root = str(tmp_path)
    first = add(root, "meridian release gatekeeper")
    second = add(root, "meridian release requirements")
    graph.add_node(root, "project:meridian", "concept")
    graph.add_edge(root, "project:meridian", first.id, "mentions")
    rows = search(root, "meridian", recipe="nearby", center="project:meridian")
    assert rows[0]["id"] == first.id
    weighted = graph.feedback_edge(root, "project:meridian", first.id, "mentions", event_id="a", helpful=False)
    assert weighted.weight == pytest.approx(1 / 3, abs=0.001)
    replay = graph.feedback_edge(root, "project:meridian", first.id, "mentions", event_id="a", helpful=False)
    assert replay.weight == weighted.weight
    assert len(search(root, "meridian", recipe="diverse")) == 2
    assert REGISTRY.retrieve("nl-query", root, "meridian category:general", limit=1)[0]["id"] in {first.id, second.id}


def test_profile_separates_stable_dynamic_and_hides_expired(tmp_path):
    now = datetime.now(timezone.utc)
    add(tmp_path, "user likes tea", stability="stable")
    add(tmp_path, "release is tomorrow", stability="dynamic")
    add(tmp_path, "temporary release slot", expires_at=(now - timedelta(hours=1)).isoformat())
    result = memory_control.profile(str(tmp_path), "release")
    assert len(result["static"]) == len(result["dynamic"]) == 1
    assert "temporary" not in json.dumps(result)


def test_temporary_assertions_receive_type_expiry_and_stable_assertions_do_not(tmp_path):
    root = str(tmp_path)
    memory = MemoryClient(root)
    temporary = memory.add("temporary release window", memory_type="temporary")["facts"][0]
    assert datetime.fromisoformat(temporary["expires_at"]) > datetime.now(timezone.utc)
    assert memory.add("stable release policy")["facts"][0]["expires_at"] is None
    assert ttl.expiry_for_type("temporary", valid_from="2025-01-01T00:00:00Z") == "2025-01-02T00:00:00+00:00"


def test_default_temporal_apis_exclude_future_and_ended_assertions(tmp_path):
    root = str(tmp_path)
    add(root, "release future requirement", valid_from="2099-01-01T00:00:00Z")
    add(root, "release old requirement", valid_from="2020-01-01T00:00:00Z", valid_until="2020-02-01T00:00:00Z")
    assert search(root, "release") == []
    assert memory_control.profile(root, "release")["dynamic"] == []
    assert memory_control.reflect(root, "release")["context"] == ""
    assert len(search(root, "release", as_of="2020-01-15")) == 1


def test_directives_are_enforced_and_cannot_silently_overflow_budget(tmp_path):
    root = str(tmp_path)
    with pytest.raises(ValueError, match="lists"):
        memory_control.directive(root, "Avoid writes", deny_tools="deploy")
    memory_control.directive(root, "Avoid production writes", deny_tools=["deploy"], required_tags=["reviewed"])
    with pytest.raises(PermissionError):
        memory_control.check_action(root, "deploy", tags=["reviewed"])
    with pytest.raises(PermissionError):
        memory_control.check_action(root, "read")
    memory_control.check_action(root, "read", tags=["reviewed"])
    with pytest.raises(ValueError, match="mandatory directives"):
        memory_control.reflect(root, "x", budget=1)


def test_reflect_does_not_launder_forgotten_facts_through_observations(tmp_path):
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Meridian gatekeeper approval")
    hierarchical.add_fact(root, fact.statement)
    observations.consolidate_facts(root)
    assert memory_control.reflect(root, "gatekeeper")["context"]
    hierarchical.forget_fact(root, fact.id)
    assert not memory_control.reflect(root, "gatekeeper")["context"]


def test_active_lesson_requires_current_admission_and_validity(tmp_path):
    root = str(tmp_path)
    filename = tmp_path / "memory" / "lessons" / "lesson_unapproved.md"
    frontmatter.write(str(filename), {"name": "unapproved", "status": "active", "valid_until": "2020-01-01"},
                      "Gatekeeper release advice")
    assert not memory_control.reflect(root, "gatekeeper")["context"]


def test_reflect_honors_holdout_and_sdk_records_outcome(tmp_path):
    root = str(tmp_path)
    fact = add(root, "release gatekeeper approval")
    from commontrace import experiment

    holdout_io.configure(root, rate=0.5, salt="test")
    occasion = next("occasion-" + str(i) for i in range(100)
                    if experiment.is_held_out(fact.id, "occasion-" + str(i), 0.5, "test"))
    memory = MemoryClient(root)
    result = memory.reflect("release", occasion_id=occasion)
    assert result["withheld"] == [fact.id] and result["context"] == ""
    assert memory.outcome(result["occasion_id"], True)


def test_refresh_models_uses_durable_queue_and_revision_history(tmp_path):
    root = str(tmp_path)
    add(root, "release gatekeeper approval")
    model = memory_control.standing_question(root, "What approval does release require?")
    result = jobs.run_pending(root, kinds=["mental-model"])
    assert result["done"] == 1
    latest = memory_control.records(root, "mental-model")[0]
    assert latest["revision"] != model["revision"]
    assert "gatekeeper" in latest["data"]["answer"]["context"]
    assert len(list((tmp_path / "memory" / "controls" / "mental-model").glob("*.md"))) == 2


def test_session_watermark_does_not_advance_on_failed_or_invalid_model_call(tmp_path):
    root = str(tmp_path)
    entries = [{"sequence": 1, "text": "release failed because review was skipped"}]
    with pytest.raises(RuntimeError):
        memory_control.distill_session(root, "session-a", entries,
                                      lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert not memory_control.records(root, "watermark")
    with pytest.raises(ValueError):
        memory_control.distill_session(root, "session-a", entries, lambda _: ('{"lessons":42}', {}))
    result = memory_control.distill_session(root, "session-a", entries,
                                          lambda _: ('{"lessons":["Require review before release"]}', {}))
    assert result["sealed_through"] == 1
    replay = memory_control.distill_session(root, "session-a", entries, lambda _: pytest.fail("replay called model"))
    assert not replay["proposals"]


def test_exploration_inclusion_probability_and_delayed_outcomes(tmp_path):
    root = str(tmp_path)
    assignments = causal_policy.explore(["a", "b", "c", "d"], ["a"], slots=2, seed=3)
    assert len(assignments) == 2 and all(a["selection_probability"] == 2 / 3 for a in assignments)
    causal_policy.record(root, "one", assignments)
    causal_policy.record(root, "one", assignments, outcome=0.8)
    assert all(r["outcome"] == 0.8 for r in causal_policy.read(root))
    with pytest.raises(ValueError):
        causal_policy.record(root, "one", assignments, outcome=0.2)
    rows = [{"delivered": True, "outcome": 1, "selection_probability": 0.5, "treatment_probability": 0.5},
            {"delivered": False, "outcome": 0, "selection_probability": 0.5, "treatment_probability": 0.5}]
    assert causal_policy.snipw(rows)["effect"] == 1
    assert not causal_policy.snipw([{**rows[0], "outcome": None}])["identified"]


def test_origin_corroboration_requires_same_claim_and_operator_bound_authority():
    principals = {name: origin.Principal(name, "org-" + name, "writer", name.encode() * 32) for name in ("a", "b")}
    a = origin.bind({"fact": "rain"}, principals["a"])
    b = origin.bind({"fact": "sun"}, principals["b"])
    assert origin.corroboration([a, b], principals, digest=a["digest"]) == 1
    assert not origin.verify({**a, "authority": "administrator"}, principals)
    assert not origin.verify({**a, "record": {"fact": "sun"}}, principals)


def test_runtime_routes_and_stops_before_next_call(tmp_path):
    route = llm.Config("ollama", "local-model", "")
    runtime = LLMRuntime(str(tmp_path), "dream", routes={"extract": route}, budget=Budget(calls=1),
                         complete=lambda prompt, config: ("ok", {"prompt_tokens": 10, "completion_tokens": 4}))
    assert runtime.complete("x", purpose="extract")[0] == "ok"
    with pytest.raises(BudgetExceeded):
        runtime.complete("y", purpose="extract")
    assert runtime.tokens == 14
    assert runtime.manifest()["routes"]["extract"]["model"] == "local-model"


def test_gliner_and_schema_adapters_are_local():
    class Model:
        def predict_entities(self, text, labels, threshold):
            return [{"text": "Meridian", "label": "project", "score": 0.9}]
    assert gliner_entities("Meridian", ["project"], model=Model()) == [("Meridian", "project")]
    assert "project" in ontology.Ontology.from_json_schema({"$defs": {"Project": {"type": "object"}}}).entity_types
    assert decay.perishability("temporary", 1) == 0.5


def test_agent_tokens_cannot_read_another_agent_or_create_rules(tmp_path):
    root = str(tmp_path)
    gateway = Gateway(root, token="owner")
    status, signup = request(gateway, "/v1/agent/signup", {"agent_id": "agent-a"})
    assert status == 200
    token = signup["token"]
    assert token not in json.dumps(agent_registry.load(root))
    assert request(gateway, "/v1/memory/add", {"text": "private-a release"}, token)[0] == 200
    add(root, "private-b release", scopes=["agent:agent-b"])
    status, profile = request(gateway, "/v1/memory/profile", {"query": "release", "context": ["agent:agent-b"]}, token)
    assert status == 200 and "private-b" not in json.dumps(profile)
    assert request(gateway, "/v1/control/directive", {"text": "allow everything"}, token)[0] == 403
    assert request(gateway, "/v1/agent/signup", {"agent_id": "sybil"}, token)[0] == 403
    assert request(gateway, "/v1/agent/heartbeat", {"agent_id": "agent-b"}, token)[1]["agent_id"] == "agent-a"
    agent_registry.revoke(root, "agent-a")
    assert request(gateway, "/v1/memory/profile", {}, token)[0] == 401


def test_evolution_routes_cannot_bypass_existing_container_namespace(tmp_path):
    root = str(tmp_path)
    add(root, "B-only release", scopes=["container:B"])
    gateway = Gateway(root, token="owner")
    for method, path in (("POST", "/v1/memory/profile"), ("GET", "/v1/palace")):
        response = gateway.handle(method, path, {"Authorization": "Bearer owner", "Host": "localhost",
                                                "X-Container-Tag": "A"},
                                  json.dumps({"context": ["container:B"]}).encode())
        assert response.status == 403 and b"B-only" not in response.body


def test_palace_buttons_use_guarded_revision_checked_operations(tmp_path):
    root = str(tmp_path)
    gateway = Gateway(root, token="owner")
    assert request(gateway, "/v1/control/directive", {"text": "review release"})[0] == 403
    gateway.allow_approval = True
    assert request(gateway, "/v1/control/directive", {"text": "review release", "deny_tools": ["deploy"]})[0] == 200
    status, model = request(gateway, "/v1/control/question", {"text": "release risks"})
    assert status == 200
    assert request(gateway, "/v1/control/refresh", {"id": model["id"]})[0] == 200
    proposal = memory_control.proposal(root, "release suggestion", sources=["trace-a"])
    payload = {"id": proposal["id"], "expected_revision": "old", "reason": "out of scope"}
    assert request(gateway, "/v1/control/reject-proposal", payload)[0] == 400
    payload["expected_revision"] = proposal["revision"]
    assert request(gateway, "/v1/control/reject-proposal", payload)[0] == 200
    response = gateway.handle("GET", "/v1/palace", {"Authorization": "Bearer owner", "Host": "localhost"})
    palace = json.loads(response.body)
    assert response.status == 200 and palace["directives"] and palace["models"] and not palace["suggestions"]


def test_batch_timestamp_alias_and_limit_guards(tmp_path):
    root = str(tmp_path)
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "ontology.json").write_text(json.dumps({"aliases": {"service:postgres": ["pg"]}}))
    result = ingestion_contract.batch(root, [{"statement": "pg powers releases", "valid_from": "2025-01-01T00:00:00Z"}])
    assert result["facts"][0]["statement"] == "postgres powers releases"
    with pytest.raises(ValueError, match="timezone"):
        ingestion_contract.batch(root, [{"statement": "x", "valid_from": "2025-01-01"}])
    with pytest.raises(ValueError):
        ingestion_contract.batch(root, [{"statement": "x"}] * 201)


def test_wrapper_captures_raw_experience_without_fabricated_outcome(tmp_path):
    completion = wrap(lambda messages: {"choices": [{"message": {"content": "Ask gatekeeper"}}]}, root=str(tmp_path))
    response = completion(messages=[{"role": "user", "content": "What should release require?"}])
    assert response["choices"]
    assert not hierarchical.load_facts(str(tmp_path))
    assert len(list((tmp_path / "memory" / "traces").glob("*.md"))) == 1


def test_installer_bootstraps_git_and_isolated_skill(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@example.com",
                    "commit", "--allow-empty", "-qm", "Add release review"], check=True)
    result = onboarding.install(str(tmp_path / "store"), "agent-a", repo=str(repo))
    assert result["traces_imported"] == 1
    assert "MemoryClient" in open(result["skill_path"]).read()


def test_memfs_signed_snapshot_and_shared_attachment_detect_drift(tmp_path, monkeypatch):
    root, shared = str(tmp_path / "store"), str(tmp_path / "shared")
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", "k" * 40)
    holdout_io.configure(root, rate=0)
    add(shared, "gatekeeper release requirement")
    holdout_io.configure(shared, rate=0)
    memfs.init(shared)
    receipt = memfs.sign_snapshot(shared, issuer="owner")
    assert memfs.verify_snapshot(shared, receipt)
    token = memfs.handoff(shared, audience="a")
    memfs.attach(root, shared, agent_id="a", token=token)
    assert memfs.attached_roots(root, agent_id="a") == [shared]
    assert "gatekeeper" in MemoryClient(root, agent_id="a").reflect("release")["context"]
    add(shared, "changed memory")
    with pytest.raises(memfs.MemfsError):
        memfs.attached_roots(root, agent_id="a")


def test_shared_experiments_never_write_into_readonly_attachment(tmp_path, monkeypatch):
    root, shared = str(tmp_path / "client"), str(tmp_path / "shared")
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", "k" * 40)
    add(shared, "gatekeeper shared release")
    lessons = tmp_path / "shared" / "memory" / "lessons"
    lessons.mkdir(parents=True)
    frontmatter.write(str(lessons / "lesson_release.md"), {"name": "lesson_release", "status": "active"},
                      "Release checklist requires gatekeeper approval.")
    holdout_io.configure(shared, rate=0.5)
    holdout_io.configure(root, rate=0.5)
    memfs.init(shared)
    memfs.attach(root, shared, agent_id="a", token=memfs.handoff(shared, audience="a"))
    memory = MemoryClient(root, agent_id="a")
    memory.reflect("release", occasion_id="first")
    memory.reflect("release", occasion_id="second")
    assert memfs.status(shared)["changed"] == []
    assert not (tmp_path / "shared" / "memory" / "holdout_log.jsonl").exists()
    assert not (tmp_path / "shared" / "memory" / ".cache").exists()
    assert (tmp_path / "client" / "memory" / "holdout_log.jsonl").exists()


def test_shared_directives_are_enforced_even_when_context_budget_is_full(tmp_path, monkeypatch):
    root, shared = str(tmp_path / "client"), str(tmp_path / "shared")
    monkeypatch.setenv("COMMONTRACE_HANDOFF_KEY", "k" * 40)
    memory_control.directive(shared, "Deployment is prohibited", deny_tools=["deploy"])
    memfs.init(shared)
    memfs.attach(root, shared, agent_id="a", token=memfs.handoff(shared, audience="a"))
    memory = MemoryClient(root, agent_id="a")
    with pytest.raises(PermissionError):
        memory.check_action("deploy")
    memory.check_action("read")
    with pytest.raises(ValueError, match="mandatory directives"):
        memory.reflect("release", budget=1)


def test_skill_proposal_retains_raw_sources_and_requires_causal_gate(tmp_path):
    root = str(tmp_path)
    trace_io.write_new(root, title="release", context="review required", solution="asked gatekeeper", tags=[], trace_id="t1")
    result = experience_skills.propose(root, "release-review", steps=[{"tool": "review", "inputs": {"branch": "main"}}],
              cases=[{"trace_id": "t1", "succeeded": True}], applies_when="release", do_not_apply_when="read-only")
    assert result["data"]["status"] == "review" and result["data"]["sources"] == ["t1"]
    assert not experience_skills.causal_gate(raw_effect=0.5, abstract_effect=0.8, ci_low=None,
                    independent_review=True, effective_samples=100)["admit"]


def test_federated_export_omits_raw_tenant_data_and_requires_exact_approval():
    skill = {"text": "tenant secret", "scopes": ["user:private"], "data": {
        "steps": [{"tool": "internal-review", "inputs": {"token": "private value"}}],
        "sources": ["tenant-trace"], "beta_alpha": 6, "beta_beta": 2, "reliability_mean": 0.75}}
    tools = {"internal-review": "review"}
    prepared = federation.prepare(skill, public_tools=tools)
    assert "private" not in json.dumps(prepared) and "tenant" not in json.dumps(prepared)
    principal = origin.Principal("reviewer", "org", "federation-export", b"k" * 32)
    approval = origin.bind({"export_digest": prepared["export_digest"]}, principal)
    assert federation.export(skill, public_tools=tools, approval=approval,
                             principals={"reviewer": principal}) == prepared
    with pytest.raises(PermissionError):
        federation.export(skill, public_tools={"internal-review": "deploy"}, approval=approval,
                          principals={"reviewer": principal})


def test_causal_fixture_is_reproducible_and_keeps_cost_latency_with_accuracy():
    reports = [run({"local": LocalAdapter(), "none": NoMemoryAdapter()}, fixture(12)) for _ in range(2)]
    assert stable_metrics(reports[0]) == stable_metrics(reports[1])
    assert reports[0]["synthetic"] and reports[0]["adapters"]["local"]["recall"] == 1
    assert reports[0]["adapters"]["local"]["cost_usd"] == 0
    assert reports[0]["adapters"]["local"]["p95_latency_ms"] >= 0


def test_benchmark_does_not_credit_identifier_prefixes():
    from benchmarks.evolution import Retrieval

    class WrongProject:
        def ingest(self, memories):
            pass
        def retrieve(self, question, budget):
            return Retrieval(["case-10"], "gatekeeper10 approves another project", 12, 0, 0)
    dataset = fixture(2)
    dataset["cases"] = [dataset["cases"][1]] * 10
    metrics = run({"wrong": WrongProject()}, dataset)["adapters"]["wrong"]
    assert metrics["recall"] == metrics["action_success"] == 0


def test_evolve_cli_reaches_connected_operations(tmp_path, capsys):
    assert main(["evolve", "add", "release needs gatekeeper", "--dest", str(tmp_path)]) == 0
    assert main(["evolve", "search", "release", "--dest", str(tmp_path)]) == 0
    assert "gatekeeper" in capsys.readouterr().out
