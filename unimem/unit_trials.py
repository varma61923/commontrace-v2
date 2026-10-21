"""UniMem typed trials reuse canonical assignments, schedules and certificates.

The experiment, integrity audit, AIPW/e-process and promotion certificate remain
the evidence of record. Registry entries describe eligibility; excluded units
are never represented as withheld observations. Independent typed factors use
separate deterministic hash inputs and an explicitly recorded factorial design.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from unimem import _jsonl, aipw, allocation, eprocess, holdout_io, promotion, safety
from unimem import frontier_protocol as fp
from unimem import nextwave_store as store

PREFIXES = ("skill:", "bundle:", "writer:", "delta:", "ask:", "operator:", "update:")


def key(unit: str) -> str:
    fp.text(unit, "typed randomization unit", 128)
    if not unit.startswith(PREFIXES) or not unit.partition(":")[2]:
        raise ValueError("UniMem trial requires a supported typed unit")
    return unit


def register(root: str, unit: str, artifact_hash: str, *, alpha: float = .05, rate: float = .5,
             margin: float = 0, training_size: int = 100, minimum_support: int = 10) -> dict[str, Any]:
    with _jsonl.locked(fp.filename(root, store.journal_name("unit-trials"))):
        return _register(root, unit, artifact_hash, alpha=alpha, rate=rate, margin=margin,
                         training_size=training_size, minimum_support=minimum_support)


def _register(root: str, unit: str, artifact_hash: str, *, alpha: float, rate: float,
              margin: float, training_size: int, minimum_support: int) -> dict[str, Any]:
    store.operator()
    key(unit)
    fp.number(rate, .05, .95)
    design = {"unit": unit, "artifact_hash": artifact_hash, "alpha": alpha, "rate": rate, "margin": margin,
              "training_size": training_size, "minimum_support": minimum_support}
    identity = fp.digest(unit)
    if identity in store.latest(root, "unit-trials"):
        prior = store.get(root, "unit-trials", identity)
        if prior["design"] != design:
            raise ValueError("UniMem typed trial design is immutable")
        return prior
    if unit in safety.protected(root):
        raise PermissionError("UniMem hazards cannot be trial units")
    cfg = holdout_io.load_config(root)
    if not cfg.running:
        raise ValueError("configure the UniMem experiment before starting a typed trial")
    name = "unimem-" + identity[:32]
    plans = [p for p in fp.journal(root, "change-plans.jsonl") if p["name"] == name]
    if not plans:
        promotion.register(root, name, candidate={"default": 1}, incumbent={"default": 0},
            allowed_changes=[unit], monitoring="canonical e-process and harm budget", rollback="retire typed unit",
            alpha=alpha, margin=margin, training_size=training_size, minimum_support=minimum_support,
            propensity_floor=min(rate, 1-rate), artifact_hash=artifact_hash, trial_unit=unit, trial_rate=rate)
    elif (plans[0].get("artifact_hash") != artifact_hash or plans[0]["trial_lesson"] != unit
          or any(plans[0][k] != v for k, v in {"alpha": alpha, "margin": margin, "trial_rate": rate,
                 "training_size": training_size, "minimum_support": minimum_support}.items())):
        raise ValueError("UniMem typed plan recovery does not match its artifact")
    past = [s for s in allocation.history(root) if s.salt == cfg.salt]
    prior_schedule = past[-1] if past else None
    rates = dict(prior_schedule.rates) if prior_schedule else {}
    rates[unit] = rate
    schedule = allocation._publish(root, salt=cfg.salt, default_rate=cfg.rate, rates=rates,
        policy=allocation.Policy(**prior_schedule.policy) if prior_schedule else allocation.Policy(),
        context_rates=prior_schedule.context_rates if prior_schedule else None,
        basis={"reason": "UniMem typed trial preregistration", "unit": unit, "artifact_hash": artifact_hash},
        now=datetime.fromisoformat(fp.now()))
    value = {"id": identity, "unit": unit, "plan": name, "design": design,
             "schedule": schedule.digest, "eligible_after": schedule.effective_from, "salt": cfg.salt}
    store.put(root, "unit-trials", identity, value, immutable=True)
    return value


def trial(root: str, unit: str) -> dict[str, Any]:
    return store.get(root, "unit-trials", fp.digest(key(unit)))


def assign(root: str, unit: str, occasion: str, *, context: str = "default") -> dict[str, Any]:
    data = trial(root, unit)
    if data["eligible_after"] > fp.now():
        raise ValueError("UniMem allocation schedule is published but not effective yet")
    cfg = holdout_io.load_config(root)
    current = allocation.active(root, data["salt"], datetime.fromisoformat(fp.now()))
    label = holdout_io.context_label(context) or "default"
    if (not cfg.running or cfg.salt != data["salt"] or current is None
            or current.rate_for(unit, label) != data["design"]["rate"]):
        raise ValueError("UniMem typed trial configuration changed; preregister a fresh trial")
    # Canonical holdout I/O owns its own frontmatter lock. A distinct lock
    # serializes typed retries without recursively acquiring that flock.
    with _jsonl.locked(holdout_io.holdout_log_path(root) + ".unimem"):
        logged, corrupt = holdout_io.read_log(root)
        if corrupt:
            raise ValueError("UniMem typed assignment requires a readable canonical log")
        prior = [r for r in logged if r.lesson == unit and r.occasion_id == occasion]
        if prior:
            row = prior[0]
            if row.salt != data["salt"] or row.rate != data["design"]["rate"] or row.context != label:
                raise ValueError("UniMem occasion is already bound to a different trial context")
            return {"occasion_id": occasion, "injected": row.injected, "trial_lesson": unit,
                    "design_hash": row.revision}
        return promotion.assign(root, data["plan"], occasion, context=label)


def excluded(root: str, unit: str, occasion: str, reason: str, *, parent: str) -> None:
    store.event(root, "unit-eligibility", fp.digest([unit, occasion]),
        {"unit": key(unit), "occasion": fp.text(occasion), "status": "ineligible",
         "reason": fp.text(reason), "parent": fp.text(parent)})


def rows(root: str, unit: str) -> list[dict[str, Any]]:
    return promotion.logged_rows(root, trial(root, unit)["plan"])


def report(root: str, unit: str) -> dict[str, Any]:
    data = trial(root, unit)
    observations = rows(root, unit)
    design = data["design"]
    result = promotion.evaluate(observations, {"default": 1}, {"default": 0}, alpha=design["alpha"],
        margin=design["margin"], propensity_floor=min(design["rate"], 1-design["rate"]),
        training_size=design["training_size"], minimum_support=design["minimum_support"])
    obs = [aipw.Observation(r["injected"], bool(r["outcome"]), r["rate"], r["context"]) for r in observations]
    helps, hurts = eprocess.process(obs), eprocess.process(obs, direction="hurts")
    bound = 1/min(design["rate"], 1-design["rate"])
    return {"unit": unit, **result, "e_value": helps.e_value, "harm_e_value": hurts.maximum,
            "minimum_detectable_effect": min(2.0, 2.802 * bound / math.sqrt(max(1, len(obs)))),
            "power_note": "normal-approximation 80% design planning; not a confidence-sequence power guarantee",
            "verdict": "POSITIVE_BOUND" if result["promote"] else "UNDERPOWERED", "classification": "estimate",
            "assumptions": ["canonical exact-propensity trial", "stationary traffic; no interference",
                            "consistent delivered catalog and complete outcome reporting"]}


def certify(root: str, unit: str) -> dict[str, Any]:
    store.operator()
    return promotion.certify(root, trial(root, unit)["plan"])
