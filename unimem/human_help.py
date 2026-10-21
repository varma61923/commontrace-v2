"""Governed corrections, fixed-cohort operator review and bounded asking trials.

Human identities and physical execution are reporter assertions. Outcome labels
come from canonical outcomes; correction residuals are not causal operator effects.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from unimem import frontier_protocol as fp


def _store() -> Any:
    from unimem import nextwave_store

    return nextwave_store


def register_operator(root: str, operator: str, principal: str, skill_tier: str, *,
                      quality_horizon: int = 40, context: Sequence[str] = ()) -> dict[str, Any]:
    store = _store()
    store.operator()
    for value in (operator, principal):
        fp.text(value, "operator identity", 128)
    if skill_tier not in {"novice", "trained", "expert"}:
        raise ValueError("UniMem operator skill tier must be novice, trained or expert")
    fp.count(quality_horizon, 40, 4096)
    roster = store.latest(root, "human-operators")
    if operator in roster:
        prior = store.get(root, "human-operators", operator, context=context)
        if (prior["principal"] != principal or prior["skill_tier"] != skill_tier
                or prior["quality_horizon"] != quality_horizon or set(prior["scopes"]) != set(context)):
            raise PermissionError("UniMem operator enrollment is immutable; use a distinct scoped identity")
        return dict(prior)
    fp.count(len(roster), 0, 63, "registered human operators")
    body = {"operator": operator, "principal": principal, "skill_tier": skill_tier,
            "quality_horizon": quality_horizon, "registered_at": fp.now(), "scopes": sorted(set(context)),
            "identity": "operator-enrolled identity assertion; physical identity not attested"}
    store.put(root, "human-operators", operator, body, scopes=context, immutable=True)
    return body


def _canonical(root: str, unit: str, occasion: str) -> dict[str, Any]:
    row = _canonical_rows(root, [unit]).get((unit, occasion))
    if row is None:
        raise PermissionError("UniMem intervention requires an existing canonical occasion and unit")
    return row


def _canonical_rows(root: str, units: Sequence[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Read authentic assignments/outcomes without quoting a treatment effect.

    An ongoing intervention cohort may contain pending outcomes. Effect-specific
    attrition/arm-balance diagnostics are applied separately by the trial engine.
    """
    from unimem import allocation, holdout_io, integrity
    from unimem.commands.experiment_cmd import _load

    fp.rows(holdout_io.holdout_log_path(root))
    fp.rows(holdout_io.outcomes_log_path(root))
    rows, _, corrupt = _load(root)
    if corrupt:
        raise PermissionError("UniMem correction quality requires a readable canonical log")
    selected = [row for row in rows if row.lesson in units]
    finding = integrity.check_assignment_drift(selected, schedules=allocation.history(root))
    if finding.severity == integrity.SEVERITY_INVALIDATES:
        raise PermissionError("UniMem intervention assignment differs from its registered randomization")
    result = {}
    for row in selected:
        key = row.lesson, row.occasion_id
        if key in result:
            raise PermissionError("UniMem intervention assignment is duplicated")
        result[key] = {"unit": row.lesson, "occasion_id": row.occasion_id, "injected": row.injected,
            "rate": row.rate, "salt": row.salt, "outcome": row.succeeded, "context": row.context or "default",
            "revision": row.revision, "schedule": row.schedule}
    return result


def intervention(root: str, occasion: str, unit: str | None, operator: str, *, pre_window: list[str],
                 corrective_segment: list[str], baseline: float, operator_seconds: int,
                 episode_seconds: int, started_at: str, ended_at: str,
                 context: Sequence[str] = ()) -> dict[str, Any]:
    from unimem import frontmatter, holdout_io, incident, person_io, transparency_log

    store = _store()
    store.operator()
    fp.number(baseline, 0, 1, "pre-outcome baseline prediction")
    fp.count(operator_seconds, 1, 86400)
    fp.count(episode_seconds, operator_seconds, 86400, "episode duration")
    started_at, ended_at = fp.moment(started_at), fp.moment(ended_at)
    if not started_at <= ended_at <= fp.now():
        raise ValueError("UniMem intervention window must be ordered and already observed")
    fp.count(len(pre_window), 1, 16)
    fp.count(len(corrective_segment), 1, 16)
    sources = sorted(set([*([unit] if unit is not None else []), *pre_window, *corrective_segment]))
    with incident.protect(root), frontmatter.locked(holdout_io.outcomes_log_path(root)):
        human = store.get(root, "human-operators", operator, context=context)
        if set(context) != set(human["scopes"]):
            raise PermissionError("UniMem corrector quality cohorts cannot mix ownership scopes")
        admitted = store.admit(root, sources, context=context)
        if set(admitted) != set(sources):
            raise PermissionError("UniMem intervention windows must use admitted canonical sources")
        if unit is None:
            fp.rows(holdout_io.outcomes_log_path(root))
        canonical = (_canonical(root, unit, occasion) if unit is not None else
                     {"unit": None, "occasion_id": occasion, "outcome": holdout_io.read_outcomes(root).get(occasion)})
        if canonical["outcome"] is not None:
            raise PermissionError("UniMem quality evidence must be registered before the canonical outcome")
        snapshots = {}
        for identity in set([*pre_window, *corrective_segment]):
            _, content, _ = person_io._snapshot(root, identity)
            fp.count(len(fp.wire(content)), 1, 32768, "intervention segment bytes")
            snapshots[identity] = content
        identity = "intervention-"+fp.digest([unit, occasion, operator])[:32]
        body = {"id": identity, "operator": operator, "skill_tier": human["skill_tier"],
            "occasion": occasion, "unit": unit, "baseline": baseline, "operator_seconds": operator_seconds,
            "episode_seconds": episode_seconds, "started_at": started_at, "ended_at": ended_at,
            "canonical_assignment": {key: value for key, value in canonical.items() if key != "outcome"},
            "pre_window": pre_window, "corrective_segment": corrective_segment, "snapshots": snapshots}
        store.put(root, "human-interventions", identity, body, scopes=context, sources=sources, immutable=True)
        prior = store.latest(root, "human-intervention-index").get(identity)
        if prior is None:
            store.event(root, "human-intervention-index", identity, {"operator": operator,
                "occasion": occasion, "unit": unit, "corrections": corrective_segment,
                "baseline": baseline, "scopes": list(context),
                "assignment_digest": fp.digest(body["canonical_assignment"]),
                "operator_seconds": operator_seconds, "episode_seconds": episode_seconds})
        anchor = transparency_log.append(root, "human-intervention", {"intervention_digest": fp.digest(body)})
        return {"intervention": body, "anchor": anchor, "outcome": "pending canonical outcome"}


def _resolved(root: str, index: Mapping[str, Any], canonical: Mapping[tuple[str, str], dict[str, Any]],
              artifacts: Mapping[str, dict[str, Any]], unassigned: Mapping[str, bool]) -> dict[str, Any] | None:
    from unimem import memory_authority, outcomes

    if (memory_authority.lineage_blocked(root, index["id"])
            or artifacts.get(index["id"], {}).get("status") != "active"):
        return None
    row = ({"unit": None, "occasion_id": index["occasion"], "outcome": unassigned.get(index["occasion"])}
           if index["unit"] is None else canonical.get((index["unit"], index["occasion"])))
    if row is None:
        return None
    if fp.digest({key: value for key, value in row.items() if key != "outcome"}) != index["assignment_digest"]:
        raise PermissionError("UniMem intervention's canonical assignment changed")
    if row["outcome"] is None:
        return None
    return outcomes.intervention_advantage(row["outcome"], index["baseline"], outcome_digest=fp.digest(row))


def operator_quality(root: str, operator: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    store = _store()
    store.operator()
    human = store.get(root, "human-operators", operator, context=context)
    if set(context) != set(human["scopes"]):
        raise PermissionError("UniMem operator quality requires the enrolled ownership scope")
    records = [row for row in store.events(root, "human-intervention-index") if row.get("operator") == operator]
    from unimem import holdout_io

    horizon = human["quality_horizon"]
    fp.rows(holdout_io.outcomes_log_path(root))
    unassigned = holdout_io.read_outcomes(root)
    complete_blocks = len(records)//horizon
    artifacts = store.latest(root, "human-interventions")
    units = sorted({row["unit"] for row in records if row["unit"] is not None})
    canonical = _canonical_rows(root, units) if units else {}
    blocks: list[dict[str, Any]] = []
    for block in range(max(1, complete_blocks)):
        cohort = records[block*horizon:(block+1)*horizon]
        measured = [_resolved(root, row, canonical, artifacts, unassigned) for row in cohort]
        resolved = [value for value in measured if value is not None]
        average = interval = None
        status = "review"
        if len(cohort) == horizon and len(resolved) == horizon:
            average = math.fsum(value["advantage"] for value in resolved)/horizon
            # Spending over predeclared fixed blocks and at most 64 operators.
            alpha = .05/(64*(block+1)*(block+2))
            radius = math.sqrt(2*math.log(2/alpha)/horizon)
            interval = [max(-1, average-radius), min(1, average+radius)]
            status = "quarantined" if interval[1] < -.1 else "eligible" if interval[0] >= 0 else "review"
        blocks.append({"block": block+1, "status": status, "resolved": len(resolved),
                       "mean_advantage": average, "interval": interval, "cohort_digest": fp.digest(cohort)})
    latest = blocks[-1]
    status = "quarantined" if any(b["status"] == "quarantined" for b in blocks) else latest["status"]
    report = {"operator": operator, "skill_tier": human["skill_tier"], "status": status,
        "horizon": horizon, "logged": len(records), "resolved": latest["resolved"],
        "mean_advantage": latest["mean_advantage"], "interval": latest["interval"], "blocks": blocks,
        "interpretation": "fixed-block downstream residual quality; not a causal operator comparison"}
    report["report_digest"] = fp.digest(report)
    return report


def review(root: str, operator: str, report_digest: str, *, decision: str, reason: str,
           context: Sequence[str] = ()) -> dict[str, Any]:
    from unimem import memory_authority, transparency_log

    store = _store()
    store.operator()
    human = store.get(root, "human-operators", operator, context=context)
    if memory_authority.bound_writer()[0] == human["principal"]:
        raise PermissionError("UniMem a corrector cannot approve their own training eligibility")
    if decision not in {"approve", "reject"}:
        raise ValueError("UniMem human-quality review must approve or reject")
    current = operator_quality(root, operator, context=context)
    if current["report_digest"] != report_digest:
        raise PermissionError("UniMem human-quality review is for a stale report")
    value = {"operator": operator, "report_digest": report_digest, "decision": decision,
             "reason_digest": fp.digest(fp.text(reason, "quality review reason", 2048)),
             "reviewer_digest": fp.digest(memory_authority.bound_writer()), "reviewed_at": fp.now()}
    signed = store.event(root, "human-quality-reviews", operator, value)
    anchor = transparency_log.append(root, "human-quality-review", {"review_digest": fp.digest(signed)})
    return {"review": signed, "anchor": anchor}


def appeal(root: str, operator: str, reason: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    store = _store()
    store.operator()
    report = operator_quality(root, operator, context=context)
    value = {"operator": operator, "reason": fp.text(reason, "appeal", 4096),
             "report_digest": report["report_digest"], "status": "review-requested"}
    identity = fp.digest(value)
    store.put(root, "human-quality-appeals", identity, value, scopes=context)
    return {"appeal_id": identity, "status": "review-requested", "training_eligibility_changed": False}


def assert_training(root: str, identities: Sequence[str], *, context: Sequence[str] = ()) -> None:
    import os

    from unimem import lineage

    store = _store()
    if not os.path.isfile(fp.filename(root, store.journal_name("human-intervention-index"))):
        return
    sources = set(identities)
    for identity in identities:
        sources.update(lineage.ancestors(root, identity))
    reviews = store.latest(root, "human-quality-reviews")
    reports: dict[str, dict[str, Any]] = {}
    for row in store.events(root, "human-intervention-index"):
        if not sources.intersection(row.get("corrections", [])):
            continue
        if row["operator"] not in reports:
            reports[row["operator"]] = operator_quality(root, row["operator"], context=context)
        report = reports[row["operator"]]
        reviewed = reviews.get(row["operator"], {})
        if reviewed.get("report_digest") == report["report_digest"] and reviewed.get("decision") == "reject":
            raise PermissionError("UniMem correction training was rejected by quality review")
        approved = reviewed.get("report_digest") == report["report_digest"] and reviewed.get("decision") == "approve"
        if report["status"] != "eligible" and not approved:
            raise PermissionError("UniMem correction training is held by human-operator quality review")


def interventions(root: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    from unimem import recovery

    store = _store()
    store.operator()
    rows = store.list_objects(root, "human-interventions", context=context)
    episodes = [{"timestamp": row["started_at"], "duration_hours": row["episode_seconds"]/3600,
                 "interventions": 1} for row in rows]
    return {"interventions": rows, "kpis": recovery.interventions_per_hour(episodes),
            "operator_minutes": math.fsum(row["operator_seconds"] for row in rows)/60,
            "interpretation": "reported durations; saved minutes require an asking trial"}


def asking_estimate(rows: Sequence[Mapping[str, Any]], *, prior: float = 0, prior_strength: float = 20,
                    alpha: float = .05) -> dict[str, Any]:
    """Unbiased IPW value stays separate from the planner's shrunken heuristic."""
    fp.count(len(rows), 1, 10000)
    fp.number(prior, -1, 1, "frozen value-of-information prior")
    fp.number(prior_strength, 0, 1000)
    fp.number(alpha, .000001, .2)
    scores, ask_costs, observed_costs = [], [], []
    bound = 0.0
    for row in rows:
        if type(row["ask"]) is not bool or type(row["outcome"]) is not bool:
            raise ValueError("UniMem asking estimates need actual binary arms and outcomes")
        p = fp.number(row["probability"], .05, .95, "logged ask propensity")
        cost = fp.number(row["operator_seconds"], 0, 86400)
        scores.append((1/p if row["ask"] else -1/(1-p))*int(row["outcome"]))
        ask_costs.append(cost/p if row["ask"] else 0)
        observed_costs.append(cost)
        bound = max(bound, 1/p+1/(1-p))
    n = len(rows)
    estimate = math.fsum(scores)/n
    radius = bound*math.sqrt(math.log(2/alpha)/(2*n))
    return {"n": n, "effect_ipw": estimate, "interval": [max(-1, estimate-radius), min(1, estimate+radius)],
        "sampling_variance": math.fsum((score-estimate)**2 for score in scores)/(n*(n-1)) if n > 1 else None,
        "planner_value": (n*estimate+prior_strength*prior)/(n+prior_strength),
        "planner_kind": "empirical-Bayes-style shrinkage heuristic; not the unbiased estimator or a confidence bound",
        "operator_minutes": math.fsum(observed_costs)/60,
        "operator_minutes_saved_estimate": (math.fsum(ask_costs)-math.fsum(observed_costs))/60,
        "reference": "estimated operator time under always asking minus observed policy time",
        "assumptions": "exact propensities, complete registered cohort, bounded outcomes, no interference"}


def evpi(beliefs: list[float], utilities: list[list[float]]) -> float:
    """Perfect-information value under an explicitly supplied finite belief model."""
    fp.count(len(beliefs), 2, 32)
    fp.count(len(utilities), 2, 8)
    for probability in beliefs:
        fp.number(probability, 0, 1)
    if abs(math.fsum(beliefs)-1) > 1e-9:
        raise ValueError("UniMem belief probabilities must sum to one")
    for action in utilities:
        if len(action) != len(beliefs):
            raise ValueError("UniMem every action needs one bounded utility per belief state")
        for value in action:
            fp.number(value, 0, 1)
    perfect = math.fsum(probability*max(action[index] for action in utilities)
                        for index, probability in enumerate(beliefs))
    current = max(math.fsum(p*value for p, value in zip(beliefs, action)) for action in utilities)
    return max(0, perfect-current)


def fit_asking_prior(reports: list[dict[str, Any]], belief_prior: float) -> dict[str, Any]:
    """Fit Gaussian random-effects hyperparameters from completed prior contexts.

    This is an empirical-Bayes planner model, not a frequentist confidence bound.
    Config enrollment separately resolves each report from the canonical ledger.
    """
    fp.count(len(reports), 3, 8)
    fp.number(belief_prior, -1, 1)
    residuals, noise, per_row = [], [], []
    for report in reports:
        if report["status"] != "complete" or report["sampling_variance"] is None:
            raise ValueError("UniMem empirical-Bayes calibration needs completed canonical asking reports")
        effect = fp.number(report["effect_ipw"], -20, 20)
        baseline = fp.number(report["belief_prior"], -1, 1)
        variance = fp.number(report["sampling_variance"], 0, 400)
        size = fp.count(report["n"], 2, 10000)
        residuals.append(effect-baseline)
        noise.append(variance)
        per_row.append(variance*size)
    mean = math.fsum(residuals)/len(residuals)
    between = math.fsum((value-mean)**2 for value in residuals)/(len(residuals)-1)
    variance = max(1e-6, between-math.fsum(noise)/len(noise))
    strength = min(1000, math.fsum(per_row)/len(per_row)/variance)
    return {"prior": max(-1, min(1, belief_prior+mean)), "prior_strength": strength,
            "between_context_variance": variance, "bias_adjustment": mean,
            "calibration_digests": [fp.digest(report) for report in reports],
            "kind": "empirical-Bayes Gaussian random-effects planner heuristic"}


def plan(tasks: list[dict[str, Any]], *, budget_seconds: int) -> dict[str, Any]:
    """Select at most one asking mode per task under an explicit integer budget."""
    fp.count(len(tasks), 0, 256)
    fp.count(budget_seconds, 0, 86400)
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    identities = set()
    for task in tasks:
        if set(task) != {"id", "risk", "modes"}:
            raise ValueError("UniMem asking tasks require only id, risk and modes")
        identity = fp.text(task["id"], "asking task", 128)
        if identity in identities:
            raise ValueError("UniMem asking tasks must have distinct IDs")
        identities.add(identity)
        if task["risk"] != "low-risk":
            candidates.append((float("inf"), identity, {"mode": "safe-escalation", "value": 0, "seconds": 0}))
            continue
        modes = task["modes"]
        fp.count(len(modes), 1, 8)
        for mode in modes:
            if set(mode) != {"mode", "seconds", "value"}:
                raise ValueError("UniMem asking mode fields cannot override the planner decision")
            fp.text(mode["mode"], "asking mode", 64)
            seconds = fp.count(mode["seconds"], 1, 86400)
            value = fp.number(mode["value"], -1, 1)
            if value > 0:
                candidates.append((value/seconds, identity, {**mode, "seconds": seconds, "value": value}))
    chosen: dict[str, dict[str, Any]] = {}
    used = 0
    for _, identity, mode in sorted(candidates, key=lambda row: (-row[0], row[1], row[2]["mode"])):
        if identity in chosen or mode["seconds"]+used > budget_seconds:
            continue
        used += mode["seconds"]
        chosen[identity] = {"id": identity, "decision": "which-mode" if mode["mode"] == "safe-escalation" else "ask",
                            **mode}
    return {"decisions": [chosen.get(task["id"], {"id": task["id"], "decision": "dont-ask",
                         "reason": "no positive-value mode fits the budget"}) for task in tasks],
            "reserved_seconds": used, "budget_seconds": budget_seconds,
            "method": "deterministic value-per-second heuristic; safety escalation consumes no experimental budget"}


def configure_asking(root: str, context: str, question_type: str, *, enabled: bool = False,
                     risk: str = "unknown", budget_seconds: int = 0, cost_seconds: int = 30,
                     probability: float = .5, trial_size: int = 100, prior: float = 0,
                     prior_strength: float = 20, requesters: list[str] | None = None,
                     sources: list[str] | None = None, scopes: Sequence[str] = (),
                     beliefs: list[float] | None = None, utilities: list[list[float]] | None = None,
                     calibration: list[dict[str, str]] | None = None) -> dict[str, Any]:
    from unimem import _jsonl, holdout_io, memory_authority, safety, unit_trials

    store = _store()
    store.operator()
    context = holdout_io.context_label(context)
    question_type = holdout_io.context_label(question_type)
    if not context or not question_type or ":" in context+question_type or len(context)+len(question_type) > 110:
        raise ValueError("UniMem asking context and question type must be distinct bounded typed labels")
    if type(enabled) is not bool or risk not in {"low-risk", "safety", "unknown"}:
        raise ValueError("UniMem asking policy requires explicit risk and boolean enablement")
    if enabled and risk != "low-risk":
        raise PermissionError("UniMem asking randomization is restricted to operator-enrolled low-risk contexts")
    fp.count(budget_seconds, 0, 86400)
    fp.count(cost_seconds, 1, 3600)
    fp.count(trial_size, 40, 10000)
    fp.number(probability, .05, .95)
    fp.number(prior, -1, 1)
    fp.number(prior_strength, 0, 1000)
    if (beliefs is None) != (utilities is None):
        raise ValueError("UniMem EVPI needs both belief probabilities and action utilities")
    belief_prior = evpi(beliefs, utilities) if beliefs is not None and utilities is not None else prior
    prior_model: dict[str, Any] = {"prior": belief_prior, "prior_strength": prior_strength,
                                 "kind": "operator-enrolled belief prior; no empirical calibration"}
    calibration_sources: list[str] = []
    if calibration:
        fp.count(len(calibration), 3, 8)
        reports = []
        for previous in calibration:
            if set(previous) != {"context", "question_type"} or previous == {
                    "context": context, "question_type": question_type}:
                raise ValueError("UniMem prior calibration must use other completed asking contexts")
            old_unit = "ask:"+previous["context"]+":"+previous["question_type"]
            metadata = store.latest(root, "ask-policies").get(fp.digest(old_unit))
            if metadata is None or set(metadata["scopes"]) != set(scopes):
                raise PermissionError("UniMem asking calibration must preserve the enrolled ownership scopes")
            report = asking_report(root, **previous)
            if report["status"] != "complete":
                raise ValueError("UniMem prior calibration requires completed canonical asking reports")
            reports.append(report)
            calibration_sources.append(_prior_evidence(root, old_unit, report, scopes))
        if len({report["unit"] for report in reports}) != len(reports):
            raise ValueError("UniMem empirical-Bayes contexts must be distinct")
        prior_model = fit_asking_prior(reports, belief_prior)
    prior, prior_strength = prior_model["prior"], prior_model["prior_strength"]
    unit = "ask:"+context+":"+question_type
    selected = sorted(set(sources or []))
    fp.count(len(selected), 0, 64)
    if set(store.admit(root, selected, context=scopes)) != set(selected):
        raise PermissionError("UniMem asking context sources are not admitted")
    if enabled and set(selected) & set(safety.protected(root)):
        raise PermissionError("UniMem protected hazards cannot enter asking experiments")
    if enabled and budget_seconds < trial_size*cost_seconds:
        raise ValueError("UniMem reserve the complete trial's maximum operator time before randomizing")
    actor = memory_authority.bound_writer()
    allowed = requesters if requesters is not None else [actor[1]+":"+actor[0]]
    fp.count(len(allowed), 1, 64)
    for requester in allowed:
        fp.text(requester, "authorized planner principal", 256)
    inherited_sources = sorted(set([*selected, *calibration_sources]))
    body = {"unit": unit, "context": context, "question_type": question_type,
        "risk": risk, "budget_seconds": budget_seconds, "cost_seconds": cost_seconds,
        "probability": probability, "trial_size": trial_size, "prior": prior, "prior_strength": prior_strength,
        "belief_prior": belief_prior, "prior_model": prior_model,
        "requesters": sorted(set(allowed)), "sources": inherited_sources, "scopes": list(scopes)}
    if enabled and not holdout_io.load_config(root).running:
        raise ValueError("UniMem configure the canonical experiment before enabling asking trials")
    if enabled:
        from pathlib import Path

        filename = fp.filename(root, "human-asking-fleet.json")
        with _jsonl.locked(fp.filename(root, "asking-budget.lock")):
            if Path(filename).exists():
                if _fleet_budget(root) != budget_seconds:
                    raise PermissionError("UniMem all asking policies share the enrolled fleet budget")
            else:
                _jsonl.write_json(filename, fp.sign(root, {"schema": "unimem.asking-budget.v1",
                                                         "budget_seconds": budget_seconds}))
    store.put(root, "ask-policies", fp.digest(unit), body, scopes=scopes, sources=inherited_sources, immutable=True)
    if enabled:
        unit_trials.register(root, unit, fp.digest(body), rate=1-probability, training_size=trial_size,
                             minimum_support=10)
    fp.append(root, "human-asking-enablements.jsonl", {"unit": unit, "enabled": enabled,
                                                    "policy_digest": fp.digest(body)})
    return {**body, "enabled": enabled}


def _prior_evidence(root: str, unit: str, report: dict[str, Any], scopes: Sequence[str]) -> str:
    """A learned prior remains linked to deletable, admitted outcome evidence."""
    store = _store()
    claims = [row for row in fp.journal(root, "human-asking-reservations.jsonl") if row["unit"] == unit]
    references = [store.reference("asking-costs", row["id"]) for row in claims]
    indexes = []
    for offset in range(0, len(references), 128):
        chunk = references[offset:offset+128]
        identity = fp.digest(chunk)
        store.put(root, "asking-prior-sources", identity, {"sources": chunk}, scopes=scopes,
                  sources=chunk, immutable=True)
        indexes.append(store.reference("asking-prior-sources", identity))
    identity = fp.digest(report)
    store.put(root, "asking-prior-reports", identity, report, scopes=scopes, sources=indexes, immutable=True)
    return str(store.reference("asking-prior-reports", identity))


def _fleet_budget(root: str) -> int:
    import json

    with open(fp.filename(root, "human-asking-fleet.json"), "rb") as stream:
        raw = stream.read(fp.MAX_BYTES+1)
    if len(raw) > fp.MAX_BYTES:
        raise PermissionError("UniMem asking budget state exceeds its bound")
    signed = json.loads(raw)
    if not fp.verify(root, signed) or signed["record"].get("schema") != "unimem.asking-budget.v1":
        raise PermissionError("UniMem asking budget signature is invalid")
    return fp.count(signed["record"]["budget_seconds"], 0, 86400)


def should_ask(root: str, context: str, question_type: str, occasion: str) -> dict[str, Any]:
    from unimem import _jsonl, capabilities, capability_io, incident, memory_authority, safety, unit_trials

    fp.text(occasion, "asking occasion", 128)
    unit = "ask:"+fp.text(context, "asking context", 64)+":"+fp.text(question_type, "question type", 64)
    store = _store()
    metadata = store.latest(root, "ask-policies").get(fp.digest(unit))
    if metadata is None:
        return {"decision": "which-mode", "mode": "operator-review", "randomized": False, "reason": "unconfigured"}
    actor = memory_authority.bound_writer()
    if actor[1] != "operator" and capabilities.CURRENT.get() is None:
        raise PermissionError("UniMem a planner agent requires an authenticated capability")
    # The enrolled scopes are not a caller-selected clearance. Existing owner,
    # capability, finite-policy and purpose admission still govern the artifact.
    policy = store.get(root, "ask-policies", fp.digest(unit), context=metadata["scopes"])
    if capabilities.CURRENT.get() is not None:
        delegation = capability_io.current(root)
        if not delegation or not capabilities.permits(delegation["verified"]["caveats"], namespace="ask",
                operation="write", memory_class="question", purpose=delegation["purpose"], labels=policy["scopes"]):
            raise PermissionError("UniMem planner capability does not permit this asking context")
    if actor[1]+":"+actor[0] not in policy["requesters"]:
        raise PermissionError("UniMem asking policy does not enroll this planner principal")
    activation = next((row for row in reversed(fp.journal(root, "human-asking-enablements.jsonl"))
                       if row["unit"] == unit), None)
    if (not activation or not activation["enabled"] or activation["policy_digest"] != fp.digest(policy)
            or policy["risk"] != "low-risk"):
        return {"decision": "which-mode", "mode": "safe-escalation", "randomized": False,
                "reason": "asking experiment disabled or context is not low-risk"}
    if set(policy["sources"]) & set(safety.protected(root)):
        return {"decision": "which-mode", "mode": "safe-escalation", "randomized": False,
                "reason": "context contains a protected hazard"}
    if unit_trials.trial(root, unit)["eligible_after"] > fp.now():
        return {"decision": "which-mode", "mode": "operator-review", "randomized": False,
                "reason": "registered randomization schedule is not yet effective"}
    with incident.protect(root), _jsonl.locked(fp.filename(root, "asking-budget.lock")):
        reservations = store.latest(root, "asking-reservations")
        key = fp.digest([unit, occasion])
        prior = reservations.get(key)
        if prior and prior.get("status") == "active":
            return dict(store.get(root, "asking-reservations", key, context=policy["scopes"]))
        if prior:
            raise PermissionError("UniMem an erased asking reservation cannot be reused")
        claims = {row["id"]: row for row in fp.journal(root, "human-asking-reservations.jsonl")}
        claim = claims.get(key)
        if claim is None:
            same_trial = [row for row in claims.values() if row["unit"] == unit]
            used = sum(row["reserved_seconds"] for row in claims.values())
            if len(same_trial) >= policy["trial_size"] or used+policy["cost_seconds"] > _fleet_budget(root):
                return {"decision": "which-mode", "mode": "operator-review", "randomized": False,
                        "reason": "fixed trial complete or fleet operator-time budget exhausted"}
            fp.count(len(claims), 0, store.MAX_OBJECTS-1, "fleet asking reservations")
            fp.append(root, "human-asking-reservations.jsonl", {"id": key, "unit": unit, "occasion": occasion,
                "reserved_seconds": policy["cost_seconds"], "policy_digest": fp.digest(policy),
                "scopes": policy["scopes"]})
        elif claim["policy_digest"] != fp.digest(policy):
            raise PermissionError("UniMem pending reservation belongs to a different asking policy")
        assigned = unit_trials.assign(root, unit, occasion, context=context)
        decision = {"unit": unit, "occasion": occasion, "decision": "ask" if assigned["injected"] else "dont-ask",
            "randomized": True, "ask": assigned["injected"],
            "probability": 1-_canonical(root, unit, occasion)["rate"],
            "reserved_seconds": policy["cost_seconds"], "context": context, "question_type": question_type}
        store.put(root, "asking-reservations", key, decision, scopes=policy["scopes"],
                  sources=policy["sources"], immutable=True)
        return decision


def record_asking_cost(root: str, context: str, question_type: str, occasion: str,
                       operator_seconds: int) -> dict[str, Any]:
    store = _store()
    store.operator()
    fp.count(operator_seconds, 0, 86400)
    unit = "ask:"+context+":"+question_type
    metadata = store.latest(root, "ask-policies").get(fp.digest(unit))
    if metadata is None:
        raise ValueError("UniMem asking policy is not enrolled")
    policy = store.get(root, "ask-policies", fp.digest(unit), context=metadata["scopes"])
    key = fp.digest([unit, occasion])
    decision = store.get(root, "asking-reservations", key, context=policy["scopes"])
    canonical = _canonical(root, unit, occasion)
    if canonical["outcome"] is None:
        raise ValueError("UniMem asking cost resolution requires a canonical outcome")
    body = {"unit": unit, "occasion": occasion, "operator_seconds": operator_seconds,
            "outcome_digest": fp.digest(canonical), "ask": decision["ask"],
            "overrun": operator_seconds > decision["reserved_seconds"],
            "cost_evidence": "operator-reported duration; execution time is not attested"}
    store.put(root, "asking-costs", key, body, scopes=policy["scopes"],
              sources=[store.reference("asking-reservations", key)], immutable=True)
    return body


def asking_report(root: str, context: str, question_type: str) -> dict[str, Any]:
    from unimem import rl_io

    store = _store()
    store.operator()
    unit = "ask:"+context+":"+question_type
    metadata = store.latest(root, "ask-policies").get(fp.digest(unit))
    if metadata is None:
        raise ValueError("UniMem asking policy is not enrolled")
    policy = store.get(root, "ask-policies", fp.digest(unit), context=metadata["scopes"])
    claims = [row for row in fp.journal(root, "human-asking-reservations.jsonl") if row["unit"] == unit]
    if len(claims) != policy["trial_size"]:
        return {"status": "incomplete", "registered": policy["trial_size"], "reserved": len(claims)}
    # Effect claims use the original complete-cohort causal integrity gate.
    canonical = rl_io._canonical(root, [unit])
    expected = {(unit, row["occasion"]) for row in claims}
    if set(canonical) != expected or any(row["outcome"] is None for row in canonical.values()):
        return {"status": "incomplete", "reason": "complete canonical outcomes required"}
    rows = []
    bindings = _canonical_rows(root, [unit])
    costs = store.latest(root, "asking-costs")
    overruns = 0
    for claim in claims:
        key = claim["id"]
        if costs.get(key, {}).get("status") != "active":
            return {"status": "incomplete", "reason": "operator time must be reported for both arms"}
        cost = store.get(root, "asking-costs", key, context=policy["scopes"])
        canonical_row = canonical[unit, claim["occasion"]]
        if cost["outcome_digest"] != fp.digest(bindings[unit, claim["occasion"]]):
            raise PermissionError("UniMem asking cost was bound to another canonical outcome")
        overruns += int(cost["overrun"])
        rows.append({"ask": canonical_row["injected"], "outcome": canonical_row["outcome"],
                     "probability": 1-canonical_row["rate"], "operator_seconds": cost["operator_seconds"]})
    return {"status": "complete", "unit": unit,
        **asking_estimate(rows, prior=policy["prior"], prior_strength=policy["prior_strength"], alpha=.05/64),
        "belief_prior": policy["belief_prior"], "prior_model": policy["prior_model"],
        "reserved_seconds": sum(row["reserved_seconds"] for row in claims), "reported_cost_overruns": overruns,
        "budget_scope": "reserved planner time; human overruns are reported separately"}
