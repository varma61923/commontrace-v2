"""UniMem canonical trials and review feedback for external optimizer proposals.

Candidates remain unverified data. The caller must deliver the returned arm,
execute edits only in its own sandbox, and resolve every assigned outcome.
Vendor validation scores never enter the estimator, gate or frontier.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from unimem import _jsonl, conformal_gate, holdout_io, memory_guard, promotion, safety
from unimem import frontier_protocol as fp
from unimem import nextwave_store as store
from unimem import optimizer_intake as pure
from unimem import unit_trials as trials

KIND = "optimizer-candidates"


def intake(root: str, proposal: dict[str, Any], *, format: str = "generic", context: Sequence[str] = (),
           source_ids: Sequence[str] = (), alpha: float = .05, rate: float = .5,
           training_size: int = 100, minimum_support: int = 10,
           segments: Sequence[str] = ("default",), harm_score: float = 1,
           harm_scorer: str = "unimem-delta") -> dict[str, Any]:
    store.operator()
    fp.wire(proposal)
    values = pure.adapt(format, proposal)
    fp.count(len(source_ids), 0, 64)
    fp.count(len(segments), 1, 16)
    strata = sorted({fp.text(segment, "pre-treatment segment", 64) for segment in segments})
    fp.number(harm_score, 0, 1)
    fp.text(harm_scorer, "harm scorer", 128)
    if len(strata) != len(segments):
        raise ValueError("UniMem optimizer segments must be unique and fixed before trial")
    sources = store.admit(root, source_ids, context=context)
    if set(sources) != set(source_ids):
        raise PermissionError("UniMem optimizer proposal has unavailable sources")
    safety.assert_mutable(root, source_ids)
    gate = conformal_gate.current(root)
    if not gate.get("enabled") or not gate.get("calibration_n"):
        raise ValueError("UniMem optimizer trials require frozen harm calibration on matched ungated pilot traffic")
    if not holdout_io.load_config(root).running:
        raise ValueError("UniMem configure the experiment before optimizer intake")
    result = []
    for value in values:
        safety.assert_mutable(root, [value["base_artifact"]])
        if memory_guard.scan_fields({"optimizer_delta": value["delta"]}).should_block:
            raise PermissionError("UniMem optimizer proposal failed the content-safety screen")
        data = {**value, "scopes": sorted(set(context)), "segments": strata,
                "gate_hash": fp.digest(gate), "harm_score": harm_score, "harm_scorer": harm_scorer,
                "source_digests": {identity: fp.digest(row) for identity, row in sources.items()}}
        store.put(root, KIND, value["id"], data, scopes=context, sources=source_ids, immutable=True)
        trial = trials.register(root, value["id"], value["artifact_hash"], alpha=alpha, rate=rate,
                                training_size=training_size, minimum_support=minimum_support)
        result.append({"id": value["id"], "unit_kind": value.get("unit_kind", "candidate"),
                       "format_status": value["format_status"], "schedule": trial["schedule"]})
    return {"candidates": result, "classification": "unverified proposal",
            "trial_design": "independent typed delta factors; joint delivery records every interaction",
            "execution": "caller-controlled sandbox; no proposed code executed by UniMem"}


def _load(root: str, identity: str, context: Sequence[str]) -> dict[str, Any]:
    from unimem import capability_io

    value = store.get(root, KIND, identity, context=context)
    if not capability_io.permits(root, {"kind": "optimizer-delta", "scopes": value["scopes"]}, namespace=KIND):
        raise PermissionError("UniMem optimizer candidate exceeds the leaf capability")
    sources = store.admit(root, list(value["source_digests"]), context=context)
    if any(i not in sources or fp.digest(sources[i]) != digest
           for i, digest in value["source_digests"].items()):
        raise PermissionError("UniMem optimizer source is unavailable or changed")
    if memory_guard.scan_fields({"optimizer_delta": value["delta"]}).should_block:
        raise PermissionError("UniMem optimizer candidate is no longer eligible for trial delivery")
    return value


def expose(root: str, identities: Sequence[str], occasion: str, *, context: Sequence[str] = (),
           segment: str = "default") -> dict[str, Any]:
    """Return the actual candidate/bullet catalog for this factorial occasion."""
    with _jsonl.locked(fp.filename(root, "optimizer-delivery.lock")):
        return _expose(root, identities, occasion, context=context, segment=segment)


def _expose(root: str, identities: Sequence[str], occasion: str, *, context: Sequence[str],
            segment: str) -> dict[str, Any]:
    fp.count(len(identities), 1, 64)
    if len(set(identities)) != len(identities):
        raise ValueError("UniMem optimizer catalog IDs must be unique")
    fp.text(occasion)
    gate = conformal_gate.current(root)
    catalog, design = [], []
    prior = None
    delivery_id = fp.digest(occasion)
    if delivery_id in store.latest(root, "optimizer-delivery"):
        prior = store.get(root, "optimizer-delivery", delivery_id, context=context)
        if (prior["identities"] != sorted(identities) or prior["segment"] != segment
                or prior["gate_hash"] != fp.digest(gate)):
            raise ValueError("UniMem retry must match the originally delivered factorial catalog")
    prepared = []
    for identity in sorted(identities):
        value = _load(root, identity, context)
        if segment not in value["segments"]:
            raise ValueError("UniMem optimizer segment was not preregistered")
        if fp.digest(gate) != value["gate_hash"]:
            raise PermissionError("UniMem optimizer harm calibration changed; preregister a fresh candidate")
        evidence = trials.report(root, identity)
        reason = ""
        if evidence["harm_e_value"] >= 1 / evidence["alpha"]:
            if prior is not None:
                raise PermissionError("UniMem historical catalog is no longer eligible; start a new occasion")
            reason = "canonical delta harm alarm"
        elif not conformal_gate.admit(gate, value["harm_score"], context=segment, scorer=value["harm_scorer"]):
            reason = "conformal harm gate"
        prepared.append((identity, value, reason))
    # Validate the whole catalog before publishing any assignment or exclusion.
    for identity, value, reason in prepared:
        if reason:
            if prior is None:
                trials.excluded(root, identity, occasion, reason, parent="harm-gate")
            continue
        arm = trials.assign(root, identity, occasion, context=segment)
        design.append({"unit": identity, "injected": arm["injected"], "segment": segment,
                       "propensity": 1 - trials.trial(root, identity)["design"]["rate"]})
        if arm["injected"]:
            catalog.append({"id": identity, "delta": value["delta"], "base_artifact": value["base_artifact"],
                            "presentation": "unverified proposal", "unit_kind": value.get("unit_kind", "candidate")})
    # One immutable delivered catalog names all crossed delta factors. A retry
    # must honor this same exact set, not remove a bullet after seeing outcomes.
    record = {"occasion": occasion, "segment": segment, "identities": sorted(identities), "factors": design,
              "interaction": "factorial", "gate_hash": fp.digest(gate)}
    if prior is not None and prior != record:
        raise PermissionError("UniMem retry would change the original delivered treatment")
    store.put(root, "optimizer-delivery", delivery_id, record, scopes=context, immutable=True)
    for item in catalog:
        current = _load(root, item["id"], context)  # Current source/capability admission before egress.
        if current["source_digests"]:
            from unimem import contagion, memory_authority

            contagion.record_read(root, list(current["source_digests"]), memory_authority.bound_writer()[0],
                                  occasion, store.reference(KIND, item["id"]), context=context)
    return {"catalog": catalog, "design": record, "classification": "randomized delivery",
            "assumptions": ["delivered catalog is the treatment", "complete outcomes",
                            "matched frozen harm calibration", "no cross-episode interference"]}


def _evaluate(rows: Sequence[dict[str, Any]], design: dict[str, Any]) -> dict[str, Any]:
    return promotion.evaluate(rows, {"default": 1}, {"default": 0}, alpha=design["alpha"],
                              training_size=design["training_size"], minimum_support=design["minimum_support"],
                              propensity_floor=min(design["rate"], 1-design["rate"]), margin=design["margin"])


def _report(root: str, identity: str, segment: str | None = None) -> dict[str, Any]:
    registration = trials.trial(root, identity)
    rows = trials.rows(root, identity)
    assignments, corrupt = holdout_io.read_log(root)
    expected = [r for r in assignments if r.lesson == identity and r.salt == registration["salt"]]
    complete = not corrupt and len(expected) == len(rows)
    design = registration["design"]
    selected = [row for row in rows if segment is None or row["context"] == segment]
    result = _evaluate(selected, design)
    # E-process monitor and approximation-only MDE stay canonical; segment
    # summaries are estimates, not a new family-wise promotion certificate.
    canonical = trials.report(root, identity)
    return {**canonical, **result, "complete": complete, "segment": segment or "all",
            "promote": bool(complete and result["promote"]),
            "verdict": "POSITIVE_BOUND" if complete and result["promote"] else "UNDERPOWERED",
            "classification": "estimate"}


def status(root: str, identity: str, *, context: Sequence[str] = (), certify: bool = False,
           pooled: bool = False) -> dict[str, Any]:
    if type(certify) is not bool or type(pooled) is not bool:
        raise ValueError("UniMem optimizer certify must be boolean")
    value = _load(root, identity, context)
    result = _report(root, identity)
    if pooled:
        from unimem import pooled_io

        result["pooled"] = pooled_io.report(root, value["content_hash"])
    hurt = result["harm_e_value"] >= 1/result["alpha"]
    verdict = "harmful" if hurt else "positive_bound" if result["promote"] else "insufficient_evidence"
    gate = conformal_gate.current(root)
    rows = trials.rows(root, identity)
    result["harm_budget"] = {"alpha": gate.get("alpha"), "calibration_hash": value["gate_hash"],
        "calibration_current": fp.digest(gate) == value["gate_hash"],
        "observed_ipw_failure": sum((1-row["outcome"])/(1-row["rate"])
                                    for row in rows if row["injected"]) / max(1, len(rows)),
        "classification": "estimate", "assumptions": ["matched frozen harm calibration and live traffic"]}
    certificate = None
    if certify:
        store.operator()
        if fp.digest(gate) != value["gate_hash"]:
            raise PermissionError("UniMem optimizer harm calibration changed before certification")
        if hurt or not result["promote"]:
            raise PermissionError("UniMem optimizer candidate has not cleared canonical promotion")
        certificate = trials.certify(root, identity)
    return {"candidate": value, "evidence": result, "certificate": certificate,
            "feedback": {"version": 1, "optimizer": value["optimizer"], "candidate": identity,
                         "decision": verdict, "negative_evidence": hurt,
                         "reason": "canonical randomized evidence; claimed validation score excluded",
                         "suggestion": "revise or remove the delta" if hurt else "continue a preregistered trial",
                         "prune_bullet": hurt and value.get("unit_kind") == "bullet"}}


def frontier(root: str, *, context: Sequence[str] = (), segment: str = "default") -> dict[str, Any]:
    fp.text(segment, "pre-treatment segment", 64)
    points: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for identity, metadata in sorted(store.latest(root, KIND).items()):
        if not store.visible(metadata["scopes"], context):
            continue
        value = _load(root, identity, context)
        if segment not in value["segments"]:
            continue
        fp.count(len(reports), 0, 255, "UniMem optimizer frontier candidates")
        result = _report(root, identity, segment)
        low, high = result["confidence_sequence"]
        # Projection into the binary-effect parameter space is explicit;
        # the original estimate and interval remain available in reports.
        points.append({"id": identity, "effect": max(-1.0, min(1.0, result["estimate"])),
                       "harm": max(0.0, min(1.0, -low)),
                       "supported": result["complete"] and result["n"] > 0 and result["support_coverage"] == 1})
        reports.append({"id": identity, "report": result})
    return {"frontier": pure.pareto(points), "points": points, "reports": reports,
            "segment": segment, "classification": "estimated frontier",
            "assumptions": ["complete canonical outcomes", "pre-treatment segment", "fixed trial criteria"],
            "limitations": ["frontier selection is exploratory; promotion still requires its canonical certificate"]}
