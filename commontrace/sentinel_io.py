"""Prospective canonical sentinel cohorts and fail-closed update promotion.

An operator pins each source, segment family and monitoring prefix before new
outcomes exist. Quarantine is a retained review restriction, never erasure.
Updates use independent canonical factors and return the actual delivered arm;
the caller must honor that arm and complete every outcome.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from typing import Any

from commontrace import frontier_protocol as fp
from commontrace import sentinel as pure

CONFIG = "sentinel-config"
REGISTRATIONS = "sentinel-registrations"
UPDATES = "sentinel-updates"
QUARANTINES = "sentinel-quarantines"


def _store() -> Any:
    # Import lazily: canonical writers are imported by the artifact store.
    from commontrace import nextwave_store

    return nextwave_store


def enabled(root: str) -> bool:
    return os.path.isfile(fp.filename(root, "unimem-sentinel-config.jsonl"))


def configure(root: str, *, alpha: float = .05, baseline_failure: float = .1) -> dict[str, Any]:
    store = _store()
    store.operator()
    fp.number(alpha, .001, .25)
    fp.number(baseline_failure, 0, 1)
    value = {"alpha": alpha, "baseline_failure": baseline_failure}
    store.put(root, CONFIG, "default", value, immutable=True)
    return value


def _source(root: str, source: str, context: Sequence[str]) -> tuple[dict[str, Any], str]:
    store = _store()
    if source.startswith("artifact:"):
        _, kind, identity = source.split(":", 2)
        value = store.get(root, kind, identity, context=context)
        from commontrace import capability_io

        memory_class = {"optimizer-candidates": "optimizer-delta", "foundry-skills": "skill"}.get(kind, "artifact")
        if not capability_io.permits(root, {"kind": memory_class, "scopes": value.get("scopes", [])}, namespace=kind):
            raise PermissionError("UniMem sentinel artifact exceeds the leaf capability")
        return value, identity
    admitted = store.admit(root, [source], context=context)
    if source not in admitted:
        raise PermissionError("UniMem sentinel source is unavailable or outside the caller's scope")
    return dict(admitted[source]), source


def _prefix(root: str) -> dict[str, Any]:
    from commontrace import holdout_io

    raw = fp.rows(holdout_io.holdout_log_path(root))
    return {"n": len(raw), "digest": _prefix_digest(raw), "encoding": "canonical-jsonl-sha256"}


def _prefix_digest(rows: Sequence[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(fp.wire(row)+b"\n")
    return digest.hexdigest()


def register(root: str, source: str, *, origin: str, segments: Sequence[str],
             context: Sequence[str] = ()) -> dict[str, Any]:
    from commontrace import _jsonl, holdout_io, safety, unit_trials

    store = _store()
    store.operator()
    config = store.get(root, CONFIG, "default")
    value, unit = _source(root, source, context)
    safety.assert_mutable(root, [source, unit])
    # Validate the family before writing any preregistration.
    pure.transfer([], origin=origin, segments=segments, alpha=config["alpha"])
    if source.startswith("artifact:"):
        design = unit_trials.trial(root, unit)["design"]
        if design["artifact_hash"] not in (value.get("content_hash"), value.get("artifact_hash")):
            raise PermissionError("UniMem sentinel artifact does not match its canonical trial")
    cfg = holdout_io.load_config(root)
    if not cfg.running:
        raise ValueError("UniMem sentinel requires a configured canonical experiment")
    identity = fp.digest(source)
    with _jsonl.locked(fp.filename(root, "sentinel-registration.lock")):
        prior = store.latest(root, REGISTRATIONS)
        if identity in prior:
            raise ValueError("UniMem sentinel source family is already preregistered")
        fp.count(len(prior), 0, 4094)
        index = len(prior)
        record = {"source": source, "unit": unit, "origin": origin, "segments": list(segments),
                  "source_digest": fp.digest(value), "salt": cfg.salt, "prefix": _prefix(root),
                  "alpha": config["alpha"]/((index+1)*(index+2)), "registered_at": fp.now()}
        # Scope the deletable metadata, but do not link it as a readable derived
        # memory: an operator must still inspect a quarantine after read denial.
        store.put(root, REGISTRATIONS, identity, record, scopes=value.get("scopes", context), immutable=True)
    return record


def _canonical(root: str, prefix: dict[str, Any], salt: str, units: Sequence[str], *,
               limit: int | None = None) -> list[dict[str, Any]]:
    from commontrace import allocation, holdout_io, integrity, transparency_log
    from commontrace.commands.experiment_cmd import _load

    raw = fp.rows(holdout_io.holdout_log_path(root))
    count = prefix["n"]
    if len(raw) < count or _prefix_digest(raw[:count]) != prefix["digest"]:
        raise PermissionError("UniMem sentinel preregistered assignment prefix changed")
    loaded, _, corrupt = _load(root)
    selected = [row for row in loaded[count:] if row.salt == salt and row.lesson in units]
    cohort_raw = [row for row in raw[count:] if row["salt"] == salt and row["lesson"] in units]
    if limit is not None:
        fp.count(limit, 1, fp.MAX_ROWS, "fixed canonical cohort horizon")
        selected, cohort_raw = selected[:limit], cohort_raw[:limit]
    if corrupt or len(loaded) != len(raw) or not integrity.audit(selected, schedules=allocation.history(root)).readable:
        raise PermissionError("UniMem sentinel canonical assignment integrity check failed")
    if holdout_io.load_config(root).salt != salt:
        raise PermissionError("UniMem sentinel randomization salt changed")
    if any(row.succeeded is None for row in selected):
        raise ValueError("UniMem sentinel requires every assigned cohort outcome before a release")
    transparency_log.head(root)
    assignments: set[str] = set()
    outcomes: set[tuple[str, bool]] = set()
    for leaf in fp.journal(root, transparency_log.LEAVES):
        event = leaf["statement"]["record"]
        if event["event"] == "holdout-assignment":
            assignments.update(event["statement"]["assignments"].values())
        elif event["event"] == "holdout-outcome":
            outcomes.add((event["statement"]["occasion_id"], event["statement"]["succeeded"]))
    if any(fp.digest(row) not in assignments for row in cohort_raw):
        raise PermissionError("UniMem sentinel assignment is not anchored in canonical transparency history")
    if any((row.occasion_id, bool(row.succeeded)) not in outcomes for row in selected):
        raise PermissionError("UniMem sentinel outcome is not anchored in canonical transparency history")
    return [{"unit": row.lesson, "occasion_id": row.occasion_id, "injected": row.injected,
             "outcome": int(bool(row.succeeded)), "rate": row.rate, "context": row.context or "default"}
            for row in selected]


def blocked(root: str, identity: str) -> bool:
    if not enabled(root):
        return False
    from commontrace import lineage, safety

    if identity in safety.protected(root):
        return False  # Mandatory hazard context remains explicit under review.

    targets = {row["target"] for row in _store().events(root, QUARANTINES)}
    return bool(targets.intersection([identity, *lineage.ancestors(root, identity)]))


def _registration(root: str, source: str, context: Sequence[str]) -> dict[str, Any]:
    from commontrace import capability_io

    store = _store()
    identity = fp.digest(source)
    value = dict(store.get(root, REGISTRATIONS, identity, context=context))
    scopes = store.latest(root, REGISTRATIONS)[identity]["scopes"]
    if not capability_io.permits(root, {"kind": "sentinel-status", "scopes": scopes}, namespace=REGISTRATIONS):
        raise PermissionError("UniMem sentinel status exceeds the leaf capability")
    return value


def status(root: str, source: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    registration = _registration(root, source, context)
    if blocked(root, registration["unit"]):
        return {"source": source, "quarantine": True, "classification": "retained operator review restriction"}
    value, _ = _source(root, source, context)
    if fp.digest(value) != registration["source_digest"]:
        raise PermissionError("UniMem sentinel source changed; this cohort cannot authorize generalization")
    rows = _canonical(root, registration["prefix"], registration["salt"], [registration["unit"]])
    report = pure.transfer(rows, origin=registration["origin"], segments=registration["segments"],
                           alpha=registration["alpha"])
    drift_rows = [{"pathway": name, "occasion_id": row["occasion_id"], "failure": 1-row["outcome"]}
                  for row in rows for name in pure.PATHWAYS if row["context"] == name]
    config = _store().get(root, CONFIG, "default")
    return {"source": source, **report,
            "drift": pure.drift(drift_rows, baseline_failure=config["baseline_failure"], alpha=registration["alpha"]),
            "drift_note": "pathway labels are frozen pre-treatment contexts; alarms do not attribute a cause"}


def quarantine(root: str, source: str, *, context: Sequence[str] = (), incident_id: str = "") -> dict[str, Any]:
    from commontrace import change_stream, contagion, incident, lineage, safety

    store = _store()
    store.operator()
    registration = _registration(root, source, context)
    result = status(root, source, context=context)
    if not result["quarantine"]:
        return result
    if blocked(root, registration["unit"]):
        return result
    safety.assert_mutable(root, [source, registration["unit"]])
    # Capture only observed admitted dependencies BEFORE restricting reads.
    radius = (contagion.radius(root, source, context=context) if not source.startswith("artifact:")
              else {"source": source, "nodes": [], "truncated": False, "classification": "artifact lineage only"})
    if incident_id:
        incident.freeze(root, incident_id, fp.now())
    scopes = store.latest(root, REGISTRATIONS)[fp.digest(source)]["scopes"]
    for target in sorted({source, registration["unit"]}):
        store.event(root, QUARANTINES, fp.digest(target),
                    {"target": target, "scopes": scopes, "evidence_digest": fp.digest(result),
                     "radius_digest": fp.digest(radius), "incident": incident_id})
        lineage.taint(root, target, "UniMem sentinel transfer harm: review descendants before reuse")
        if change_stream.enabled(root):
            change_stream.publish(root, "quarantine", target, scopes=scopes,
                                  event_id="sentinel:"+fp.digest(target))
    return {**result, "radius": radius, "incident": incident_id}


def assert_generalize(root: str, sources: Sequence[str] | None = None, *,
                      context: Sequence[str] | None = None) -> None:
    if not enabled(root):
        return
    store = _store()
    if context is None:
        store.operator()  # Global operator jobs must also check private families.
    requested = set(sources) if sources is not None else None
    if requested is not None:
        from commontrace import lineage

        pending = list(requested)
        while pending:
            fp.count(len(requested), 0, 2000, "UniMem generalization source graph")
            identity = pending.pop()
            parents = set(lineage.ancestors(root, identity))
            if identity.startswith("artifact:"):
                _, kind, key = identity.split(":", 2)
                metadata = store.latest(root, kind).get(key, {})
                if context is not None and not store.visible(metadata.get("scopes", []), context):
                    raise PermissionError("UniMem generalization source is outside the caller's scope")
                parents.update(metadata.get("sources", []))
            fresh = parents-requested
            requested.update(fresh)
            pending.extend(fresh)
    for identity, metadata in store.latest(root, REGISTRATIONS).items():
        labels = list(context) if context is not None else metadata["scopes"]
        if not store.visible(metadata["scopes"], labels):
            continue
        registration = store.get(root, REGISTRATIONS, identity, context=labels)
        if requested is not None and not requested.intersection((registration["source"], registration["unit"])):
            continue
        report = status(root, registration["source"], context=labels)
        if report["quarantine"]:
            # Delegates may never cause operator state changes; they still fail
            # closed. The operator can persist quarantine through the CLI.
            from commontrace import capabilities, memory_authority

            if capabilities.CURRENT.get() is None and memory_authority.bound_writer()[1] == "operator":
                quarantine(root, registration["source"], context=labels)
            raise PermissionError("UniMem sentinel transfer quarantine blocks generalization")


def ledger(root: str, action: str = "status", *, update: str = "", payload: Any = None,
           canary: str = "", certificate: dict[str, Any] | None = None, signer: str = "",
           tolerance: float = .1, alpha: float = .05, minimum_support: int = 5,
           context: Sequence[str] = (), source_ids: Sequence[str] = (), occasion: str = "",
           policy_logprobs: Sequence[float] | None = None,
           base_logprobs: Sequence[float] | None = None, kl_budget: float = .1) -> dict[str, Any]:
    """Register, expose or inspect an immutable independently randomized update."""
    from commontrace import holdout_io, memory_guard, promotion, rl_export, safety, unit_trials

    store = _store()
    if action == "register":
        store.operator()
        if not enabled(root) or payload is None or certificate is None:
            raise ValueError("UniMem enable the sentinel and provide actual payload and a proven canary")
        digest = fp.digest(payload)
        if memory_guard.scan_fields({"update": payload}).should_block:
            raise PermissionError("UniMem update failed its content-safety screen")
        identity = "update:"+digest
        if identity in store.latest(root, UPDATES):
            raise ValueError("UniMem update payload is already bound to an immutable trial")
        if update and update != identity:
            raise ValueError("UniMem update identity must bind its exact payload")
        material, canary_unit = _source(root, canary, context)
        fp.count(len(source_ids), 0, 64)
        if set(store.admit(root, source_ids, context=context)) != set(source_ids):
            raise PermissionError("UniMem update has unavailable sources")
        safety.assert_mutable(root, [canary, canary_unit, identity])
        trial = unit_trials.trial(root, canary_unit)
        # Canary certificates can precede enablement, so check the content
        # dependency graph as well as the unit ID before adding a new edge.
        parent_hash = trial["design"]["artifact_hash"]
        seen = {digest}
        for _ in range(16):
            if parent_hash in seen:
                raise ValueError("UniMem update and certified canary must have acyclic distinct content")
            seen.add(parent_hash)
            parent_id = "update:"+parent_hash
            if parent_id not in store.latest(root, UPDATES):
                break
            parent = store.get(root, UPDATES, parent_id, context=context)
            parent_hash = unit_trials.trial(root, parent["canary_unit"])["design"]["artifact_hash"]
        else:
            raise ValueError("UniMem canary dependency chain exceeds sixteen updates")
        if (not canary.startswith("artifact:") or trial["design"]["artifact_hash"] not in
                (material.get("content_hash"), material.get("artifact_hash"))
                or not promotion.verify(root, certificate, artifact_hash=trial["design"]["artifact_hash"])):
            raise PermissionError("UniMem forgetting canary requires a current content-bound canonical certificate")
        pure.interference([], tolerance=tolerance, alpha=alpha, minimum_support=minimum_support)
        value = {"id": identity, "artifact_hash": digest, "payload": payload, "signer": fp.text(signer),
                 "provenance": "operator-pinned digest and signer; external signer attestation not inferred",
                 "canary": canary, "canary_unit": canary_unit, "canary_digest": fp.digest(material),
                 "certificate": certificate, "prefix": _prefix(root), "salt": trial["salt"],
                 "tolerance": tolerance, "alpha": alpha, "minimum_support": minimum_support,
                 "kl": rl_export.token_kl(policy_logprobs, base_logprobs, kl_budget)}
        unit_trials.register(root, identity, digest, alpha=alpha)
        store.put(root, UPDATES, identity, value, scopes=context,
                  sources=sorted({canary, *source_ids}), immutable=True)
        return {"update": identity, "artifact_hash": digest, "provenance": value["provenance"]}
    store.operator()
    value = store.get(root, UPDATES, update, context=context)
    material, _ = _source(root, value["canary"], context)
    trial = unit_trials.trial(root, value["canary_unit"])
    if (fp.digest(material) != value["canary_digest"] or
            not promotion.verify(root, value["certificate"], artifact_hash=trial["design"]["artifact_hash"])):
        raise PermissionError("UniMem update canary changed or lost its promotion certificate")
    if action == "expose":
        from datetime import datetime

        from commontrace import _jsonl, allocation

        fp.text(occasion)
        with _jsonl.locked(fp.filename(root, "sentinel-factorial.lock")):
            # Validate both immutable factors before issuing either arm.
            for unit in (update, value["canary_unit"]):
                design = unit_trials.trial(root, unit)
                cfg = holdout_io.load_config(root)
                schedule = allocation.active(root, cfg.salt, datetime.fromisoformat(fp.now()))
                if (cfg.salt != design["salt"] or not cfg.running or design["eligible_after"] > fp.now()
                        or schedule is None or schedule.rate_for(unit, "sentinel") != design["design"]["rate"]):
                    raise PermissionError("UniMem update factor is no longer eligible")
            arms = {unit: unit_trials.assign(root, unit, occasion, context="sentinel")
                    for unit in (update, value["canary_unit"])}
            record = {"occasion": occasion, "update": update, "canary": value["canary_unit"],
                      "arms": arms, "design": "independent factorial"}
            store.put(root, "sentinel-deliveries", fp.digest([update, occasion]), record,
                      scopes=context, immutable=True)
        return {"design": record, "update_payload": value["payload"] if arms[update]["injected"] else None,
                "canary_payload": material if arms[value["canary_unit"]]["injected"] else None,
                "instruction": "honor both returned arms and record the actual common outcome"}
    if action != "status":
        raise ValueError("UniMem sentinel ledger action must be register, expose or status")
    raw = _canonical(root, value["prefix"], value["salt"], [update, value["canary_unit"]])
    by_occasion: dict[str, dict[str, dict[str, Any]]] = {}
    for row in raw:
        by_occasion.setdefault(row["occasion_id"], {})[row["unit"]] = row
    rows = []
    for identity, pair in by_occasion.items():
        # Other canary use is not an update trial. Conversely, an update
        # assignment without an immutable joint delivery cannot be certified.
        if update not in pair:
            continue
        delivery = store.get(root, "sentinel-deliveries", fp.digest([update, identity]), context=context)
        if value["canary_unit"] not in pair or set(pair) != {update, value["canary_unit"]}:
            raise PermissionError("UniMem update lacks a complete actual paired canary assignment")
        u, c = pair[update], pair[value["canary_unit"]]
        if (u["context"] != "sentinel" or c["context"] != "sentinel" or u["outcome"] != c["outcome"] or
                any(delivery["arms"][unit]["injected"] != item["injected"] for unit, item in pair.items())):
            raise PermissionError("UniMem update trial differs from its delivered factorial catalog")
        rows.append({"occasion_id": identity, "update": u["injected"], "canary": c["injected"],
                     "outcome": u["outcome"], "update_rate": u["rate"], "canary_rate": c["rate"]})
    result = pure.interference(rows, tolerance=value["tolerance"], alpha=value["alpha"],
                               minimum_support=value["minimum_support"])
    return {"update": update, "artifact_hash": value["artifact_hash"], "signer": value["signer"],
            "kl_to_base": value["kl"], **result,
            "allow": result["allow"] and value["kl"]["status"] != "over sampled budget"}


def require_update(root: str, artifact_hash: str, *, context: Sequence[str] | None = None) -> None:
    """An additional necessary gate; the canonical promotion test still applies."""
    if not enabled(root):
        return
    store = _store()
    store.operator()
    metadata = store.latest(root, UPDATES)
    labels = list(context) if context is not None else metadata.get("update:"+artifact_hash, {}).get("scopes", [])
    matches = [identity for identity, row in metadata.items()
               if identity == "update:"+artifact_hash and store.visible(row["scopes"], labels)]
    if len(matches) != 1 or not ledger(root, update=matches[0], context=labels)["allow"]:
        raise PermissionError("UniMem update requires a supported non-regression canary bound before promotion")
