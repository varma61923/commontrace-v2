"""Doubly robust policy promotion with a finite-sample anytime betting gate.

Waudby-Smith et al. (arXiv:2210.10768); Cook, Mishler & Ramdas
(arXiv:2311.18274); SPIBB (arXiv:1712.06924). This is a conservative bounded
binary-action specialization using predictable betting, not a reproduction of
the cited algorithms. A disjoint training slice freezes nuisance models and
support masks. Certificates cover the supported policy under stationary traffic,
exact propensities, bounded binary outcomes, consistent actions and no interference.
They do not certify an unobserved model upgrade or deployment distribution shift.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Any, Sequence

from unimem import eprocess
from unimem import frontier_protocol as fp
from unimem import transparency_log as log


def evaluate(rows: Sequence[dict[str, Any]], candidate: dict[str, float], incumbent: dict[str, float], *,
             alpha: float = .05, margin: float = 0, minimum_support: int = 10,
             propensity_floor: float = .05, training_size: int = 100) -> dict[str, Any]:
    fp.count(len(rows), 0, fp.MAX_ROWS)
    fp.count(len(candidate), 1, 1024)
    fp.count(minimum_support, 1, 10_000)
    fp.count(training_size, 1, 10_000)
    fp.number(margin, 0, 1)
    fp.number(propensity_floor, .001, .5)
    for policy in (candidate, incumbent):
        for context, probability in policy.items():
            fp.text(context)
            fp.number(probability, 0, 1)
    # Fix this split before observing outcomes. Moving a half-sample split at
    # every look would invalidate optional stopping by recycling evaluation data.
    training, evaluation = rows[:training_size], rows[training_size:]
    seen: set[str] = set()
    support: dict[tuple[str, bool], list[float]] = defaultdict(list)
    scores = []
    supported = 0
    effective = {context: incumbent.get(context, incumbent.get("default", 1)) for context in candidate}
    for i, row in enumerate(rows):
        identity = fp.text(row["occasion_id"])
        if identity in seen:
            raise ValueError("promotion requires one outcome per task")
        seen.add(identity)
        p = fp.number(1 - row["rate"], propensity_floor, 1 - propensity_floor, "injection propensity")
        y = fp.number(row["outcome"], 0, 1, "outcome")
        if type(row["injected"]) is not bool:
            raise ValueError("binary logged action required")
        context = str(row.get("context") or "default")
        if i < len(training):
            support[context, row["injected"]].append(y)
            continue
        base = incumbent.get(context, incumbent.get("default", 1))
        good = all(len(support[context, arm]) >= minimum_support for arm in (False, True))
        target = candidate.get(context, candidate.get("default", base)) if good else base
        effective[context] = target
        supported += good
        m0, m1 = [(sum(support[context, arm]) + 1) / (len(support[context, arm]) + 2)
                  for arm in (False, True)]
        dr = m1 - m0 + (y - m1) / p if row["injected"] else m1 - m0 - (y - m0) / (1 - p)
        scores.append((target - base) * dr)
    estimate, low, high = eprocess.interval(scores, bound=1 / propensity_floor, alpha=alpha)
    supported_policy = {"default": incumbent.get("default", 1), **effective}
    return {"estimate": estimate, "confidence_sequence": [low, high], "margin": margin,
            "promote": bool(scores and low > margin), "support_coverage": supported / max(1, len(evaluation)),
            "effective_candidate": effective, "candidate": candidate, "incumbent": incumbent,
            "policy_hash": fp.digest(supported_policy), "supported_policy": supported_policy,
            "alpha": alpha, "n": len(scores),
            "propensity_floor": propensity_floor, "minimum_support": minimum_support, "training_size": training_size,
            "data_hash": fp.digest(list(rows)),
            "kind": "finite-sample anytime guarantee under stationary trial assumptions"}


def register(root: str, name: str, *, candidate: dict[str, float], incumbent: dict[str, float],
             allowed_changes: list[str], monitoring: str, rollback: str, margin: float = 0,
             alpha: float = .05, minimum_support: int = 10, propensity_floor: float = .05,
             training_size: int = 100, artifact_hash: str = "", trial_unit: str = "",
             trial_rate: float | None = None) -> dict[str, Any]:
    fp.text(name)
    fp.count(len(allowed_changes), 1, 128)
    for change in allowed_changes:
        fp.text(change)
    fp.text(monitoring)
    fp.text(rollback)
    fp.number(margin, 0, 1)
    fp.number(alpha, 1e-6, .99)
    if artifact_hash and (len(artifact_hash) != 64 or any(c not in "0123456789abcdef" for c in artifact_hash)):
        raise ValueError("artifact hash must be a SHA256 commitment")
    evaluate([], candidate, incumbent, minimum_support=minimum_support, propensity_floor=propensity_floor,
             training_size=training_size, alpha=alpha, margin=margin)
    from unimem import holdout_io

    config = holdout_io.load_config(root)
    if not config.running:
        raise ValueError("configure a randomized experiment before registering a change plan")
    body = {"name": name, "candidate": candidate, "incumbent": incumbent,
            "allowed_changes": allowed_changes, "monitoring": monitoring, "rollback": rollback,
            "margin": margin, "alpha": alpha, "minimum_support": minimum_support,
            "propensity_floor": propensity_floor, "training_size": training_size,
            "salt": config.salt, "trial_rate": config.rate, "trial_lesson": "promotion-" + fp.digest(name)[:24]}
    if artifact_hash:
        body["artifact_hash"] = artifact_hash
    if trial_unit:
        from unimem import unit_trials

        body["trial_lesson"] = unit_trials.key(trial_unit)
        body["trial_rate"] = fp.number(config.rate if trial_rate is None else trial_rate, .05, .95)
    elif trial_rate is not None:
        raise ValueError("UniMem typed trial rate requires a typed unit")
    from unimem import _jsonl

    with _jsonl.locked(fp.filename(root, "change-plans.jsonl")):
        if any(row.get("name") == name for row in fp.journal(root, "change-plans.jsonl")):
            raise ValueError("change plan is immutable; register a new name")
        return fp.append(root, "change-plans.jsonl", {**body, "design_hash": fp.digest(body)})


def assign(root: str, plan: str, occasion_id: str, *, context: str = "default") -> dict[str, Any]:
    """Assign one logged binary action for a preregistered change trial."""
    from unimem import holdout_io

    designs = [r for r in fp.journal(root, "change-plans.jsonl") if r["name"] == plan]
    if len(designs) != 1:
        raise ValueError("unknown immutable change plan")
    design = designs[0]
    withheld = holdout_io.assign_and_log(root, [design["trial_lesson"]], occasion_id=occasion_id,
        rate=design["trial_rate"], salt=design["salt"], context=context,
        revisions={design["trial_lesson"]: design["design_hash"]}, scorer="preregistered-change-trial")
    return {"occasion_id": occasion_id, "injected": design["trial_lesson"] not in withheld,
            "trial_lesson": design["trial_lesson"], "design_hash": design["design_hash"]}


def logged_rows(root: str, plan: str) -> list[dict[str, Any]]:
    from unimem.commands.experiment_cmd import _load

    designs = [r for r in fp.journal(root, "change-plans.jsonl") if r["name"] == plan]
    if len(designs) != 1:
        raise ValueError("unknown immutable change plan")
    design = designs[0]
    rows, _, corrupt = _load(root)
    if corrupt:
        raise ValueError("corrupt assignment records cannot certify a release")
    return [{"occasion_id": row.occasion_id, "injected": row.injected, "outcome": int(row.succeeded),
             "rate": row.rate, "context": row.context or "default", "at": row.at.isoformat()}
            for row in rows if row.lesson == design["trial_lesson"] and row.salt == design["salt"]
            and row.revision == design["design_hash"]
            and row.succeeded is not None and row.at is not None]


def certify(root: str, plan: str, rows: Sequence[dict[str, Any]] | None = None, **options: Any) -> dict[str, Any]:
    designs = [r for r in fp.journal(root, "change-plans.jsonl") if r["name"] == plan]
    if len(designs) != 1:
        raise ValueError("an immutable preregistered change plan is required")
    design = designs[0]
    canonical = logged_rows(root, plan)
    if design["trial_lesson"].startswith(("update:", "delta:", "skill:", "bundle:")) or any(
            row.get("artifact") in design["allowed_changes"] for row in fp.journal(root, "compiled-trials.jsonl")):
        from unimem import sentinel_io

        sentinel_io.require_update(root, design.get("artifact_hash", ""))
    if rows is not None and list(rows) != canonical:
        raise ValueError("certificate data must equal canonical logged trial rows in assignment order")
    rows = canonical
    criteria = {key: design[key] for key in ("minimum_support", "propensity_floor", "training_size")}
    if any(key not in criteria or criteria[key] != value for key, value in options.items()):
        raise ValueError("cannot change preregistered acceptance criteria after observing outcomes")
    if any(fp.moment(row["at"]) <= design["recorded_at"] for row in rows):
        raise ValueError("certification outcomes must follow preregistration")
    from unimem import allocation, experiment, holdout_io, integrity
    from unimem.commands.experiment_cmd import _load, _observations

    evidence_rows = [row for row in _load(root)[0]
                     if row.lesson == design["trial_lesson"] and row.salt == design["salt"]
                     and row.revision == design["design_hash"]]
    if any(row.succeeded is None for row in evidence_rows):
        raise ValueError("resolve every assigned trial outcome before certification; selective completion is unsafe")
    audit = integrity.audit(evidence_rows, schedules=allocation.history(root))
    if not audit.readable:
        raise ValueError("canonical integrity audit does not permit release evidence")
    log.head(root)  # Verify nested statement signatures before trusting event digests.
    leaves = fp.journal(root, log.LEAVES)
    assignment_hashes = set()
    outcomes = set()
    for leaf in leaves:
        event = leaf["statement"]["record"]
        if event["created_at"] <= design["recorded_at"]:
            continue
        if event["event"] == "holdout-assignment":
            assignment_hashes.update(event["statement"]["assignments"].values())
        elif event["event"] == "holdout-outcome":
            outcomes.add((event["statement"]["occasion_id"], event["statement"]["succeeded"]))
    raw = [row for row in fp.rows(holdout_io.holdout_log_path(root))
           if row["lesson"] == design["trial_lesson"] and row["salt"] == design["salt"]
           and row.get("revision") == design["design_hash"]]
    if any(fp.digest(row) not in assignment_hashes for row in raw) \
            or any((row["occasion_id"], bool(row["outcome"])) not in outcomes for row in rows):
        raise ValueError("trial assignments/outcomes are not anchored in the transparency history")
    result = evaluate(rows, design["candidate"], design["incumbent"], margin=design["margin"],
                      alpha=design["alpha"], **criteria)
    if not result["promote"]:
        raise ValueError("candidate has not cleared the preregistered promotion margin")
    signed = fp.sign(root, {**result, "design_hash": design["design_hash"], "plan": plan,
                            **({"artifact_hash": design["artifact_hash"]} if design.get("artifact_hash") else {}),
                            "issued_at": fp.now(), "integrity_verdict": audit.verdict,
                            "experiment_effects": [asdict(effect) for effect in
                                                   experiment.analyze(_observations(evidence_rows), sequential=True)]})
    anchor = log.append(root, "policy-promotion", {"certificate_digest": fp.digest(signed)})
    fp.append(root, "promotion-certificates.jsonl", {"certificate": signed, "anchor": anchor})
    return {"certificate": signed, "anchor": anchor}


def verify(root: str, certificate: dict[str, Any], *, policy_hash: str = "", artifact_hash: str = "") -> bool:
    try:
        signed = certificate["certificate"]
        body = signed["record"]
        from unimem import sentinel_io

        digest = body.get("artifact_hash", "")
        if sentinel_io.enabled(root):
            from unimem import nextwave_store

            if "update:"+digest in nextwave_store.latest(root, sentinel_io.UPDATES):
                sentinel_io.require_update(root, digest)
        revoked = {r["certificate_digest"] for r in fp.journal(root, "promotion-rollbacks.jsonl")}
        return (fp.verify(root, signed) and log.verify(root, certificate["anchor"])
                and certificate["anchor"]["statement"]["record"]["statement"]["certificate_digest"] == fp.digest(signed)
                and body["confidence_sequence"][0] > body["margin"] and body["promote"]
                and (not policy_hash or body["policy_hash"] == policy_hash)
                and (not artifact_hash or body.get("artifact_hash") == artifact_hash)
                and fp.digest(signed) not in revoked)
    except (ValueError, KeyError, TypeError, PermissionError):
        return False


def rollback(root: str, certificate: dict[str, Any], reason: str) -> dict[str, Any]:
    if not verify(root, certificate):
        raise ValueError("invalid or already revoked certificate")
    result = fp.append(root, "promotion-rollbacks.jsonl",
                       {"certificate_digest": fp.digest(certificate["certificate"]), "reason": fp.text(reason)})
    log.append(root, "policy-rollback", {"certificate_digest": fp.digest(certificate["certificate"])})
    return result
