"""Personalization audits honor actual catalogs, graded metrics and domain scope."""
from __future__ import annotations

import json

import pytest

from unimem import (
    causal_gate,
    cli,
    hierarchical,
    holdout_io,
    memory_authority,
    search_recipes,
)
from unimem import frontier_protocol as fp
from unimem import nextwave_store as store
from unimem import sycophancy_io as io
from tests import test_skill_foundry

clock = test_skill_foundry.clock
CONTEXT = ["domain:science"]


def prepare(root, clock, *, statement="The user believes crystals improve computer performance.", horizon=400):
    holdout_io.configure(root, rate=.5, salt="personalization")
    source = hierarchical.append_facts(root, [{"statement": statement, "memory_type": "personal-opinion",
                                               "scopes": CONTEXT}])[0][0]
    registered = io.register(root, [source.id], domain="science", context=CONTEXT,
                             horizon=horizon, equivalence_margin=.3, queries=["q0", "q1"])
    clock.tick()
    return source, registered["id"]


def traffic(root, identity, clock, *, n=400, helpful=False, prefix="personal"):
    for i in range(n):
        occasion = prefix+str(i)
        actual = io.expose(root, identity, occasion, query_id="q"+str(i%2), context=CONTEXT)
        arm = actual["design"]["arm"]["injected"]
        assert bool(actual["memories"]) is arm
        holdout_io.record_outcome(root, occasion, arm if helpful else i%3 != 0,
            secondary_metrics={"accuracy": arm if helpful else i%3 != 1, "agreement": arm})
        clock.tick(1)


def test_flattering_audit_changes_actual_gate_and_factual_admission(tmp_path, clock):
    root = str(tmp_path)
    source, identity = prepare(root, clock)
    assert not memory_authority.permits(root, source, action_class="factual:science")
    traffic(root, identity, clock)
    report = io.report(root, identity, context=CONTEXT, apply=True)
    assert report["decision_ready"] and report["flattering"]
    assert not report["accuracy_nonnegative"]
    assert not memory_authority.permits(root, source, action_class="factual:science")
    rates, _, _ = causal_gate.plan_rates(root, .1)
    assert rates[source.id] == .9
    assert source.id not in search_recipes.admit(root, [source.id], context=["domain:music"])
    from unimem import memory_control

    assert not memory_control.matches(["project:a", "domain:science"], ["project:a", "domain:music"])
    assert source.id not in search_recipes.admit(root, [source.id], context=CONTEXT, action_class="factual:science")
    assert source.id in search_recipes.admit(root, [source.id], context=CONTEXT)
    first = fp.rows(holdout_io.outcomes_log_path(root))[0]
    assert set(first) == {"occasion_id", "succeeded", "at"}  # Existing outcome format is unchanged.


def test_helpful_personal_memory_clears_accuracy_and_preserves_domain(tmp_path, clock):
    root = str(tmp_path)
    source, identity = prepare(root, clock, statement="Verify factual units before scientific calculations.")
    traffic(root, identity, clock, helpful=True)
    result = io.report(root, identity, context=CONTEXT, apply=True)
    assert not result["flattering"] and result["accuracy_nonnegative"]
    assert memory_authority.permits(root, source, action_class="factual:science")
    assert not memory_authority.permits(root, source, action_class="factual:music")
    with pytest.raises(PermissionError):
        io.report(root, identity)


def test_secondary_labels_cannot_follow_outcome_or_change_on_retry(tmp_path, clock):
    root = str(tmp_path)
    _, identity = prepare(root, clock, horizon=32)
    io.expose(root, identity, "late", query_id="q0", context=CONTEXT)
    holdout_io.record_outcome(root, "late", True)
    with pytest.raises(ValueError, match="precede"):
        holdout_io.record_outcome(root, "late", True, secondary_metrics={"accuracy": True, "agreement": True})
    io.expose(root, identity, "first", query_id="q1", context=CONTEXT)
    io.record_metrics(root, "first", {"accuracy": True, "agreement": False})
    holdout_io.record_outcome(root, "first", True, secondary_metrics={"accuracy": True, "agreement": False})
    with pytest.raises(ValueError, match="immutable"):
        io.record_metrics(root, "first", {"accuracy": False, "agreement": False})
    with pytest.raises(ValueError, match="unavailable"):
        io.report(root, identity, context=CONTEXT)


def test_delivery_retry_and_horizon_are_immutable_and_underpowered_is_not_applied(tmp_path, clock):
    root = str(tmp_path)
    _, identity = prepare(root, clock, horizon=32)
    actual = io.expose(root, identity, "first", query_id="q0", context=CONTEXT)
    assert actual == io.expose(root, identity, "first", query_id="q0", context=CONTEXT)
    with pytest.raises(PermissionError, match="retry"):
        io.expose(root, identity, "first", query_id="q1", context=CONTEXT)
    holdout_io.record_outcome(root, "first", True, secondary_metrics={"accuracy": True, "agreement": True})
    assert not io.report(root, identity, context=CONTEXT)["decision_ready"]
    with pytest.raises(PermissionError, match="incomplete"):
        io.report(root, identity, context=CONTEXT, apply=True)
    assert not store.latest(root, io.DECISIONS)


def test_changed_source_and_missing_domain_fail_closed(tmp_path, clock):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="personalization")
    source = hierarchical.append_facts(root, [{"statement": "Inspect measurements."}])[0][0]
    with pytest.raises(PermissionError, match="domain"):
        io.register(root, [source.id], domain="science", horizon=32)
    with pytest.raises(ValueError, match="domain"):
        hierarchical.append_facts(root, [{"statement": "A declared opinion.", "memory_type": "personal-opinion"}])
    source, identity = prepare(root, clock, horizon=32)
    hierarchical.update_fact(root, source.id, statement="Changed scientific preference.")
    with pytest.raises(PermissionError, match="source"):
        io.expose(root, identity, "changed", query_id="q0", context=CONTEXT)


def test_api_numeric_schema_rejects_nonfinite_metrics_and_cli_is_real(tmp_path, clock, capsys):
    from unimem import api_schema

    source, identity = prepare(str(tmp_path), clock, horizon=32)
    valid = {"occasion_id": "assigned", "succeeded": True, "secondary_metrics": {"accuracy": .5, "agreement": True}}
    api_schema.validate_request("outcome", valid)
    for bad in (float("nan"), float("inf"), 1.01, -1, "0.5"):
        with pytest.raises(ValueError):
            api_schema.validate_request("outcome", {**valid, "secondary_metrics": {"accuracy": bad, "agreement": True}})
    request = json.dumps({"action": "report", "identity": identity, "context": CONTEXT})
    assert cli.main(["experiment", "sycophancy", request, "--dest", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["horizon"] == 32
    assert not memory_authority.permits(str(tmp_path), source, action_class="factual:science")


def test_pinned_benchmark_subsets_keep_gold_separate_and_require_local_license(tmp_path, clock):
    from pathlib import Path

    from unimem import sycophancy

    fixture = json.loads((Path(__file__).parent/"fixtures/personalization_benchmark_contract.json").read_text())
    assert sycophancy.BENCHMARK_PINS["persistbench"] == fixture["persistbench_revision"]
    assert sycophancy.BENCHMARK_PINS["memsyco-bench"] == fixture["memsyco_revision"]
    path = tmp_path/"local-authored-cases.jsonl"
    path.write_text(json.dumps(fixture["memsyco"])+"\n")
    with pytest.raises(PermissionError, match="license"):
        io.benchmark(str(tmp_path), "memsyco-bench", str(path), license_notice="authored fixture")
    case = io.benchmark(str(tmp_path), "memsyco-bench", str(path), license_notice="authored synthetic fixture",
                        license_confirmed=True)["cases"][0]
    assert case["provided_memories"] is None
    assert "GOLD_ANNOTATION_NEVER_MODEL_CONTEXT" not in json.dumps(case["history"])
    assert "GOLD_ANNOTATION_NEVER_MODEL_CONTEXT" in json.dumps(case["grader_reference"])
    persist = sycophancy.benchmark_cases("persistbench", [fixture["persistbench"]])[0]
    assert persist["provided_memories"] == fixture["persistbench"]["memories"]


def test_actual_offline_benchmark_harness_honors_arms_and_hides_gold(tmp_path, clock):
    from pathlib import Path

    from unimem import sycophancy

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="benchmark")
    fixture = json.loads((Path(__file__).parent/"fixtures/personalization_benchmark_contract.json").read_text())
    case = sycophancy.benchmark_cases("memsyco-bench", [fixture["memsyco"]])[0]
    source = hierarchical.append_facts(root, [{"statement": "The user prefers explanations with unit checks.",
                                                "scopes": CONTEXT, "memory_type": "personal-opinion"}])[0][0]
    value = io.register(root, [source.id], domain="science", context=CONTEXT, horizon=32,
        queries=[case["id"]], query_texts={case["id"]: case["query"]})
    clock.tick()
    observed = []

    def runner(query, memories):
        assert "GOLD_ANNOTATION_NEVER_MODEL_CONTEXT" not in query+json.dumps(memories)
        assert query == case["query"]
        observed.append(bool(memories))
        return "One hundred." if memories else "I do not know."

    def grader(reference, response):
        correct = response == reference["evaluation"]["reference_answer"]
        return {"succeeded": correct, "accuracy": correct, "agreement": False}

    audits = {case["id"]: value["id"]}
    result = io.acceptance(root, [case], audits, runner=runner, grader=grader, context=CONTEXT,
                           license_notice="synthetic fixture authored here", license_confirmed=True)
    assert any(observed) and not all(observed)
    assert len(observed) == 32 and result["results"][0]["report"]["n"] == 32
    # A restart does not regrade or change already resolved occasions.
    io.acceptance(root, [case], audits, runner=runner, grader=grader, context=CONTEXT,
                   license_notice="synthetic fixture authored here", license_confirmed=True)
    assert len(observed) == 32


def test_newer_harm_replaces_old_accuracy_and_bundle_result_is_not_individual_evidence(tmp_path, clock):
    from unimem import incident

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="newer-personalization")
    first = hierarchical.append_facts(root, [{"statement": "Prefer unit checks when reviewing scientific claims.",
                                               "memory_type": "personal-opinion", "scopes": CONTEXT}])[0][0]
    second = hierarchical.append_facts(root, [{"statement": "Prefer converting units before comparison.",
                                                "memory_type": "personal-opinion", "scopes": CONTEXT}])[0][0]
    reports = []
    for number, (identities, helpful) in enumerate((([first.id], True), ([first.id], False),
                                                   ([first.id, second.id], True))):
        query = "cohort-"+str(number)
        value = io.register(root, identities, domain="science", context=CONTEXT, horizon=64,
                             minimum_support=1, queries=[query])
        clock.tick()
        for i in range(64):
            occasion = query+":"+str(i)
            arm = io.expose(root, value["id"], occasion, query_id=query, context=CONTEXT)["design"]["arm"]["injected"]
            y = arm if helpful else not arm
            holdout_io.record_outcome(root, occasion, y, secondary_metrics={"accuracy": y, "agreement": arm})
            clock.tick(1)
        reports.append(io.report(root, value["id"], context=CONTEXT, apply=True))
        if number == 0:
            assert memory_authority.permits(root, first, action_class="factual:science")
        else:
            assert not memory_authority.permits(root, first, action_class="factual:science")
    assert reports[-1]["accuracy_nonnegative"]  # Positive for the jointly delivered bundle only.
    assert not memory_authority.permits(root, second, action_class="factual:science")
    incident.freeze(root, "personalization-review", fp.now())
    frozen = incident.pack(root, "personalization-review")["files"]["evidence.json"]["snapshot"]
    assert len(frozen["personalization_audit"]["decisions"]) == 3
    assert incident.verify_pack(root, incident.pack(root, "personalization-review"))


def test_source_erasure_removes_secondary_metric_and_delivery_payloads(tmp_path, clock):
    from unimem import privacy_erasure

    root = str(tmp_path)
    source, identity = prepare(root, clock, horizon=32)
    io.expose(root, identity, "erase", query_id="q0", context=CONTEXT)
    holdout_io.record_outcome(root, "erase", True, secondary_metrics={"accuracy": True, "agreement": False})
    privacy_erasure.purge_fact(root, source.id, reason="owner requested erasure")
    assert store.latest(root, io.SECONDARY)[fp.digest("erase")]["status"] == "erased"
    assert not list((tmp_path/"memory/unimem/secondary-metrics").glob("*.json"))
    with pytest.raises((ValueError, PermissionError)):
        io.report(root, identity, context=CONTEXT)


def test_real_gateway_and_local_sdk_record_labels_before_outcome(tmp_path, clock):
    from unimem import gateway
    from unimem.client import MemoryClient
    from tests.test_gateway import TOKEN, call

    root = str(tmp_path)
    _, identity = prepare(root, clock, horizon=32)
    server = gateway.Gateway(root, token=TOKEN)
    io.expose(root, identity, "http", query_id="q0", context=CONTEXT)
    body = {"occasion_id": "http", "succeeded": True,
            "secondary_metrics": {"accuracy": .5, "agreement": False}}
    status, result = call(server, "POST", "/v1/memory/outcome", body)
    assert status == 200 and result["recorded"]
    labels = store.get(root, io.SECONDARY, fp.digest("http"), context=CONTEXT)
    assert labels["metrics"] == {"accuracy": .5, "agreement": 0.}
    io.expose(root, identity, "native", query_id="q1", context=CONTEXT)
    status, result = call(server, "POST", "/v1/outcome", {**body, "occasion_id": "native"})
    assert status == 200 and result["recorded"]
    io.expose(root, identity, "sdk", query_id="q0", context=CONTEXT)
    client = MemoryClient(root, context={"domain": "science"})
    assert client.outcome("sdk", True, secondary_metrics={"accuracy": True, "agreement": False})
    io.expose(root, identity, "invalid", query_id="q1", context=CONTEXT)
    status, _ = call(server, "POST", "/v1/memory/outcome", {**body, "occasion_id": "invalid",
        "secondary_metrics": {"accuracy": 2, "agreement": True}})
    assert status == 400 and "invalid" not in holdout_io.read_outcomes(root)
