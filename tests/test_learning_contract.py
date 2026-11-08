"""Cross-backend learning semantics, using the production statistical engines."""

import copy

import pytest

from unimem import learning_contract as c


def config(mode="adaptive", **options):
    return {**c.validate_config({"mode": mode, **options}), "experiment_id": "e", "revision": 1, "salt": "s"}


def evidence(mode="help", count=400, **options):
    cfg = config(**options)
    rows = []
    for index in range(count):
        occasion = str(index)
        row = c.make_assignment(cfg, occasion, [{"id": "m", "revision": 1, "text": "memory"}])
        row["policy_revision"] = 0
        row["delivery"] = row["inject"][:]
        succeeded = bool(row["inject"]) if mode == "help" else not bool(row["inject"])
        if mode == "neutral":
            succeeded = index % 2 == 0
        row["outcome"] = c.normalize_outcome(cfg, occasion, {"occasion_id": occasion, "succeeded": succeeded})
        rows.append(row)
    return cfg, rows


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"scope": {}},
        {"memory_ids": ["m"]},
        {"occasion_id": "o", "memory_ids": ["m", "m"]},
        {"occasion_id": "o", "memory_ids": ["m"], "task_context": " x "},
    ],
)
def test_request_rejects_malformed_and_scope_override(body):
    with pytest.raises(ValueError):
        c.validate_request("assign", body)


@pytest.mark.parametrize("reward", [True, False, float("nan"), float("inf"), -0.1, 1.1, "0.5"])
def test_reward_is_finite_fraction(reward):
    with pytest.raises(ValueError):
        c.normalize_outcome(config(), "o", {"occasion_id": "o", "reward": reward})


def test_utility_is_frozen_and_rounding_is_shared():
    cfg = config(utility={"terms": {"latency": {"low": 0, "high": 100, "better": "lower"}}})
    outcome = c.normalize_outcome(cfg, "o", {"occasion_id": "o", "metrics": {"latency": 25}})
    assert outcome["reward"] == 0.75
    assert outcome["succeeded"] == c.rewards.realize(cfg["salt"], "o", 0.75)
    assert outcome["utility_digest"] == cfg["utility_digest"]
    with pytest.raises(ValueError, match="missing"):
        c.normalize_outcome(cfg, "o", {"occasion_id": "o", "metrics": {}})


@pytest.mark.parametrize("mode,verdict", [("help", "HELPS"), ("harm", "HURTS")])
def test_real_anytime_evaluation_detects_large_effects(mode, verdict):
    cfg, rows = evidence(mode)
    before = copy.deepcopy(rows)
    report = c.evaluate(cfg, rows)
    assert report["blocked"] == []
    assert report["decisions"][0]["verdict"] == verdict
    assert report["decisions"][0]["disabled"] == (verdict == "HURTS")
    assert rows == before


def test_neutral_evidence_cannot_manufacture_a_winner():
    cfg, rows = evidence("neutral")
    assert c.evaluate(cfg, rows)["decisions"][0]["verdict"] not in ("HELPS", "HURTS")


def test_partial_delivery_and_missing_outcomes_block_promotion():
    cfg, rows = evidence()
    injected = next(row for row in rows if row["inject"])
    injected["delivery"] = []
    assert "incomplete_delivery" in c.evaluate(cfg, rows)["blocked"]
    assert c.evaluate(cfg, rows)["decisions"] == []
    injected["delivery"] = injected["inject"][:]
    rows[0]["outcome"] = None
    assert "pending_outcomes" in c.evaluate(cfg, rows)["blocked"]


def test_off_and_protected_sources_are_not_randomized():
    for cfg, source in [
        (config("off"), {"id": "m", "revision": 1}),
        (config(), {"id": "m", "revision": 1, "protected": True}),
    ]:
        row = c.make_assignment(cfg, "o", [source])
        assert row["inject"] == ["m"]
        assert row["arms"][0]["rate"] == 0
        assert c.evaluate(cfg, [row])["decisions"] == []


def test_source_revision_and_context_are_separate_hypotheses():
    assert len({c.policy_key("m", 1, "robot"), c.policy_key("m", 2, "robot"), c.policy_key("m", 1, "agent")}) == 3
    cfg, rows = evidence()
    rows[0]["utility_digest"] = "new-utility"
    with pytest.raises(c.LearningConflict):
        c.evaluate(cfg, rows)


def test_all_hypotheses_use_one_multiplicity_corrected_analysis(monkeypatch):
    cfg, rows = evidence(count=40)
    other = copy.deepcopy(rows)
    for row in other:
        row["task_context"] = "robot"
    real = c.experiment.analyze
    families = []

    def analyze(observations, **kwargs):
        families.append({o.lesson_slug for o in observations})
        return real(observations, **kwargs)

    monkeypatch.setattr(c.experiment, "analyze", analyze)
    c.evaluate(cfg, rows + other)
    assert len(families) == 1
    assert len(families[0]) == 2


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("assign", {"occasion_id": "o", "memory_ids": []}),
        ("assign", {"occasion_id": "o", "memory_ids": ["m", "m"]}),
        ("assign", {"occasion_id": "o", "memory_ids": ["m"], "task_context": "a" * 65}),
        ("assign", {"occasion_id": "o", "memory_ids": ["m" * 257]}),
        ("configure", {"mode": "observe", "min_arm": 10001}),
        ("outcome", {"occasion_id": "o"}),
        ("outcome", {"occasion_id": "o", "succeeded": True, "reward": 1}),
        ("outcome", {"occasion_id": "o", "reward": 0.5, "metrics": {}}),
    ],
)
def test_openapi_discovery_schema_rejects_runtime_invalid_wire_shapes(operation, payload):
    from openapi_schema_validator import OAS30Validator

    assert list(OAS30Validator(c.request_schema(operation)).iter_errors(payload))
    with pytest.raises(ValueError):
        c.validate_request(operation, payload)


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("delivery", {"occasion_id": "o", "memory_ids": []}),
        ("assign", {"occasion_id": "o", "memory_ids": ["m"]}),
        ("configure", {"mode": "observe", "utility": None}),
        ("outcome", {"occasion_id": "o", "succeeded": True}),
        ("outcome", {"occasion_id": "o", "reward": 0.5}),
        ("outcome", {"occasion_id": "o", "metrics": {"success": 1}}),
    ],
)
def test_openapi_discovery_schema_accepts_runtime_valid_wire_shapes(operation, payload):
    from openapi_schema_validator import OAS30Validator

    OAS30Validator(c.request_schema(operation)).validate(payload)
    c.validate_request(operation, payload)
