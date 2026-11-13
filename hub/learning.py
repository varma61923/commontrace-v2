"""Measured learning backed exclusively by the tenant's canonical SQL authority."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import StrictBool, StrictFloat, StrictInt
from sqlalchemy import any_, bindparam, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert

from hub import audit, auth, delegation, policies
from hub.db import session_scope
from hub.models import (
    LearningDecision,
    LearningExperiment,
    LearningOccasion,
    LearningWork,
    MemoryRecordProjection,
    Organization,
    Trace,
)
from unimem import learning_contract as contract
from unimem import revision
from unimem.async_workers import STORE_WORKERS, WorkerCapacityError
from unimem.memory_record import MemoryRecord
from unimem.record_visibility import now, scope_from_labels, visible

logger = logging.getLogger(__name__)
_ADMIN = frozenset(("configure", "run", "rollback"))


def _error(message, *, status=409, code="conflict"):
    error = contract.LearningError(message)
    error.status_code, error.code = status, code
    return error


async def _lock(session, org_id):
    # All learning mutations in a tenant share a short transaction lock. Sources
    # remain SQL authority; no state is copied into per-tenant local files.
    await session.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())


async def _experiment(session, org_id, experiment_id=None):
    stmt = select(LearningExperiment).where(LearningExperiment.org_id == org_id)
    stmt = (
        stmt.where(LearningExperiment.id == experiment_id)
        if experiment_id
        else stmt.where(LearningExperiment.active.is_(True))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _error("learning is not configured", status=404, code="not_configured")
    return row


async def _sources(session, session_factory, org_id, identities, scope, *, allow_missing=False):
    from hub.memory_records import PostgresJournal

    context = {**scope, "tenant": org_id}
    policy = await policies.active(session, org_id)
    at = datetime.now(timezone.utc)
    trace_ids = []
    for identity in identities:
        try:
            trace_ids.append(str(uuid.UUID(identity)))
        except ValueError:
            pass
    traces = (
        (
            await session.execute(
                select(Trace)
                .where(
                    Trace.org_id == org_id,
                    # Worker cohorts combine many individually bounded assignments.
                    # One typed array avoids asyncpg's 32,767-argument ceiling.
                    Trace.id == any_(bindparam(None, trace_ids, type_=ARRAY(Trace.id.type))),
                    Trace.quarantined.is_(False),
                    Trace.superseded_at.is_(None),
                    delegation.trace_predicate(),
                    policies.trace_predicate(policy),
                    or_(Trace.valid_from.is_(None), Trace.valid_from <= at),
                    or_(Trace.valid_until.is_(None), Trace.valid_until > at),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    labels = set(context.get("labels", []))
    result = {}
    for row in traces:
        if row.scopes and not set(row.scopes) <= labels:
            continue
        result[row.id] = {
            "id": row.id,
            "text": "\n\n".join((row.title, row.context_text, row.solution_text)),
            "revision": revision.revision_of_trace(row.title, row.context_text, row.solution_text, row.tags),
            "protected": bool(row.extensions.get("protected", False)),
        }
    records = (
        (
            await session.execute(
                select(MemoryRecordProjection)
                .where(
                    MemoryRecordProjection.org_id == org_id,
                    MemoryRecordProjection.id
                    == any_(bindparam(None, identities, type_=ARRAY(MemoryRecordProjection.id.type))),
                    delegation.record_predicate(),
                    MemoryRecordProjection.scope.op("<@")(context),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if records:
        allowed = await PostgresJournal(session_factory, org_id)._cap_allowed_ids(
            session, context, now(), (), identities=[row.id for row in records]
        )
        for row in records:
            record = MemoryRecord(**row.record)
            if row.id not in allowed or not visible(record, context):
                continue
            if row.id in result:
                raise _error("source identifier is ambiguous")
            value = record.payload.get("text", record.payload.get("content", record.payload.get("body")))
            if not isinstance(value, str):
                value = json.dumps(record.payload, sort_keys=True, allow_nan=False)
            result[row.id] = {
                "id": row.id,
                "text": value,
                "revision": contract.digest(record.to_dict()),
                "protected": contract.protected_memory(record.kind, record.payload),
            }
    if not allow_missing and len(result) != len(identities):
        raise contract.LearningUnavailable("one or more sources are unavailable in this scope")
    return [result[identity] for identity in identities if identity in result]


def _public(assignment, sources=None):
    result = {key: value for key, value in assignment.items() if key != "sources"}
    if sources is not None:
        result["memories"] = [
            {key: source[key] for key in ("id", "text", "revision")}
            for source in sources
            if source["id"] in assignment["inject"]
        ]
    return contract.response(**result)


async def _latest_decision(session, org_id, experiment_id):
    return (
        await session.execute(
            select(LearningDecision)
            .where(LearningDecision.org_id == org_id, LearningDecision.experiment_id == experiment_id)
            .order_by(LearningDecision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _enqueue(session, org_id, experiment_id):
    await session.execute(
        insert(LearningWork)
        .values(org_id=org_id, experiment_id=experiment_id, pending=True, generation=1)
        .on_conflict_do_update(
            index_elements=["org_id", "experiment_id"],
            set_={"pending": True, "generation": LearningWork.generation + 1, "lease_token": None, "lease_until": None},
        )
    )


async def operate(session_factory, org_id, principal, operation, payload=None, *, scope=None, admin=False):
    token = policies.ACTOR.set(principal)
    try:
        return await _operate(session_factory, org_id, principal, operation, payload, scope=scope, admin=admin)
    finally:
        policies.ACTOR.reset(token)


async def _operate(session_factory, org_id, principal, operation, payload=None, *, scope=None, admin=False):
    """Execute one operation with trusted org/principal/scope separate from user JSON."""
    payload = {} if payload is None else payload
    contract.validate_request(operation, payload)
    if not isinstance(principal, str) or not principal or len(principal) > 256:
        raise PermissionError("an authenticated principal is required")
    if operation in _ADMIN and not admin:
        raise PermissionError("learning administration requires an owner/admin")
    if operation == "capabilities":
        return contract.capabilities()
    scope = {**(scope or {}), "tenant": org_id}
    MemoryRecord(id="scope-validation", kind="fact", version=1, payload={}, scope=scope)
    if "labels" in scope:
        scope["labels"] = sorted(set(scope["labels"]))
    async with session_scope(session_factory, org_id=org_id) as session:
        await _lock(session, org_id)
        if operation == "configure":
            config = contract.validate_config(payload)
            experiment_id = str(uuid.uuid4())
            current = (
                await session.execute(
                    select(LearningExperiment).where(
                        LearningExperiment.org_id == org_id, LearningExperiment.active.is_(True)
                    )
                )
            ).scalar_one_or_none()
            config_revision = current.config["revision"] + 1 if current else 1
            config = {**config, "experiment_id": experiment_id, "revision": config_revision, "salt": uuid.uuid4().hex}
            await session.execute(
                update(LearningExperiment).where(LearningExperiment.org_id == org_id).values(active=False)
            )
            session.add(LearningExperiment(org_id=org_id, id=experiment_id, config=config, active=True))
            session.add(
                LearningDecision(
                    org_id=org_id, experiment_id=experiment_id, revision=0, decision={"policy": {}, "report": {}}
                )
            )
            await audit.record(
                session,
                actor=principal,
                action="learning.configure",
                org_id=org_id,
                target_type="learning_experiment",
                target_id=experiment_id,
                summary=f"mode={config['mode']}; config_revision={config_revision}",
            )
            return contract.response(experiment_id=experiment_id, config_revision=config_revision, config=config)
        experiment = await _experiment(session, org_id, payload.get("experiment_id"))
        config = experiment.config
        latest = await _latest_decision(session, org_id, experiment.id)
        if operation == "status":
            count, outcomes = (
                await session.execute(
                    select(
                        func.count(), func.count().filter(LearningOccasion.evidence["outcome"].astext.is_not(None))
                    ).where(
                        LearningOccasion.org_id == org_id,
                        LearningOccasion.experiment_id == experiment.id,
                        LearningOccasion.principal == principal,
                        LearningOccasion.evidence["scope"] == scope,
                    )
                )
            ).one()
            work = await session.get(LearningWork, (org_id, experiment.id))
            policy, report = await _scoped_status(
                session, session_factory, org_id, principal, experiment, latest, scope, admin
            )
            return contract.response(
                experiment_id=experiment.id,
                config=config,
                decision_revision=latest.revision if latest else 0,
                config_revision=config["revision"],
                policy=policy,
                report=report,
                occasions=count,
                outcomes=outcomes,
                pending_work=bool(work and work.pending and (admin or count)),
                failures=[work.failure] if work and work.failure else [],
            )
        if operation == "run":
            result = await _evaluate(session, org_id, experiment, limit=payload.get("limit", 10000))
            await audit.record(
                session,
                actor=principal,
                action="learning.run",
                org_id=org_id,
                target_type="learning_experiment",
                target_id=experiment.id,
                summary=(
                    f"applied={result.get('applied', False)}; "
                    f"decision_revision={result.get('decision_revision', latest.revision if latest else 0)}; "
                    f"evidence={result.get('evidence', 0)}; blocked={len(result.get('blocked', []))}"
                ),
            )
            return result
        if operation == "rollback":
            target = payload.get("decision_revision", max(0, (latest.revision if latest else 0) - 1))
            stmt = select(LearningDecision).where(
                LearningDecision.org_id == org_id,
                LearningDecision.experiment_id == experiment.id,
                LearningDecision.revision == target,
            )
            previous = (
                await session.execute(stmt.order_by(LearningDecision.revision.desc()).limit(1))
            ).scalar_one_or_none()
            if previous is None:
                raise _error("rollback policy revision is unknown", status=404, code="not_found")
            restored = copy.deepcopy(previous.decision)
            restored.update(reason=payload["reason"], rollback_of=latest.revision if latest else 0)
            number = (latest.revision if latest else 0) + 1
            session.add(
                LearningDecision(org_id=org_id, experiment_id=experiment.id, revision=number, decision=restored)
            )
            await audit.record(
                session,
                actor=principal,
                action="learning.rollback",
                org_id=org_id,
                target_type="learning_experiment",
                target_id=experiment.id,
                summary=f"decision_revision={number}; restored_revision={target}",
            )
            return contract.response(
                experiment_id=experiment.id,
                decision_revision=number,
                restored_revision=previous.revision if previous else 0,
                policy=restored["policy"],
            )
        identity = (org_id, experiment.id, principal, payload["occasion_id"])
        occasion = await session.get(LearningOccasion, identity)
        if operation == "assign":
            canonical_request = {
                "occasion_id": payload["occasion_id"],
                "memory_ids": payload["memory_ids"],
                "task_context": payload.get("task_context", ""),
            }
            sources = await _sources(session, session_factory, org_id, payload["memory_ids"], scope or {})
            if occasion:
                evidence = occasion.evidence
                if (
                    evidence.get("request") != canonical_request
                    or evidence.get("scope") != (scope or {})
                    or evidence.get("source_digest") != contract.digest(sources)
                ):
                    raise _error("occasion assignment conflicts with its original request or sources")
                return _public(evidence["assignment"], sources)
            assignment = contract.make_assignment(
                config,
                contract.digest([principal, payload["occasion_id"]]),
                sources,
                task_context=payload.get("task_context", ""),
                policy=latest.decision.get("policy") if latest else None,
            )
            assignment.update(
                occasion_id=payload["occasion_id"], at=now(), policy_revision=latest.revision if latest else 0
            )
            stored_assignment = copy.deepcopy(assignment)
            stored_assignment["sources"] = [
                {key: value for key, value in source.items() if key != "text"} for source in sources
            ]
            evidence = {
                "request": canonical_request,
                "source_digest": contract.digest(sources),
                "assignment": stored_assignment,
                "delivery": None,
                "outcome": None,
                "scope": scope or {},
                "at": now(),
            }
            session.add(
                LearningOccasion(
                    org_id=org_id,
                    experiment_id=experiment.id,
                    principal=principal,
                    occasion_id=payload["occasion_id"],
                    evidence=evidence,
                )
            )
            return _public(assignment, sources)
        if occasion is None or occasion.evidence.get("scope") != scope:
            raise _error("occasion assignment does not exist", status=404, code="not_found")
        evidence = copy.deepcopy(occasion.evidence)
        if operation == "delivery":
            selected = evidence["assignment"]["inject"]
            if not set(payload["memory_ids"]) <= set(selected):
                raise _error(
                    "delivery contains a memory outside the assigned injection",
                    status=400,
                    code="invalid_learning_request",
                )
            sources = await _sources(session, session_factory, org_id, evidence["request"]["memory_ids"], scope or {})
            if contract.digest(sources) != evidence["source_digest"]:
                raise _error("sources changed since assignment", code="stale_source")
            delivery = sorted(payload["memory_ids"])
            if evidence["delivery"] is not None and evidence["delivery"] != delivery:
                raise _error("delivery conflicts with its first receipt")
            evidence["delivery"] = delivery
        elif operation == "outcome":
            outcome = contract.normalize_outcome(config, contract.digest([principal, payload["occasion_id"]]), payload)
            if evidence["outcome"] is not None and evidence["outcome"] != outcome:
                raise _error("outcome conflicts with its first receipt")
            evidence["outcome"] = outcome
        else:
            raise _error("unknown learning operation", status=404, code="not_found")
        changed = occasion.evidence != evidence
        occasion.evidence = evidence
        if changed:
            await _enqueue(session, org_id, experiment.id)
        return _public({**evidence["assignment"], "delivery": evidence["delivery"], "outcome": evidence["outcome"]})


async def _scoped_status(session, session_factory, org_id, principal, experiment, latest, scope, admin):
    policy = copy.deepcopy(latest.decision.get("policy", {})) if latest else {}
    report = copy.deepcopy(experiment.report)
    if admin:
        return policy, report
    rows = (
        (
            await session.execute(
                select(LearningOccasion.evidence["assignment"])
                .where(
                    LearningOccasion.org_id == org_id,
                    LearningOccasion.experiment_id == experiment.id,
                    LearningOccasion.principal == principal,
                    LearningOccasion.evidence["scope"] == scope,
                )
                .limit(10001)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > 10000:
        return {}, {"decisions": [], "blocked": ["status_budget_exceeded"], "evidence": 0, "stale_sources": []}
    identities = sorted({arm["id"] for row in rows for arm in row["arms"]})
    current = (
        await _sources(session, session_factory, org_id, identities, scope, allow_missing=True) if identities else []
    )
    pairs = {(source["id"], source["revision"]) for source in current}
    keys = {
        contract.policy_key(arm["id"], arm["revision"], row["task_context"])
        for row in rows
        for arm in row["arms"]
        if (arm["id"], arm["revision"]) in pairs
    }
    policy = {key: value for key, value in policy.items() if key in keys}
    if report is not None:
        # A policy key is shared across principals. Matching a caller's key does
        # not authorize its pooled outcomes, intervals, counts or future report
        # fields. Only administrators can inspect the aggregate experiment.
        report = {
            "decisions": [],
            "blocked": [],
            "evidence": len(rows),
            "stale_sources": sorted(
                {arm["id"] for row in rows for arm in row["arms"] if (arm["id"], arm["revision"]) not in pairs}
            ),
        }
    return policy, report


async def _settle_work(session, org_id, experiment_id, *, failure=None):
    work = await session.get(LearningWork, (org_id, experiment_id))
    if work:
        work.pending, work.failure = False, failure
        work.lease_token, work.lease_until = None, None


async def _evaluate(session, org_id, experiment, *, limit=10000):
    rows = (
        (
            await session.execute(
                select(LearningOccasion)
                .where(LearningOccasion.org_id == org_id, LearningOccasion.experiment_id == experiment.id)
                .order_by(LearningOccasion.created_at, LearningOccasion.principal, LearningOccasion.occasion_id)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    if len(rows) > limit:
        work = await session.get(LearningWork, (org_id, experiment.id))
        if work:
            work.failure = "evidence_budget_exceeded"
            if limit == 10000:
                await _settle_work(session, org_id, experiment.id, failure="evidence_budget_exceeded")
        return contract.response(
            experiment_id=experiment.id, applied=False, blocked=["evidence_budget_exceeded"], evidence=len(rows)
        )
    assignments = []
    for row in rows:
        item = {**row.evidence["assignment"], "delivery": row.evidence["delivery"], "outcome": row.evidence["outcome"]}
        item["occasion_id"] = contract.digest([row.principal, row.occasion_id])
        assignments.append(item)
    # Revalidate canonical SQL authority in batches per original principal scope.
    groups = {}
    for row in rows:
        context = row.evidence.get("scope", {})
        key = contract.digest([context, row.principal])
        group = groups.setdefault(key, {"scope": context, "principal": row.principal, "sources": {}, "keys": {}})
        for source in row.evidence["assignment"]["sources"]:
            group["sources"].setdefault(source["id"], set()).add(source["revision"])
            cohort = contract.policy_key(source["id"], source["revision"], row.evidence["assignment"]["task_context"])
            group["keys"][cohort] = (source["id"], source["revision"])
    stale, denied_keys = set(), set()
    for group in groups.values():
        originals = group["sources"]
        token = policies.ACTOR.set(group["principal"])
        try:
            current = await _sources(session, session.bind, org_id, list(originals), group["scope"], allow_missing=True)
        finally:
            policies.ACTOR.reset(token)
        present = {source["id"]: source["revision"] for source in current}
        denied_keys.update(
            key for key, (identity, version) in group["keys"].items() if present.get(identity) != version
        )
        stale.update(
            identity
            for identity, versions in originals.items()
            if present.get(identity) not in versions or len(versions) != 1
        )
    checkpoint = contract.digest([assignments, sorted(stale), sorted(denied_keys)])
    latest = await _latest_decision(session, org_id, experiment.id)
    if experiment.checkpoint == checkpoint:
        await _settle_work(session, org_id, experiment.id)
        return contract.response(
            experiment_id=experiment.id,
            applied=False,
            decision_revision=latest.revision if latest else 0,
            **(experiment.report or {}),
        )
    report = await STORE_WORKERS.run(contract.evaluate, experiment.config, assignments)
    report["stale_sources"] = sorted(stale)
    if stale:
        report["blocked"] = sorted(set(report["blocked"]) | {"stale_sources"})
    policy = copy.deepcopy(latest.decision.get("policy", {})) if latest else {}
    before = copy.deepcopy(policy)
    if experiment.config["mode"] == "adaptive":
        for entry in report["decisions"]:
            if entry["key"] not in denied_keys and entry["verdict"] in (
                "HELPS",
                "HURTS",
                "NO_MEASURABLE_EFFECT",
            ):
                policy[entry["key"]] = {"rate": entry["rate"], "disabled": entry["disabled"]}
    changed = policy != before
    number = (latest.revision if latest else 0) + int(changed)
    if changed:
        session.add(
            LearningDecision(
                org_id=org_id,
                experiment_id=experiment.id,
                revision=number,
                decision={"policy": policy, "report": report},
            )
        )
        await audit.record(
            session,
            actor=policies.ACTOR.get(),
            action="learning.policy_update",
            org_id=org_id,
            target_type="learning_experiment",
            target_id=experiment.id,
            summary=f"decision_revision={number}; policy_entries={len(policy)}",
        )
    experiment.checkpoint, experiment.report = checkpoint, report
    await _settle_work(session, org_id, experiment.id)
    return contract.response(experiment_id=experiment.id, applied=changed, decision_revision=number, **report)


async def claim_work(session_factory, *, lease_seconds=60):
    """Short lease acquisition; generation and token fence resumed workers."""
    at = datetime.now(timezone.utc)
    async with session_scope(session_factory, operator=True) as session:
        row = (
            await session.execute(
                select(LearningWork)
                .where(
                    LearningWork.pending.is_(True),
                    or_(LearningWork.lease_until.is_(None), LearningWork.lease_until <= at),
                )
                .order_by(LearningWork.org_id, LearningWork.experiment_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.lease_token = uuid.uuid4().hex
        row.lease_until = at + timedelta(seconds=lease_seconds)
        return (row.org_id, row.experiment_id, row.generation, row.lease_token)


async def complete_work(session_factory, claim):
    org_id, experiment_id, generation, token = claim
    async with session_scope(session_factory, org_id=org_id) as session:
        await _lock(session, org_id)
        work = await session.get(LearningWork, (org_id, experiment_id), with_for_update=True)
        if (
            not work
            or work.generation != generation
            or work.lease_token != token
            or work.lease_until <= datetime.now(timezone.utc)
        ):
            return False
        result = await _evaluate(session, org_id, await _experiment(session, org_id, experiment_id))
        return "evidence_budget_exceeded" not in result.get("blocked", [])


async def _record_worker_failure(session_factory, claim, *, capacity, error_type, interval_seconds):
    async with session_scope(session_factory, org_id=claim[0]) as session:
        await session.execute(
            update(LearningWork)
            .where(
                LearningWork.org_id == claim[0],
                LearningWork.experiment_id == claim[1],
                LearningWork.generation == claim[2],
                LearningWork.lease_token == claim[3],
            )
            .values(
                **(
                    {
                        "failure": "worker_capacity",
                        "lease_token": None,
                        "lease_until": datetime.now(timezone.utc) + timedelta(seconds=interval_seconds),
                    }
                    if capacity
                    else {"failure": error_type}
                )
            )
        )


async def _worker_pause(stop_event, interval_seconds):
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
    except asyncio.TimeoutError:
        pass


async def run_worker(session_factory, *, stop_event, interval_seconds=5, health=None):
    state = {} if health is None else health
    state.update(running=True, healthy=False)
    state.setdefault("failures", 0)
    pending_failure = None
    try:
        while not stop_event.is_set():
            claim = None
            try:
                # Failure recording has its own retry obligation. A simultaneous
                # evaluation/database failure must neither kill the worker nor
                # overwrite a newer generation when connectivity returns.
                if pending_failure is not None:
                    failed_claim, capacity, error_type = pending_failure
                    await _record_worker_failure(
                        session_factory,
                        failed_claim,
                        capacity=capacity,
                        error_type=error_type,
                        interval_seconds=interval_seconds,
                    )
                    pending_failure = None
                claim = await claim_work(session_factory)
                completed = bool(claim and await complete_work(session_factory, claim))
                state.update(healthy=True, last_error=None)
                if completed:
                    continue
            except Exception as exc:
                state.update(healthy=False, last_error=type(exc).__name__)
                state["failures"] += 1
                logger.warning("learning worker iteration failed", extra={"error_type": type(exc).__name__})
                if claim:
                    capacity = isinstance(exc, WorkerCapacityError)
                    pending_failure = (claim, capacity, type(exc).__name__)
                    try:
                        await _record_worker_failure(
                            session_factory,
                            claim,
                            capacity=capacity,
                            error_type=type(exc).__name__,
                            interval_seconds=interval_seconds,
                        )
                        pending_failure = None
                    except Exception as recording_error:
                        logger.warning(
                            "learning failure recording awaits recovery",
                            extra={"error_type": type(recording_error).__name__},
                        )
            await _worker_pause(stop_event, interval_seconds)
    finally:
        state.update(running=False, healthy=False)


async def supervise_worker(session_factory, *, stop_event, interval_seconds=5, health=None):
    """Restart an unexpectedly terminated worker; cancellation still propagates."""
    state = {} if health is None else health
    state.setdefault("restarts", 0)
    state.setdefault("failures", 0)
    try:
        while not stop_event.is_set():
            try:
                await run_worker(
                    session_factory, stop_event=stop_event, interval_seconds=interval_seconds, health=state
                )
                error_type = "WorkerStopped"
            except Exception as exc:
                error_type = type(exc).__name__
            if stop_event.is_set():
                break
            state.update(running=False, healthy=False, last_error=error_type)
            state["failures"] += 1
            state["restarts"] += 1
            logger.warning("restarting learning worker", extra={"error_type": error_type})
            await _worker_pause(stop_event, interval_seconds)
    finally:
        state.update(running=False, healthy=False)


def add_routes(app, session_factory, *, authenticate, require_scope):
    """REST admission shares the Hub's API-key, role and delegation checks."""
    from starlette.responses import JSONResponse

    from hub import rbac, scopes
    from hub.rest import _read_json

    async def endpoint(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        authenticated, denied = await authenticate(request)
        required = (
            scopes.SCOPE_ADMIN
            if operation in _ADMIN
            else scopes.SCOPE_READ
            if operation in ("status", "capabilities", "assign")
            else scopes.SCOPE_WRITE
        )
        capability = (
            rbac.CAP_MANAGE_USERS
            if operation in _ADMIN
            else rbac.CAP_VIEW
            if required == scopes.SCOPE_READ
            else rbac.CAP_ANALYZE
        )
        if denied is None:
            denied = require_scope(authenticated, required, capability)
        if denied is not None:
            return denied
        payload = dict(request.query_params) if request.method == "GET" else await _read_json(request)
        labels = (
            request.query_params.getlist("context")
            if request.method == "GET"
            else payload.pop("context", [])
            if isinstance(payload, dict)
            else []
        )
        if isinstance(payload, dict):
            payload.pop("context", None)
        if payload is None:
            return JSONResponse(
                {"error": "invalid_learning_request", "detail": "expected a bounded JSON object"}, status_code=400
            )
        principal = (
            authenticated.key_prefix
            if isinstance(authenticated, auth.UserPrincipal)
            else "key:" + authenticated.key_id
            if authenticated.key_id
            else audit.actor_for_api_key(authenticated.key_prefix)
        )
        try:
            if (
                not isinstance(labels, list)
                or len(labels) > 128
                or any(not isinstance(label, str) or not label.strip() or len(label) > 256 for label in labels)
            ):
                raise ValueError("context must contain at most 128 nonempty scope labels of at most 256 characters")
            if any(
                label.partition(":")[0] in ("tenant", "user", "agent", "session", "repo")
                and not label.partition(":")[2]
                for label in labels
            ):
                raise ValueError("principal scope labels require a nonempty value")
            context = scope_from_labels(labels)
            trusted = {"tenant": authenticated.org_id}
            if isinstance(authenticated, auth.UserPrincipal):
                trusted["user"] = authenticated.key_prefix.removeprefix("user:")
            if any(name in context and context[name] != value for name, value in trusted.items()):
                raise PermissionError("context cannot override authenticated identity")
            result = await operate(
                session_factory,
                authenticated.org_id,
                principal,
                operation,
                payload,
                admin=scopes.satisfies(authenticated.scopes, scopes.SCOPE_ADMIN)
                and (
                    not isinstance(authenticated, auth.UserPrincipal)
                    or rbac.has_capability(authenticated.role, rbac.CAP_MANAGE_USERS)
                ),
                scope={**context, **trusted},
            )
            return JSONResponse(result)
        except WorkerCapacityError:
            return JSONResponse(
                {"error": "learning_busy", "detail": "numeric workers are at capacity; retry later"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        except PermissionError as exc:
            return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)
        except contract.LearningError as exc:
            return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=exc.status_code)
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": "invalid_learning_request", "detail": str(exc)}, status_code=400)

    for operation in contract.OPERATIONS:
        app.add_route(
            "/v1/learning/" + operation,
            endpoint,
            methods=["GET" if operation in ("capabilities", "status") else "POST"],
        )


def register_mcp(scoped_tool, session_factory):
    """Register the same contract behind MCP scope and role enforcement."""
    from hub import scopes

    async def call(operation, payload):
        try:
            return await operate(
                session_factory,
                auth.get_current_org_id(),
                "key:" + auth.current_key_id.get() if auth.current_key_id.get() else auth.get_current_actor(),
                operation,
                payload,
                admin=scopes.satisfies(auth.current_scopes.get(), scopes.SCOPE_ADMIN)
                and (auth.current_user.get() is None or auth.current_user.get().role == "owner"),
                scope={"user": auth.current_user.get().id} if auth.current_user.get() else {},
            )
        except WorkerCapacityError:
            return {"error": "learning_busy", "status": 503, "retry_after": 1}
        except PermissionError as exc:
            return {"error": "forbidden", "detail": str(exc)}
        except contract.LearningError as exc:
            return {"error": exc.code, "detail": str(exc), "status": exc.status_code}

    @scoped_tool(scopes.SCOPE_READ)
    async def learning_capabilities() -> dict:
        """Discover versioned measured-learning operations."""
        return await call("capabilities", {})

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def learning_configure(
        mode: str, utility: dict | None = None, holdout_rate: StrictFloat = 0.5, min_arm: StrictInt = 10
    ) -> dict:
        """Start an immutable experiment configuration; requires owner/admin."""
        return await call(
            "configure", {"mode": mode, "utility": utility, "holdout_rate": holdout_rate, "min_arm": min_arm}
        )

    @scoped_tool(scopes.SCOPE_READ)
    async def learning_assign(
        occasion_id: str, memory_ids: list[str], task_context: str = "", experiment_id: str | None = None
    ) -> dict:
        """Persist a randomized assignment from current authorized SQL sources."""
        return await call(
            "assign",
            {
                "occasion_id": occasion_id,
                "memory_ids": memory_ids,
                "task_context": task_context,
                **({"experiment_id": experiment_id} if experiment_id is not None else {}),
            },
        )

    @scoped_tool(scopes.SCOPE_WRITE)
    async def learning_delivery(occasion_id: str, memory_ids: list[str], experiment_id: str | None = None) -> dict:
        """Record the exact memories serialized for this assignment."""
        return await call(
            "delivery",
            {
                "occasion_id": occasion_id,
                "memory_ids": memory_ids,
                **({"experiment_id": experiment_id} if experiment_id is not None else {}),
            },
        )

    @scoped_tool(scopes.SCOPE_WRITE)
    async def learning_outcome(
        occasion_id: str,
        succeeded: StrictBool | None = None,
        reward: StrictFloat | None = None,
        metrics: dict | None = None,
        experiment_id: str | None = None,
    ) -> dict:
        """Accept exactly one binary, graded, or frozen-utility task outcome."""
        payload: dict = {"occasion_id": occasion_id}
        payload.update(
            {
                key: value
                for key, value in (
                    ("succeeded", succeeded),
                    ("reward", reward),
                    ("metrics", metrics),
                    ("experiment_id", experiment_id),
                )
                if value is not None
            }
        )
        return await call("outcome", payload)

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def learning_run(experiment_id: str | None = None, limit: StrictInt = 10000) -> dict:
        """Run a bounded deterministic evidence evaluation."""
        return await call(
            "run", {"limit": limit, **({"experiment_id": experiment_id} if experiment_id is not None else {})}
        )

    @scoped_tool(scopes.SCOPE_READ)
    async def learning_status(experiment_id: str | None = None) -> dict:
        """Read configuration, decisions and durable pending work."""
        return await call("status", {"experiment_id": experiment_id} if experiment_id is not None else {})

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def learning_rollback(
        reason: str, decision_revision: StrictInt | None = None, experiment_id: str | None = None
    ) -> dict:
        """Restore an earlier policy as a new auditable decision revision."""
        return await call(
            "rollback",
            {
                "reason": reason,
                **({"decision_revision": decision_revision} if decision_revision is not None else {}),
                **({"experiment_id": experiment_id} if experiment_id is not None else {}),
            },
        )
