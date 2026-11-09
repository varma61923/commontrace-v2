"""Scoped measured learning backed by the canonical runtime journal.

Memory truth remains in Journal. These tables contain immutable experimental
receipts and policy revisions, never a competing memory store. Outcome acceptance
and the pending-work checkpoint share one SQLite transaction. A numeric worker's
transaction is also its fence: crashes roll back both policy and checkpoint.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from unimem import capabilities as authority
from unimem import learning_contract as contract
from unimem import memory_authority, purpose
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord
from unimem.record_visibility import now, scope_from_labels, visible

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_experiments (
 namespace TEXT NOT NULL, experiment TEXT NOT NULL, scope TEXT NOT NULL,
 config TEXT NOT NULL, policy TEXT NOT NULL, decision_revision INTEGER NOT NULL DEFAULT 0,
 checkpoint TEXT, pending INTEGER NOT NULL DEFAULT 0, last_report TEXT,
 PRIMARY KEY(namespace,experiment));
CREATE TABLE IF NOT EXISTS learning_current (namespace TEXT PRIMARY KEY, experiment TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_assignments (
 namespace TEXT NOT NULL, experiment TEXT NOT NULL, occasion TEXT NOT NULL,
 request_digest TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(namespace,experiment,occasion));
CREATE TABLE IF NOT EXISTS learning_decisions (
 namespace TEXT NOT NULL, experiment TEXT NOT NULL, revision INTEGER NOT NULL,
 policy TEXT NOT NULL, report TEXT NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(namespace,experiment,revision));
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def capabilities() -> dict:
    return contract.capabilities()


def _initialize(journal: Journal) -> None:
    # executescript commits implicitly: schema initialization uses its own connection.
    with journal.connect() as db:
        db.executescript(_SCHEMA)


def _experiment(db, namespace, experiment_id=None):
    if experiment_id is None:
        current = db.execute("SELECT experiment FROM learning_current WHERE namespace=?", (namespace,)).fetchone()
        experiment_id = current[0] if current else None
    row = db.execute(
        "SELECT config,policy,decision_revision,checkpoint,pending,last_report "
        "FROM learning_experiments WHERE namespace=? AND experiment=?",
        (namespace, experiment_id),
    ).fetchone()
    if row is None:
        raise contract.LearningUnavailable("learning is not configured in this principal scope")
    return experiment_id, row


def _source(journal: Journal, identity: str, scope: dict) -> dict:
    record = journal.current(identity, scope=scope)
    if record is None:
        raise contract.LearningUnavailable("memory source is unavailable in this principal scope")
    from unimem import safety

    protected = contract.protected_memory(record.kind, record.payload) or identity in safety.protected(journal.root)
    # Payload is the canonical typed representation; rendering must never accept caller text.
    text = record.payload.get("text", record.payload.get("content", record.payload.get("body")))
    if not isinstance(text, str):
        text = _json(record.payload)
    return {"id": identity, "revision": record.version, "text": text, "protected": protected}


def _capture_authority(root: str) -> dict:
    """Persist authenticated context, never caller-supplied identity or proof."""
    principal, actor_class = memory_authority.bound_writer()
    original: dict[str, Any] = {"principal": principal, "authority": actor_class, "purpose": purpose.current(root)}
    if authority.CURRENT.get() is not None:
        from unimem import capability_io

        delegated = capability_io.current(root)
        if delegated is None:
            raise PermissionError("authenticated capability disappeared")
        original["delegation"] = {"token": delegated["token"], "registration": delegated["registration"]}
    return original


def _original_permits(journal: Journal, source: dict, scope: dict, original: dict | None) -> bool:
    """Recheck original authority in addition to the current worker's admission.

    Historical rows without authenticated context are ineligible: current owner
    permissions cannot be invented as a substitute for their missing authority.
    """
    if not isinstance(original, dict) or not isinstance(original.get("principal"), str):
        return False
    if original.get("authority") not in memory_authority.TRUST or not isinstance(original.get("purpose"), str):
        return False
    # Trusted internal replay must also restore an empty purpose. The public
    # request() API intentionally cannot let agents choose arbitrary purposes.
    purpose_token = purpose._REQUEST.set(original["purpose"])
    try:
        with memory_authority.writer(original["principal"], original["authority"]):
            record = journal.current(source["id"], scope=scope)
            if record is None or record.version != source["revision"]:
                return False
            delegation = original.get("delegation")
            if delegation is not None:
                from unimem import capabilities, capability_io
                from unimem import frontier_protocol as fp

                verified = capability_io.check(journal.root, delegation["token"])
                registration = capability_io.agent(journal.root, verified["caveats"]["audience"])
                if (
                    fp.digest(registration) != delegation["registration"]
                    or registration["principal"] != original["principal"]
                ):
                    return False
                # Journal admission rechecks policy, consent, relationships and
                # grounding. Reapply the original leaf's limits as well.
                pending = [record]
                seen = set()
                while pending:
                    current = pending.pop()
                    if current.id in seen:
                        continue
                    seen.add(current.id)
                    if len(seen) > 10000:
                        return False
                    descriptor = capability_io.runtime_descriptor(current)
                    namespace, kind, labels = capability_io.record_policy(descriptor, namespace="records")
                    if not capabilities.permits(
                        verified["caveats"],
                        namespace=namespace,
                        operation="read",
                        memory_class=kind,
                        purpose=original["purpose"],
                        labels=labels,
                    ):
                        return False
                    for identity in current.relations.get("derives", []):
                        parent = journal.current(identity, scope=scope)
                        if parent is None:
                            return False
                        pending.append(parent)
                    for identity in current.relations.get("caused_by", []):
                        with journal.connect() as db:
                            capture = db.execute(
                                "SELECT session,event,body,scope FROM captures WHERE id=?", (identity,)
                            ).fetchone()
                        if capture is None:
                            return False
                        descriptor = capability_io.capture_descriptor(
                            identity, capture[0], capture[1], json.loads(capture[2]), json.loads(capture[3])
                        )
                        namespace, kind, labels = capability_io.record_policy(descriptor, namespace="records")
                        if not capabilities.permits(
                            verified["caveats"],
                            namespace=namespace,
                            operation="read",
                            memory_class=kind,
                            purpose=original["purpose"],
                            labels=labels,
                        ):
                            return False
            return True
    except (ValueError, PermissionError, KeyError, TypeError):
        return False
    finally:
        purpose._REQUEST.reset(purpose_token)


def _sources(journal: Journal, identities: list[str], scope: dict) -> list[dict]:
    return [_source(journal, identity, scope) for identity in identities]


def _check_revisions(journal: Journal, db, sources: list[dict], scope: dict) -> None:
    """Revalidate the complete grounding closure on the locked canonical snapshot.

    An unchanged leaf version does not establish that its parents remain active,
    revision-compatible or authorized. Use the same admission as ordinary reads
    while BEGIN IMMEDIATE prevents canonical writers from changing that evidence.
    """
    reference = now()
    for source in sources:
        row = db.execute("SELECT body FROM records WHERE id=?", (source["id"],)).fetchone()
        current = MemoryRecord(**json.loads(row[0])) if row else None
        if (
            current is None
            or current.version != source["revision"]
            or not visible(current, scope, at=reference)
            or not journal._grounded(db, current, scope, statuses=("active",), at=reference)
        ):
            raise contract.LearningConflict("source or its grounding changed during learning operation")


def _public(assignment: dict, sources: list[dict] | None = None) -> dict:
    out = {k: v for k, v in assignment.items() if k not in ("request_digest", "sources", "_authority")}
    if sources is not None:
        out["memories"] = [
            {k: source[k] for k in ("id", "text", "revision")}
            for source in sources
            if source["id"] in assignment["inject"]
        ]
    return contract.response(**out)


def _load_assignment(db, namespace, experiment_id, occasion_id):
    row = db.execute(
        "SELECT body FROM learning_assignments WHERE namespace=? AND experiment=? AND occasion=?",
        (namespace, experiment_id, occasion_id),
    ).fetchone()
    if row is None:
        raise contract.LearningUnavailable("learning occasion is unknown in this principal scope")
    return json.loads(row[0])


def _save_assignment(db, namespace, experiment_id, assignment):
    db.execute(
        "UPDATE learning_assignments SET body=? WHERE namespace=? AND experiment=? AND occasion=?",
        (_json(assignment), namespace, experiment_id, assignment["occasion_id"]),
    )
    db.execute(
        "UPDATE learning_experiments SET pending=1 WHERE namespace=? AND experiment=?", (namespace, experiment_id)
    )


def operate(
    root: str,
    operation: str,
    payload: dict | None = None,
    *,
    context: list[str] | None = None,
    untrusted: bool = False,
    scope: dict | None = None,
    admin: bool | None = None,
) -> dict:
    """Execute one operation under separately supplied trusted principal scope.

    Transport adapters must set untrusted for restricted principals. Delegated
    capabilities additionally constrain Journal admission and cannot administer
    experiments. The request body cannot supply scope, permissions or source text.
    """
    body = {} if payload is None else payload
    contract.validate_request(operation, body)
    if operation == "capabilities":
        return capabilities()
    if scope is not None and context:
        raise contract.LearningError("supply scope or context, not both")
    if context is not None and (
        not isinstance(context, list)
        or len(context) > 128
        or any(not isinstance(label, str) or not label.strip() or len(label) > 256 for label in context)
    ):
        raise contract.LearningError("context must be an array of at most 128 nonempty bounded scope labels")
    if context and any(
        label.partition(":")[0] in ("tenant", "user", "agent", "session", "repo") and not label.partition(":")[2]
        for label in context
    ):
        raise contract.LearningError("principal scope labels require a nonempty value")
    actual_scope = scope_from_labels(context or []) if scope is None else scope
    # Reuse record validation so malformed or unrecognized scope keys fail closed.
    MemoryRecord(id="scope-validation", kind="fact", version=1, payload={}, scope=actual_scope)
    actual_scope = json.loads(_json(actual_scope))
    if "labels" in actual_scope:
        actual_scope["labels"] = sorted(set(actual_scope["labels"]))
    from unimem.memory_control import REQUIRED_PRINCIPAL_SCOPE

    principal = REQUIRED_PRINCIPAL_SCOPE.get()
    if principal and actual_scope.get("agent") != principal.partition(":")[2]:
        raise PermissionError("learning scope must match the authenticated principal")
    namespace = contract.digest(actual_scope)
    delegated = authority.CURRENT.get() is not None
    if operation in ("configure", "run", "rollback") and (
        untrusted or delegated or admin is False or not memory_authority.is_operator()
    ):
        raise PermissionError("learning administration requires the local owner")
    if untrusted and memory_authority.bound_writer()[1] == "operator":
        actor = actual_scope.get("agent")
        if not actor:
            raise PermissionError("untrusted learning requires a pinned agent principal")
        with memory_authority.restricted_writer(actor, "agent"):
            return operate(root, operation, body, scope=actual_scope, untrusted=True, admin=admin)
    journal = Journal(root)
    _initialize(journal)
    if operation == "configure":
        config = contract.validate_config(body)
        with journal.connect(write=True) as db:
            previous = db.execute(
                "SELECT e.config FROM learning_experiments e JOIN learning_current c "
                "ON e.namespace=c.namespace AND e.experiment=c.experiment WHERE e.namespace=?",
                (namespace,),
            ).fetchone()
            revision = json.loads(previous[0])["revision"] + 1 if previous else 1
            config.update(experiment_id=str(uuid.uuid4()), revision=revision, salt=uuid.uuid4().hex)
            db.execute(
                "INSERT INTO learning_experiments(namespace,experiment,scope,config,policy) VALUES(?,?,?,?,?)",
                (namespace, config["experiment_id"], _json(actual_scope), _json(config), "{}"),
            )
            db.execute(
                "INSERT INTO learning_current VALUES(?,?) "
                "ON CONFLICT(namespace) DO UPDATE SET experiment=excluded.experiment",
                (namespace, config["experiment_id"]),
            )
            db.execute(
                "INSERT INTO learning_decisions VALUES(?,?,?,?,?,?)",
                (namespace, config["experiment_id"], 0, "{}", "{}", "configured"),
            )
        return contract.response(experiment_id=config["experiment_id"], config_revision=revision, config=config)
    with journal.connect() as db:
        experiment_id, row = _experiment(db, namespace, body.get("experiment_id"))
    config, policy = json.loads(row[0]), json.loads(row[1])
    if operation == "assign":
        sources = _sources(journal, body["memory_ids"], actual_scope)
        original_authority = _capture_authority(root)
        fingerprint = contract.digest(
            {
                "ids": body["memory_ids"],
                "context": body.get("task_context", ""),
                "revisions": [[s["id"], s["revision"]] for s in sources],
                "authority": original_authority,
            }
        )
        with journal.connect(write=True) as db:
            _, locked = _experiment(db, namespace, experiment_id)
            _check_revisions(journal, db, sources, actual_scope)
            existing = db.execute(
                "SELECT request_digest,body FROM learning_assignments "
                "WHERE namespace=? AND experiment=? AND occasion=?",
                (namespace, experiment_id, body["occasion_id"]),
            ).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise contract.LearningConflict(
                        "occasion identity is already bound to different input or revisions"
                    )
                assignment = json.loads(existing[1])
            else:
                assignment = contract.make_assignment(
                    config,
                    body["occasion_id"],
                    sources,
                    task_context=body.get("task_context", ""),
                    policy=json.loads(locked[1]),
                )
                assignment["at"] = now()
                assignment["policy_revision"] = locked[2]
                assignment["_authority"] = original_authority
                # Never retain copied source content after consent revocation/erasure.
                assignment["sources"] = [{k: v for k, v in s.items() if k != "text"} for s in sources]
                assignment["request_digest"] = fingerprint
                db.execute(
                    "INSERT INTO learning_assignments VALUES(?,?,?,?,?)",
                    (namespace, experiment_id, body["occasion_id"], fingerprint, _json(assignment)),
                )
        return _public(assignment, sources)
    if operation in ("delivery", "outcome"):
        with journal.connect() as db:
            assignment = _load_assignment(db, namespace, experiment_id, body["occasion_id"])
        value: Any
        if operation == "delivery":
            if set(body["memory_ids"]) - set(assignment["inject"]):
                raise contract.LearningError("delivery IDs must be a subset of assigned injection")
            sources = _sources(journal, [s["id"] for s in assignment["sources"]], actual_scope)
            if [[s["id"], s["revision"]] for s in sources] != [[s["id"], s["revision"]] for s in assignment["sources"]]:
                raise contract.LearningConflict("source revision changed before delivery")
            value = sorted(body["memory_ids"])
        else:
            sources = []
            value = contract.normalize_outcome(config, body["occasion_id"], body)
        with journal.connect(write=True) as db:
            assignment = _load_assignment(db, namespace, experiment_id, body["occasion_id"])
            if sources:
                _check_revisions(journal, db, sources, actual_scope)
            if assignment[operation] is not None and assignment[operation] != value:
                raise contract.LearningConflict(f"conflicting {operation} retry")
            if assignment[operation] is None:
                assignment[operation] = value
                _save_assignment(db, namespace, experiment_id, assignment)
        return _public(assignment)
    if operation == "status":
        with journal.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM learning_assignments WHERE namespace=? AND experiment=?",
                (namespace, experiment_id),
            ).fetchone()[0]
            references = db.execute(
                "SELECT DISTINCT json_extract(s.value,'$.id'),json_extract(s.value,'$.revision') "
                "FROM learning_assignments a,json_each(a.body,'$.sources') s "
                "WHERE a.namespace=? AND a.experiment=? LIMIT 101",
                (namespace, experiment_id),
            ).fetchall()
        stale_status = set()
        admitted_by_id = {}
        for identity, version in references[:100]:
            if identity not in admitted_by_id:
                try:
                    admitted_by_id[identity] = _source(journal, identity, actual_scope)["revision"]
                except contract.LearningUnavailable:
                    admitted_by_id[identity] = None
            if admitted_by_id[identity] != version:
                stale_status.add(identity)
        from unimem import jobs

        work_key = f"learning:{namespace}:{experiment_id}"
        failures = [
            {"status": job.status, "attempts": job.attempts, "error": job.error}
            for status in ("failed", "dead")
            for job in jobs.list_jobs(root, status=status, limit=100)
            if job.dedupe_key == work_key
        ]
        return contract.response(
            experiment_id=experiment_id,
            config_revision=config["revision"],
            config=config,
            decision_revision=row[2],
            policy=policy,
            pending_work=bool(row[4]),
            occasions=count,
            report=json.loads(row[5]) if row[5] else None,
            stale_sources=sorted(stale_status),
            source_scan_truncated=len(references) > 100,
            failures=failures,
        )
    if operation == "rollback":
        with journal.connect(write=True) as db:
            _, row = _experiment(db, namespace, experiment_id)
            target = body.get("decision_revision", max(0, row[2] - 1))
            previous = db.execute(
                "SELECT policy FROM learning_decisions WHERE namespace=? AND experiment=? AND revision=?",
                (namespace, experiment_id, target),
            ).fetchone()
            if previous is None or target > row[2]:
                raise contract.LearningUnavailable("rollback policy revision is unknown")
            revision = row[2] + 1
            db.execute(
                "INSERT INTO learning_decisions VALUES(?,?,?,?,?,?)",
                (namespace, experiment_id, revision, previous[0], "{}", body["reason"]),
            )
            db.execute(
                "UPDATE learning_experiments SET policy=?,decision_revision=? WHERE namespace=? AND experiment=?",
                (previous[0], revision, namespace, experiment_id),
            )
        return contract.response(
            experiment_id=experiment_id,
            decision_revision=revision,
            restored_revision=target,
            policy=json.loads(previous[0]),
        )
    # Resolve authority outside the transaction, then verify exact record revisions
    # under the write fence before applying any policy changes.
    with journal.connect() as db:
        saved = db.execute(
            "SELECT body FROM learning_assignments WHERE namespace=? AND experiment=? ORDER BY rowid LIMIT ?",
            (namespace, experiment_id, body.get("limit", 10000) + 1),
        ).fetchall()
    assignments = [json.loads(item[0]) for item in saved]
    if len(assignments) > body.get("limit", 10000):
        # Refusal is a completed bounded pass, not a retryable worker failure.
        # Count the complete cohort and settle its work request under the same
        # write fence as outcome acceptance; no evidence is silently truncated.
        with journal.connect(write=True) as db:
            _, locked = _experiment(db, namespace, experiment_id)
            count = db.execute(
                "SELECT COUNT(*) FROM learning_assignments WHERE namespace=? AND experiment=?",
                (namespace, experiment_id),
            ).fetchone()[0]
            report = {"decisions": [], "blocked": ["evidence_budget_exceeded"], "evidence": count}
            checkpoint = contract.digest(
                ["evidence_budget_exceeded", config["revision"], body.get("limit", 10000), count]
            )
            db.execute(
                "UPDATE learning_experiments SET checkpoint=?,pending=?,last_report=? "
                "WHERE namespace=? AND experiment=?",
                (checkpoint, 0 if count > 10000 else locked[4], _json(report), namespace, experiment_id),
            )
        return contract.response(experiment_id=experiment_id, applied=False, decision_revision=locked[2], **report)
    admitted, stale = [], []
    seen = set()
    for assignment in assignments:
        for source in assignment["sources"]:
            key = (source["id"], source["revision"])
            if key in seen:
                continue
            seen.add(key)
            try:
                current = _source(journal, source["id"], actual_scope)
                if current["revision"] != source["revision"]:
                    stale.append(source["id"])
                else:
                    admitted.append(current)
            except contract.LearningUnavailable:
                stale.append(source["id"])
    original_blocked = set()
    original_cache = {}
    for assignment in assignments:
        original = assignment.get("_authority")
        for source in assignment["sources"]:
            authority_key = (contract.digest(original), source["id"], source["revision"])
            if authority_key not in original_cache:
                original_cache[authority_key] = _original_permits(journal, source, actual_scope, original)
            if not original_cache[authority_key]:
                original_blocked.add(contract.policy_key(source["id"], source["revision"], assignment["task_context"]))
    with journal.connect(write=True) as db:
        _, locked = _experiment(db, namespace, experiment_id)
        # Evidence may have arrived while source admission ran; abort instead of
        # accidentally checkpointing an unprocessed accepted outcome.
        current_assignments = [
            json.loads(item[0])
            for item in db.execute(
                "SELECT body FROM learning_assignments WHERE namespace=? AND experiment=? ORDER BY rowid LIMIT ?",
                (namespace, experiment_id, body.get("limit", 10000) + 1),
            ).fetchall()
        ]
        if contract.digest(current_assignments) != contract.digest(assignments):
            raise contract.LearningConflict("learning evidence changed; retry bounded pass")
        _check_revisions(journal, db, admitted, actual_scope)
        checkpoint = contract.digest(
            [assignments, sorted(stale), sorted((s["id"], s["revision"]) for s in admitted), sorted(original_blocked)]
        )
        if locked[3] == checkpoint:
            # A duplicate durable job must settle its request even if its
            # evidence was already evaluated by another numeric worker.
            db.execute(
                "UPDATE learning_experiments SET pending=0 WHERE namespace=? AND experiment=?",
                (namespace, experiment_id),
            )
            return contract.response(
                experiment_id=experiment_id,
                applied=False,
                decision_revision=locked[2],
                **(json.loads(locked[5]) if locked[5] else {}),
            )
        report = contract.evaluate(config, assignments)
        if original_blocked:
            report["decisions"] = [d for d in report["decisions"] if d["key"] not in original_blocked]
            report["blocked"] = sorted(set(report["blocked"]) | {"original_authority_unavailable"})
        report["stale_sources"] = sorted(set(stale))
        if stale:
            report["blocked"] = sorted(set(report["blocked"]) | {"stale_sources"})
        changed = False
        policy = json.loads(locked[1])
        admitted_revisions = {(source["id"], source["revision"]) for source in admitted}
        if config["mode"] == "adaptive":
            for decision in report["decisions"]:
                if (decision["id"], decision["revision"]) not in admitted_revisions:
                    continue
                if decision["verdict"] not in ("HELPS", "HURTS", "NO_MEASURABLE_EFFECT"):
                    continue
                selected = {"rate": decision["rate"], "disabled": decision["disabled"]}
                if policy.get(decision["key"]) != selected:
                    policy[decision["key"]] = selected
                    changed = True
        revision = locked[2] + int(changed)
        if changed:
            db.execute(
                "INSERT INTO learning_decisions VALUES(?,?,?,?,?,?)",
                (namespace, experiment_id, revision, _json(policy), _json(report), "measured evidence"),
            )
        db.execute(
            "UPDATE learning_experiments SET policy=?,decision_revision=?,checkpoint=?,pending=0,last_report=? "
            "WHERE namespace=? AND experiment=?",
            (_json(policy), revision, checkpoint, _json(report), namespace, experiment_id),
        )
    return contract.response(experiment_id=experiment_id, applied=changed, decision_revision=revision, **report)


def run_pending(root: str, *, limit: int = 20) -> dict:
    """Resume atomically accepted numeric work without any LLM dependency."""
    path = os.path.join(root, "memory", "runtime.sqlite3")
    if not os.path.isfile(path):
        return {"processed": 0, "failed": 0}
    journal = Journal(root)
    _initialize(journal)
    with journal.connect() as db:
        pending = db.execute(
            "SELECT scope,experiment FROM learning_experiments WHERE pending=1 ORDER BY rowid LIMIT ?",
            (max(1, min(100, limit)),),
        ).fetchall()
    processed, failed = 0, 0
    for scope, experiment_id in pending:
        try:
            operate(root, "run", {"experiment_id": experiment_id}, scope=json.loads(scope))
            processed += 1
        except (ValueError, PermissionError, RuntimeError, OSError):
            failed += 1
    return {"processed": processed, "failed": failed}


def enqueue_pending(root: str, *, limit: int = 20) -> int:
    """Recover the atomic learning outbox into the existing leased job queue.

    A crash between databases cannot lose work: the pending bit is only cleared
    with the measured decision, and repeated queue delivery is idempotent.
    """
    if not os.path.isfile(os.path.join(root, "memory", "runtime.sqlite3")) or limit <= 0:
        return 0
    journal = Journal(root)
    _initialize(journal)
    with journal.connect() as db:
        rows = db.execute(
            "SELECT namespace,experiment,scope FROM learning_experiments WHERE pending=1 ORDER BY rowid LIMIT ?",
            (min(100, limit),),
        ).fetchall()
    from unimem import jobs

    for namespace, experiment_id, scope in rows:
        jobs.enqueue(
            root,
            "learn-measured",
            {"experiment_id": experiment_id, "scope": json.loads(scope)},
            dedupe_key=f"learning:{namespace}:{experiment_id}",
        )
    return len(rows)
