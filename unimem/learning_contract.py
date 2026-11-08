"""Transport independent, versioned measured-learning semantics.

Backends persist these immutable assignments and outcomes transactionally. No model
provider or storage backend is required by this module.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from unimem import experiment, integrity, rewards
from unimem.holdout_io import context_label

SCHEMA = "unimem.learning.v1"
OPERATIONS = ("capabilities", "configure", "assign", "delivery", "outcome", "run", "status", "rollback")


class LearningError(ValueError):
    status_code = 400
    code = "invalid_learning_request"


class LearningConflict(LearningError):
    status_code = 409
    code = "learning_conflict"


class LearningUnavailable(LearningError):
    status_code = 404
    code = "learning_unavailable"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def response(**fields: Any) -> dict:
    return {"schema": SCHEMA, **fields}


def capabilities() -> dict:
    return response(
        operations=list(OPERATIONS),
        modes=["off", "observe", "adaptive"],
        outcome_types=["succeeded", "reward", "metrics"],
        delivery_required=True,
        source_revisions=True,
        rollback=True,
    )


_FIELDS = {
    "capabilities": set(),
    "status": {"experiment_id"},
    "configure": {"mode", "utility", "holdout_rate", "min_arm"},
    "assign": {"occasion_id", "memory_ids", "task_context", "experiment_id"},
    "delivery": {"occasion_id", "memory_ids", "experiment_id"},
    "outcome": {"occasion_id", "succeeded", "reward", "metrics", "experiment_id"},
    "run": {"experiment_id", "limit"},
    "rollback": {"reason", "decision_revision", "experiment_id"},
}


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise LearningError(f"{name} must be a nonempty string of at most 256 characters")
    return value


def validate_request(operation: str, payload: dict | None) -> None:
    if operation not in OPERATIONS:
        raise LearningError("unknown learning operation")
    if not isinstance(payload, dict):
        raise LearningError("learning request must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise LearningError("learning field names must be strings")
    unknown = set(payload) - _FIELDS[operation]
    if unknown:
        raise LearningError("unknown learning fields: " + ", ".join(sorted(unknown)))
    for name in ("occasion_id", "experiment_id"):
        if name in payload:
            _identifier(payload[name], name)
    if operation in ("assign", "delivery", "outcome") and "occasion_id" not in payload:
        raise LearningError("occasion_id is required")
    if operation in ("assign", "delivery"):
        ids = payload.get("memory_ids")
        minimum = 1 if operation == "assign" else 0
        if not isinstance(ids, list) or not minimum <= len(ids) <= 100:
            raise LearningError(f"memory_ids must contain {minimum} to 100 IDs")
        for identity in ids:
            _identifier(identity, "memory_id")
        if len(set(ids)) != len(ids):
            raise LearningError("memory_ids must be unique")
    if "task_context" in payload:
        value = payload["task_context"]
        if not isinstance(value, str) or len(value) > 64 or context_label(value) != value:
            raise LearningError("task_context must be a normalized string of at most 64 characters")
    if operation == "outcome" and sum(k in payload for k in ("succeeded", "reward", "metrics")) != 1:
        raise LearningError("supply exactly one of succeeded, reward, metrics")
    if operation == "run" and (
        type(payload.get("limit", 10000)) is not int or not 1 <= payload.get("limit", 10000) <= 10000
    ):
        raise LearningError("limit must be an integer from 1 to 10000")
    if operation == "rollback":
        _identifier(payload.get("reason"), "reason")
        if "decision_revision" in payload and (
            type(payload["decision_revision"]) is not int or payload["decision_revision"] < 0
        ):
            raise LearningError("decision_revision must be a nonnegative integer")
    if operation == "configure":
        validate_config(payload)


def validate_config(payload: dict) -> dict:
    """Canonical user configuration; backend adds experiment_id/revision/salt."""
    mode = payload.get("mode")
    if mode not in ("off", "observe", "adaptive"):
        raise LearningError("mode must be off, observe, or adaptive")
    rate = payload.get("holdout_rate", 0.5)
    if type(rate) not in (int, float) or not 0.05 <= rate <= 0.95:
        raise LearningError("holdout_rate must be finite and between .05 and .95")
    minimum = payload.get("min_arm", experiment.DEFAULT_MIN_ARM)
    if type(minimum) is not int or not 10 <= minimum <= 10000:
        raise LearningError("min_arm must be an integer from 10 to 10000")
    utility = payload.get("utility")
    if utility is not None:
        utility = rewards._canonical(rewards.parse_utility(utility))
    return {
        "mode": mode,
        "holdout_rate": float(rate),
        "min_arm": minimum,
        "utility": utility,
        "utility_digest": digest(utility),
    }


def policy_key(identity: str, revision: Any, task_context: str) -> str:
    return digest([identity, revision, task_context])


def make_assignment(
    config: dict, occasion_id: str, sources: list[dict], *, task_context: str = "", policy: dict | None = None
) -> dict:
    """Freeze source bodies, revisions and pre-treatment randomized propensities.

    policy maps policy_key(id,revision,context) to {rate,disabled}. Assignment
    includes arms, inject/withhold IDs, sources, delivery=None and outcome=None.
    Backends add at, policy_revision and request_digest before durable storage.
    """
    policy = policy or {}
    arms: list[dict] = []
    inject: list[str] = []
    withhold: list[str] = []
    for source in sources:
        choice = policy.get(policy_key(source["id"], source["revision"], task_context), {})
        rate = choice.get("rate", config["holdout_rate"])
        if source.get("protected") or config["mode"] == "off":
            rate = 0.0
        elif choice.get("disabled"):
            rate = 1.0
        injected = not experiment.is_held_out(source["id"], occasion_id, rate, config["salt"])
        (inject if injected else withhold).append(source["id"])
        arms.append({"id": source["id"], "revision": source["revision"], "injected": injected, "rate": rate})
    return {
        "experiment_id": config["experiment_id"],
        "config_revision": config["revision"],
        "occasion_id": occasion_id,
        "task_context": task_context,
        "utility_digest": config["utility_digest"],
        "sources": sources,
        "arms": arms,
        "inject": inject,
        "withhold": withhold,
        "delivery": None,
        "outcome": None,
    }


def normalize_outcome(config: dict, occasion_id: str, payload: dict) -> dict:
    validate_request("outcome", payload)
    measurement: dict[str, Any]
    if "succeeded" in payload:
        if type(payload["succeeded"]) is not bool:
            raise LearningError("succeeded must be boolean")
        value = float(payload["succeeded"])
        measurement = {"succeeded": payload["succeeded"]}
    elif "reward" in payload:
        value = rewards._fraction(payload["reward"], "reward")
        measurement = {"reward": value}
    else:
        if config.get("utility") is None:
            raise LearningError("metrics require an assignment-time utility")
        terms = rewards.parse_utility(config["utility"])
        value = rewards.score(terms, payload["metrics"])
        measurement = {"metrics": rewards._metrics(payload["metrics"])}
    return {
        "reward": value,
        "succeeded": rewards.realize(config["salt"], occasion_id, value),
        "utility_digest": config["utility_digest"],
        "measurement": measurement,
    }


def evaluate(config: dict, assignments: list[dict]) -> dict:
    """Return {decisions,evidence,blocked}; never mutate assignment evidence.

    Each decision is {key,id,revision,task_context,verdict,rate,disabled,effect,
    ci_low,ci_high,n_injected,n_withheld,integrity}. Full cohort integrity and
    explicit exact delivery are required before any decision can be applied.
    """
    groups: dict[str, list] = defaultdict(list)
    metadata = {}
    blocked = set()
    for assignment in assignments:
        if (
            assignment["experiment_id"] != config["experiment_id"]
            or assignment["utility_digest"] != config["utility_digest"]
        ):
            raise LearningConflict("evidence belongs to a different experiment or utility")
        for arm in assignment["arms"]:
            if not 0 < arm["rate"] < 1:
                continue
            key = policy_key(arm["id"], arm["revision"], assignment["task_context"])
            metadata[key] = arm, assignment["task_context"]
            groups[key].append((assignment, arm))
    decisions = []
    eligible = []
    for key, cohort in sorted(groups.items()):
        rows, observations = [], []
        reasons = set()
        for assignment, arm in cohort:
            outcome = assignment.get("outcome")
            receipt = assignment.get("delivery")
            if receipt is None or sorted(receipt) != sorted(assignment["inject"]):
                reasons.add("incomplete_delivery")
            if outcome is None:
                reasons.add("pending_outcomes")
            rows.append(
                integrity.Assignment(
                    lesson=key,
                    occasion_id=assignment["occasion_id"],
                    injected=arm["injected"],
                    rate=arm["rate"],
                    salt=config["salt"],
                    succeeded=None if outcome is None else outcome["succeeded"],
                    revision=str(arm["revision"]),
                    schedule=str(assignment.get("policy_revision", 0)),
                    context=assignment["task_context"],
                )
            )
            if outcome is not None:
                observations.append(
                    experiment.HoldoutObservation(
                        key,
                        assignment["occasion_id"],
                        arm["injected"],
                        outcome["succeeded"],
                        rate=arm["rate"],
                        scheduled=True,
                    )
                )
        report = integrity.audit(rows, min_arm=config["min_arm"])
        if report.verdict != integrity.VERDICT_SOUND:
            reasons.add("integrity_" + report.verdict.lower())
        if reasons:
            blocked.update(reasons)
            continue
        eligible.extend(observations)
    # Analyze the eligible family together so canonical multiplicity correction
    # also covers independent source/context hypotheses.
    effects = experiment.analyze(eligible, min_arm=config["min_arm"], sequential=True)
    for effect in effects:
        key = effect.lesson_slug
        arm, context = metadata[key]
        decisions.append(
            {
                "key": key,
                "id": arm["id"],
                "revision": arm["revision"],
                "task_context": context,
                **{k: v for k, v in asdict(effect).items() if k != "lesson_slug"},
                "rate": 0.05
                if effect.verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_NO_EFFECT)
                else config["holdout_rate"],
                "disabled": effect.verdict == experiment.VERDICT_HURTS,
                "integrity": integrity.VERDICT_SOUND,
            }
        )
    return {"decisions": decisions, "evidence": len(assignments), "blocked": sorted(blocked)}


def request_schema(operation: str) -> dict:
    """JSON Schema for transport discovery; runtime validation stays authoritative."""
    identifier = {"type": "string", "minLength": 1, "maxLength": 256}
    properties: dict[str, dict[str, Any]] = {k: dict(identifier) for k in ("occasion_id", "experiment_id", "reason")}
    properties["task_context"] = {"type": "string", "maxLength": 64}
    properties.update(
        mode={"type": "string", "enum": ["off", "observe", "adaptive"]},
        memory_ids={
            "type": "array",
            "items": dict(identifier),
            "minItems": 1 if operation == "assign" else 0,
            "maxItems": 100,
            "uniqueItems": True,
        },
        utility={"type": "object", "nullable": True},
        metrics={"type": "object"},
        succeeded={"type": "boolean"},
        reward={"type": "number", "minimum": 0, "maximum": 1},
        holdout_rate={"type": "number", "minimum": 0.05, "maximum": 0.95},
        min_arm={"type": "integer", "minimum": 10, "maximum": 10000},
        limit={"type": "integer", "minimum": 1, "maximum": 10000},
        decision_revision={"type": "integer", "minimum": 0},
    )
    required = {
        "configure": ["mode"],
        "assign": ["occasion_id", "memory_ids"],
        "delivery": ["occasion_id", "memory_ids"],
        "outcome": ["occasion_id"],
        "rollback": ["reason"],
    }
    return {
        "type": "object",
        "properties": {k: properties[k] for k in sorted(_FIELDS[operation])},
        **({"required": required[operation]} if operation in required else {}),
        **(
            {"oneOf": [{"required": [field]} for field in ("succeeded", "reward", "metrics")]}
            if operation == "outcome"
            else {}
        ),
        "additionalProperties": False,
    }
