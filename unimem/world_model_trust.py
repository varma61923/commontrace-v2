"""Signed world-model registry, action-following checks and sim-to-real trust gates."""
from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any, Protocol, cast

from unimem import frontier_protocol as fp
from unimem import transparency_log as log

REGISTRY = "world-models.jsonl"
OUTCOMES = "world-model-outcomes.jsonl"
GATES = "world-model-gates.jsonl"


class WorldModelAdapter(Protocol):
    def rollout(self, state: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]: ...


_ADAPTERS: dict[str, WorldModelAdapter] = {}
_LOCK = threading.RLock()


def register_adapter(name: str, adapter: WorldModelAdapter) -> None:
    """Install a runtime evaluator in trusted robot deployment configuration."""
    fp.text(name, "world-model adapter", 64)
    if not callable(getattr(adapter, "rollout", None)):
        raise TypeError("world-model adapter must implement rollout(state, action)")
    with _LOCK:
        _ADAPTERS[name] = adapter


def _digest(value: str, label: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def register(root: str, name: str, model_digest: str, *, embodiment: str, tasks: Sequence[str],
             adapter: str) -> dict[str, Any]:
    model_digest = _digest(model_digest, "world-model digest")
    name, embodiment, adapter = (fp.text(v, label, 128) for v, label in
                                 ((name, "world-model name"), (embodiment, "robot embodiment"),
                                  (adapter, "world-model adapter")))
    task_set = sorted(set(fp.text(task, "world-model task", 128) for task in tasks))
    fp.count(len(task_set), 1, 256)
    body = {"name": name, "model_digest": model_digest, "embodiment": embodiment,
            "tasks": task_set, "adapter": adapter, "status": "active"}
    prior = [row for row in fp.journal(root, REGISTRY) if row.get("model_digest") == model_digest]
    if prior:
        if prior[-1] != body:
            raise ValueError("world-model digest registration is immutable")
        return prior[-1]
    signed = fp.append(root, REGISTRY, body)
    log.append(root, "world-model-register", {"model_digest": model_digest, "embodiment": embodiment,
                                              "digest": fp.digest(signed)}, receipt=False)
    return cast(dict[str, Any], signed["record"])


def _model(root: str, model_digest: str, embodiment: str, task: str) -> dict[str, Any]:
    rows = [row for row in fp.journal(root, REGISTRY) if row.get("model_digest") == model_digest]
    if not rows or rows[-1].get("status") != "active":
        raise PermissionError("world model is not registered or has been retired")
    model = rows[-1]
    if model["embodiment"] != embodiment or task not in model["tasks"]:
        raise PermissionError("world model registration does not cover this embodiment and task")
    return model


def record_outcome(root: str, *, model_digest: str, embodiment: str, task: str, episode_id: str,
                   predicted_safe: bool, real_success: bool) -> dict[str, Any]:
    model_digest = _digest(model_digest, "world-model digest")
    model = _model(root, model_digest, fp.text(embodiment), fp.text(task))
    fp.text(episode_id, "real episode id", 128)
    if type(predicted_safe) is not bool or type(real_success) is not bool:
        raise ValueError("world-model evaluation requires binary prediction and real outcome")
    score = 1 if predicted_safe == real_success else -1
    body = {"model_digest": model["model_digest"], "embodiment": model["embodiment"], "task": task,
            "episode_id": episode_id, "predicted_safe": predicted_safe, "real_success": real_success,
            "agreement_score": score}
    with _LOCK:
        if any(row.get("episode_id") == episode_id and row.get("model_digest") == model_digest
               for row in fp.journal(root, OUTCOMES)):
            raise ValueError("real episode already has a world-model outcome")
        signed = fp.append(root, OUTCOMES, body)
    log.append(root, "world-model-outcome", {"episode_id": episode_id, "model_digest": model_digest,
                                              "score": score, "digest": fp.digest(signed)}, receipt=False)
    return cast(dict[str, Any], signed["record"])


def trust(root: str, model_digest: str, *, embodiment: str, task: str, alpha: float = .05,
          minimum_trials: int = 20, minimum_agreement: float = .8) -> dict[str, Any]:
    model_digest = _digest(model_digest, "world-model digest")
    _model(root, model_digest, fp.text(embodiment), fp.text(task))
    fp.number(alpha, 1e-6, .5)
    fp.count(minimum_trials, 1, 10_000)
    fp.number(minimum_agreement, .5, 1)
    rows = [row for row in fp.journal(root, OUTCOMES) if row.get("model_digest") == model_digest
            and row.get("embodiment") == embodiment and row.get("task") == task]
    scores = [fp.number(row["agreement_score"], -1, 1) for row in rows]
    estimate, low, high = _interval(scores, alpha)
    lower_agreement, upper_agreement = (low + 1) / 2, (high + 1) / 2
    trusted = len(scores) >= minimum_trials and lower_agreement >= minimum_agreement
    return {"model_digest": model_digest, "embodiment": embodiment, "task": task, "n": len(scores),
            "agreement_estimate": (estimate + 1) / 2, "agreement_interval": [lower_agreement, upper_agreement],
            "minimum_trials": minimum_trials, "minimum_agreement": minimum_agreement,
            "trusted_for_veto": trusted, "kind": "anytime confidence sequence; not a safety certification"}


def _interval(scores: Sequence[float], alpha: float) -> tuple[float, float, float]:
    from unimem.eprocess import interval

    return interval(scores, bound=1, alpha=alpha)


def _distance(left: Any, right: Any, depth: int = 0) -> float:
    if depth > 16:
        raise ValueError("world-model rollout exceeds comparison depth")
    if isinstance(left, bool) or isinstance(right, bool):
        return 0.0 if left is right else 1.0
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right))
    if isinstance(left, dict) and isinstance(right, dict) and set(left) == set(right):
        return max((_distance(left[key], right[key], depth + 1) for key in left), default=0.0)
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return max((_distance(a, b, depth + 1) for a, b in zip(left, right)), default=0.0)
    return 0.0 if left == right else 1.0


def action_following(adapter: WorldModelAdapter, state: dict[str, Any], action: dict[str, Any], *,
                     perturbations: Sequence[dict[str, Any]], minimum_change: float = 1e-4) -> dict[str, Any]:
    fp.count(len(perturbations), 1, 32)
    minimum_change = fp.number(minimum_change, 1e-12, 1e6)
    baseline = adapter.rollout(state, action)
    fp.wire(baseline)
    if not isinstance(baseline, dict):
        raise ValueError("world-model rollout must return an object")
    differences = []
    for changed_action in perturbations:
        fp.wire(changed_action)
        if not isinstance(changed_action, dict) or changed_action == action:
            raise ValueError("action-following perturbations must be distinct actions")
        changed = adapter.rollout(state, changed_action)
        fp.wire(changed)
        if not isinstance(changed, dict):
            raise ValueError("world-model rollout must return an object")
        differences.append(_distance(baseline, changed))
    follows = all(delta >= minimum_change for delta in differences)
    return {"action_following": follows, "rollout_deltas": differences,
            "minimum_change": minimum_change,
            "kind": "deterministic perturbation sensitivity check; not model validity evidence"}


def gate(root: str, memory_id: str, *, model_digest: str, embodiment: str, task: str,
         state: dict[str, Any], action: dict[str, Any], perturbations: Sequence[dict[str, Any]],
         adapter: WorldModelAdapter | None = None, alpha: float = .05, minimum_trials: int = 20,
         minimum_agreement: float = .8, context: Sequence[str] = ()) -> dict[str, Any]:
    model_digest = _digest(model_digest, "world-model digest")
    model = _model(root, model_digest, fp.text(embodiment), fp.text(task))
    from unimem import search_recipes

    admitted = search_recipes.admit(root, [fp.text(memory_id)], context=context)
    if memory_id not in admitted:
        raise PermissionError("world-model gate source is unavailable or not admitted")
    adapter = adapter or _ADAPTERS.get(model["adapter"])
    if adapter is None:
        raise RuntimeError("registered world-model runtime adapter is not installed")
    action_check = action_following(adapter, state, action, perturbations=perturbations)
    if not action_check["action_following"]:
        decision, reason = "veto", "world model did not respond to a perturbed action"
    else:
        predicted = adapter.rollout(state, action)
        if type(predicted.get("safe")) is not bool:
            raise ValueError("world-model rollout must include a boolean safe verdict")
        if not predicted["safe"]:
            decision, reason = "veto", "registered world model predicted an unsafe rollout"
        else:
            report = trust(root, model_digest, embodiment=embodiment, task=task, alpha=alpha,
                           minimum_trials=minimum_trials, minimum_agreement=minimum_agreement)
            if not report["trusted_for_veto"]:
                decision, reason = "hold", "model trust is insufficient; it cannot promote a real-world action"
            else:
                decision, reason = "eligible-for-real-trial", "simulation passed; real evidence is still required"
    body = {"memory_id": memory_id, "source_hash": fp.digest(admitted[memory_id].statement),
            "model_digest": model_digest, "embodiment": embodiment, "task": task,
            "decision": decision, "reason": reason, "action_following": action_check,
            "at": fp.now()}
    signed = fp.append(root, GATES, body)
    log.append(root, "world-model-gate", {"memory_id": memory_id, "model_digest": model_digest,
                                          "decision": decision, "digest": fp.digest(signed)}, receipt=False)
    return {**body, "trust": trust(root, model_digest, embodiment=embodiment, task=task,
                                   alpha=alpha, minimum_trials=minimum_trials,
                                   minimum_agreement=minimum_agreement)}


def prioritize_real_trials(candidates: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Spend limited real trials on the predicted best and worst candidates first."""
    fp.count(len(candidates), 2, 4096)
    scored = [(fp.text(item["id"], "candidate id"), fp.number(item["predicted_score"], -1, 1))
              for item in candidates]
    if len({identity for identity, _ in scored}) != len(scored):
        raise ValueError("real-trial candidate IDs must be unique")
    ranked = sorted(scored, key=lambda pair: (pair[1], pair[0]))
    return {"bottom": {"id": ranked[0][0], "predicted_score": ranked[0][1]},
            "top": {"id": ranked[-1][0], "predicted_score": ranked[-1][1]},
            "remaining": len(scored) - 2,
            "kind": "endpoint-prioritized trial allocation; not an unbiased population estimate"}
