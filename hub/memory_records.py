"""Event-backed PostgreSQL records with full-text candidates and Hub tenancy.

The optional vector index is the existing owner-pinned pgvector/HNSW engine.
Canonical records are revalidated after every ANN lookup, including erasure.
"""

from __future__ import annotations

import html
import json
import logging
import math
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy import DateTime, cast, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from commontrace import capabilities, memory_guard
from commontrace.fact_evidence import MAX_CHECKS, EvidenceResolver
from commontrace.memory_record import (
    MemoryRecord,
    atomic_fact_from_record,
    facet_observation,
    preference_candidates,
    preference_statistics,
    rerank_profile_results,
)
from commontrace.record_visibility import GroundingState, bind_capture_sources, now, sanitize_record_content, visible
from hub import audit, delegation
from hub.db import session_scope
from hub.memory_vector_events import append_change, redact_changes
from hub.models import MemoryRecordEvent, MemoryRecordProjection, MemoryVectorErasure

_VECTOR_CANDIDATE_BATCH = 500
_ATOMIC_FACT_FORMAT = "commontrace.atomic-fact.v1"


def _erasure_closure(rows, identity):
    """Index historical causal edges once, then visit each affected edge once."""
    dependents, upstream, episodes = {}, {}, set()
    for row in rows:
        record = row.record
        scope = json.dumps(record["scope"], sort_keys=True)
        if record["kind"] == "episode":
            episodes.add((row.id, scope))
        for identities in record["relations"].values():
            for source in identities:
                dependents.setdefault(source, set()).add(row.id)
        if record["provenance"].get("atomic_fact_format") == _ATOMIC_FACT_FORMAT:
            for receipt in record["payload"].get("evidence", []):
                if isinstance(receipt, dict):
                    dependents.setdefault(receipt.get("source_id"), set()).add(row.id)
        for source in record["payload"].get("source_message_ids", []):
            upstream.setdefault(row.id, set()).add((source, scope))

    pending, deleted, source_episodes = [identity], set(), set()
    while pending:
        current = pending.pop()
        if current in deleted:
            continue
        deleted.add(current)
        sources = {source for source, scope in upstream.get(current, ()) if (source, scope) in episodes}
        source_episodes.update(sources)
        pending.extend((dependents.get(current, set()) | sources) - deleted)
    return deleted, source_episodes


class _HostedEvidenceResolver(EvidenceResolver):
    """Resolve hosted fact evidence without reading an unrelated local lesson store."""

    def _lesson(self, identity):
        # Hosted lessons need their own typed, tenant-scoped receipt before they
        # can support an AtomicFact. A local filesystem lesson is never authority
        # for a server-side record.
        self._lessons[identity] = None
        return None


class PostgresJournal:
    def __init__(self, session_factory, org_id: str, *, vector_index=None, projection_factory=None):
        if not org_id or vector_index is not None and vector_index.tenant != org_id:
            raise ValueError("vector and record owners must match authenticated tenancy")
        self.factory, self.org_id, self.vector_index = session_factory, org_id, vector_index
        self.projection_factory = projection_factory

    async def put_targeted(self, record: MemoryRecord, policy: dict, *, context: str, features: dict,
                           environment: str, verify_policy) -> MemoryRecord:
        """Promote into a measured fleet class only; ownership still comes from org_id.

        Trusted coordinator code supplies the pinned policy verifier. This is a
        server-side SDK operation, not a client-controlled verification callback.
        Ordinary record writes do not constitute fleet promotion.
        """
        from commontrace import frontier_protocol as fp

        if not verify_policy(policy):
            raise PermissionError("fleet transfer requires an authenticated uplift policy")
        row = policy["record"]
        if (row.get("type") != "validated-uplift-policy-v1"
                or fp.number(row["rate_lower"], -100, 100) <= 0
                or row.get("environment") != environment
                or row.get("classes", {}).get(context) != features):
            raise PermissionError("fleet class has no validated positive effect in this environment")
        if row.get("lesson") != record.id:
            raise PermissionError("targeting policy belongs to a different memory")
        if not features or len(features) > 7:
            raise ValueError("bounded pre-treatment fleet features required")
        labels = list(record.scope.get("labels", []))
        for key, value in features.items():
            fp.text(key, "feature", 64)
            fp.text(value, "class", 64)
            labels.append(key + ":" + value)
        governed = replace(record, scope={**record.scope, "labels": sorted(set(labels))},
            provenance={**record.provenance, "uplift_policy_digest": policy["digest"], "uplift_context": context})
        return await self.put(governed)

    async def put(self, record: MemoryRecord) -> MemoryRecord:
        if record.scope.get("tenant", self.org_id) != self.org_id:
            raise ValueError("record tenant cannot override authenticated organization")
        record = bind_capture_sources(
            replace(sanitize_record_content(record), scope={**record.scope, "tenant": self.org_id})
        )
        if capabilities.CURRENT.get() is not None:
            from commontrace import capability_io

            record = capability_io.runtime_prepare(delegation.ROOT.get(), record)
        if "atomic_fact_format" in record.provenance:
            atomic_fact_from_record(record)
        try:
            async with session_scope(self.factory, org_id=self.org_id) as session:
                await self._lock_owner(session)
                await self._put(session, record)
                if record.kind == "fact":
                    await self._refresh_facet(session, record)
            return record
        except IntegrityError as exc:
            raise ValueError("concurrent record version conflict") from exc

    async def _lock_owner(self, session):
        # One tenant's projection/erasure must serialize; other tenants remain independent.
        # In particular, a remote erasure retry must not delete a newly reused identifier.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 61923))"),
            {"owner": "commontrace-memory:" + self.org_id},
        )

    async def _put(self, session, record):
        if capabilities.CURRENT.get() is not None:
            from commontrace import capability_io

            record = capability_io.runtime_prepare(delegation.ROOT.get(), record)
        from commontrace.capability_io import runtime_descriptor
        from hub import policies

        policy = await policies.active(session, self.org_id)
        if not policies.permits(policy, runtime_descriptor(record), action="write"):
            raise PermissionError("UniMem active finite policy denies this hosted memory write")
        if await session.get(MemoryVectorErasure, (self.org_id, record.id)) is not None:
            raise ValueError("record identity has pending vector erasure")
        old = await session.get(MemoryRecordProjection, (self.org_id, record.id), with_for_update=True)
        if record.version != (old.version + 1 if old else 1):
            raise ValueError("record version conflict")
        if old and (old.record["kind"] != record.kind or old.scope != record.scope):
            raise ValueError("record identity cannot change kind or scope")
        incoming = (
            (
                await session.execute(
                    select(MemoryRecordProjection).where(
                        MemoryRecordProjection.org_id == self.org_id,
                        or_(
                            *(
                                MemoryRecordProjection.record.contains({"relations": {kind: [record.id]}})
                                for kind in ("updates", "extends", "derives", "supersedes", "caused_by")
                            )
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(row.scope != record.scope for row in incoming):
            raise ValueError("forward record relationships cannot cross scopes")
        for identities in record.relations.values():
            for identity in identities:
                source = await session.get(MemoryRecordProjection, (self.org_id, identity))
                if source is not None and source.scope != record.scope:
                    raise ValueError("record relationships cannot cross scopes")
        await delegation.stamp_record(session, self.org_id, record)
        session.add(
            MemoryRecordEvent(org_id=self.org_id, id=record.id, version=record.version, record=record.to_dict())
        )
        change_seq = await append_change(session, self.org_id, record)
        statement = insert(MemoryRecordProjection).values(
            org_id=self.org_id,
            id=record.id,
            version=record.version,
            change_seq=change_seq,
            record=record.to_dict(),
            scope=record.scope,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["org_id", "id"],
                set_={"version": record.version, "change_seq": change_seq,
                      "record": record.to_dict(), "scope": record.scope},
            )
        )
        from commontrace import frontier_protocol as fp
        from hub import audit

        if session.info.get("transparency_key"):
            await audit.record(session, actor="memory-runtime", action="memory_record.put", org_id=self.org_id,
                               target_type="memory_record", target_id=record.id,
                               summary=f"version={record.version};digest={fp.digest(record.to_dict())}")

    async def _refresh_facet(self, session, record):
        if capabilities.CURRENT.get() is not None:
            from commontrace import capability_io

            if not capability_io.permits(delegation.ROOT.get(), {"scopes": capability_io.scope_labels(record.scope),
                                          "memory_type": "observation"}, namespace="records", operation="write"):
                return
        entity, facet = record.payload.get("entity"), record.payload.get("facet")
        if not isinstance(entity, str) or not isinstance(facet, str) or not entity.strip() or not facet.strip():
            return
        rows = (
            await session.execute(
                select(MemoryRecordProjection)
                .where(
                    MemoryRecordProjection.org_id == self.org_id,
                    MemoryRecordProjection.scope == record.scope,
                    MemoryRecordProjection.record.contains(
                        {"kind": "fact", "payload": {"entity": entity, "facet": facet}}
                    ),
                )
                .order_by(MemoryRecordProjection.id)
            )
        ).scalars()
        sources = []
        for row in rows:
            source = MemoryRecord(**row.record)
            if source.id != record.id and visible(source, record.scope):
                sources.append(source)
                if len(sources) == 8:
                    break
        if visible(record, record.scope):
            sources.append(record)
        if not sources:
            return
        observation = facet_observation(entity, facet, sources)
        from commontrace.capability_io import runtime_descriptor
        from hub import policies

        policy = await policies.active(session, self.org_id)
        if not policies.permits(policy, runtime_descriptor(observation), action="write"):
            return
        old = await session.get(MemoryRecordProjection, (self.org_id, observation.id))
        if old:
            observation = facet_observation(entity, facet, sources, previous=MemoryRecord(**old.record))
        await self._put(session, observation)

    async def _hosted_evidence_resolver(self, session, roots, context, *, at):
        """Load the bounded, visible fact dependency closure for one search."""
        facts, invalid, pending = {}, set(), set()

        def decode(record):
            if record.provenance.get("atomic_fact_format") != _ATOMIC_FACT_FORMAT:
                return
            if not visible(record, context, at=at):
                invalid.add(record.id)
                return
            try:
                fact = atomic_fact_from_record(record)
            except (TypeError, ValueError):
                invalid.add(record.id)
                return
            facts[record.id] = fact
            if fact.evidence_bound:
                pending.update(receipt.source_id for receipt in fact.evidence if receipt.kind == "fact")

        for record in roots:
            decode(record)

        seen = set(facts) | invalid
        while pending and len(seen) < MAX_CHECKS:
            requested = sorted(pending - seen)
            pending.clear()
            if not requested:
                break
            requested = requested[: MAX_CHECKS - len(seen)]
            seen.update(requested)
            for start in range(0, len(requested), _VECTOR_CANDIDATE_BATCH):
                rows = (await session.execute(
                    select(MemoryRecordProjection).where(
                        MemoryRecordProjection.org_id == self.org_id,
                        MemoryRecordProjection.id.in_(requested[start:start + _VECTOR_CANDIDATE_BATCH]),
                        MemoryRecordProjection.scope.op("<@")(context),
                        delegation.record_predicate(),
                        MemoryRecordProjection.record.contains({
                            "kind": "fact",
                            "provenance": {"atomic_fact_format": _ATOMIC_FACT_FORMAT},
                        }),
                    )
                )).scalars().all()
                for row in rows:
                    decode(MemoryRecord(**row.record))
        return _HostedEvidenceResolver("", facts), invalid

    async def _cap_allowed_ids(self, session, context, reference, kinds):
        from commontrace import frontier_protocol as fp
        from commontrace.capability_io import runtime_descriptor
        from hub import policies

        policy = await policies.active(session, self.org_id)

        rows = (await session.execute(select(MemoryRecordProjection).where(
            MemoryRecordProjection.org_id == self.org_id, delegation.record_predicate(),
            MemoryRecordProjection.scope.op("<@")(context),
            MemoryRecordProjection.record.contains({"status": "active", "is_latest": True}),
        ).order_by(MemoryRecordProjection.id).limit(2001))).scalars().all()
        fp.count(len(rows), 0, 2000, "UniMem scoped Hub capability namespace")
        records = {row.id: MemoryRecord(**row.record) for row in rows}
        grounding = GroundingState()
        def admitted(identity, trail=frozenset()):
            cached = grounding.enter(identity, trail)
            if cached is not None:
                return cached
            record = records.get(identity)
            if (record is None or not visible(record, context, at=reference) or not delegation.readable_record(record)
                    or not policies.permits(policy, runtime_descriptor(record))):
                return grounding.remember(identity, trail, False)
            parents = sorted(set(record.relations.get("derives", [])) | set(record.relations.get("caused_by", [])))
            versions = record.provenance.get("source_versions", {})
            okay = all(admitted(parent, trail | {identity}) and
                       (parent not in versions or versions[parent] == records[parent].version) for parent in parents)
            return grounding.remember(identity, trail, okay)
        return {identity for identity, record in records.items() if (not kinds or record.kind in kinds)
                and admitted(identity)}

    async def search(self, query: str, *, scope: dict, limit=20, vector=None, kinds=()) -> list[MemoryRecord]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 1000:
            raise ValueError("record result limit must be in 0..1000")
        if not limit:
            return []
        context = {**scope, "tenant": self.org_id}
        reference = now()
        projected_hits = None
        projection_cursor = None
        from hub import policies

        async with session_scope(self.factory, org_id=self.org_id) as session:
            policy = await policies.active(session, self.org_id)
        cap_mode = capabilities.CURRENT.get() is not None or delegation.enrolled() or policy is not None
        allowed_ids = None
        if vector is None and query.strip() and self.projection_factory is not None:
            try:
                async with self.projection_factory.acquire(self.org_id) as projection:
                    if projection.org_id != self.org_id:
                        raise ValueError("projection owner cannot override authenticated organization")
                    await projection.drain(limit=128)
                    if not cap_mode:
                        projection_cursor, projected_hits = await projection.candidates(
                            query, top_k=max(100, limit * 5))
            except Exception as exc:  # noqa: BLE001 - optional dense failure preserves lexical retrieval
                logging.getLogger(__name__).debug("Hosted dense fallback: %s", type(exc).__name__)
        async with session_scope(self.factory, org_id=self.org_id) as session:
            if self.projection_factory is not None or cap_mode:
                # Keep candidate hydration and recursive proof checks on one
                # tenant revision while writes and erasures use the same fence.
                await self._lock_owner(session)
            from sqlalchemy import func

            if cap_mode:
                allowed_ids = await self._cap_allowed_ids(session, context, reference, kinds)
                if not allowed_ids:
                    return []
                if vector is None and query.strip() and self.projection_factory is not None:
                    try:
                        async with self.projection_factory.acquire(self.org_id) as projection:
                            projection_cursor, projected_hits = await projection.candidates(
                                query, top_k=max(100, limit * 5), allowed_ids=allowed_ids)
                    except Exception as exc:  # noqa: BLE001 - optional dense errors preserve admitted sparse reads
                        logging.getLogger(__name__).debug("Hosted scoped dense fallback: %s", type(exc).__name__)
            statement = select(MemoryRecordProjection).where(
                MemoryRecordProjection.org_id == self.org_id,
                delegation.record_predicate(),
                MemoryRecordProjection.scope.op("<@")(context),
                # Apply lifecycle admission before the bounded candidate window.
                MemoryRecordProjection.record.contains({"status": "active", "is_latest": True}),
            )
            if allowed_ids is not None:
                statement = statement.where(MemoryRecordProjection.id.in_(sorted(allowed_ids)))
            if kinds:
                statement = statement.where(MemoryRecordProjection.record["kind"].astext.in_(list(kinds)))
            if query.strip():
                tsquery = func.websearch_to_tsquery("english", query[:20000])
                statement = statement.where(MemoryRecordProjection.search_vector.op("@@")(tsquery)).order_by(
                    func.ts_rank_cd(MemoryRecordProjection.search_vector, tsquery).desc()
                )
            statement = statement.order_by(MemoryRecordProjection.id)
            rows = (await session.execute(statement.limit(max(200, limit * 5)))).scalars().all()
            found = {row.id: MemoryRecord(**row.record) for row in rows}
            sparse_order = list(found)
            dense_order = []
            if projected_hits is not None or vector is not None and self.vector_index is not None:
                if projected_hits is not None:
                    hits = projected_hits
                elif allowed_ids is not None:
                    hits = await self.vector_index.search(vector, top_k=max(100, limit * 5), allowed_ids=allowed_ids)
                else:
                    hits = await self.vector_index.search(vector, top_k=max(100, limit * 5))
                hit_ids = list(dict.fromkeys(hit.key for hit in hits))
                dense_records: dict[str, MemoryRecord] = {}
                for start in range(0, len(hit_ids), _VECTOR_CANDIDATE_BATCH):
                    group = hit_ids[start:start + _VECTOR_CANDIDATE_BATCH]
                    dense_statement = select(MemoryRecordProjection).where(
                        MemoryRecordProjection.org_id == self.org_id,
                        delegation.record_predicate(),
                        MemoryRecordProjection.id.in_(group),
                        MemoryRecordProjection.scope.op("<@")(context),
                        MemoryRecordProjection.record.contains({"status": "active", "is_latest": True}),
                    )
                    if allowed_ids is not None:
                        dense_statement = dense_statement.where(MemoryRecordProjection.id.in_(sorted(allowed_ids)))
                    if projection_cursor is not None:
                        dense_statement = dense_statement.where(
                            MemoryRecordProjection.change_seq <= projection_cursor)
                    if kinds:
                        dense_statement = dense_statement.where(
                            MemoryRecordProjection.record["kind"].astext.in_(list(kinds)))
                    dense_rows = (await session.execute(dense_statement)).scalars().all()
                    dense_records.update((row.id, MemoryRecord(**row.record)) for row in dense_rows)
                for hit in hits:
                    record = dense_records.get(hit.key)
                    if record and visible(record, context, at=reference):
                        found[record.id] = record
                        dense_order.append(record.id)
            atomic_roots = [
                record for record in found.values()
                if record.provenance.get("atomic_fact_format") == _ATOMIC_FACT_FORMAT
            ]
            evidence_resolver, invalid_atomic = None, set()
            if atomic_roots:
                evidence_resolver, invalid_atomic = await self._hosted_evidence_resolver(
                    session, atomic_roots, context, at=reference
                )
            ranks: dict[str, float] = {}
            for order in (sparse_order, dense_order):
                for rank, identity in enumerate(order, 1):
                    ranks[identity] = ranks.get(identity, 0) + 1 / (60 + rank)
            grounding = GroundingState()

            async def grounded(record, trail=frozenset()):
                cached = grounding.enter(record.id, trail)
                if cached is not None:
                    return cached
                if not visible(record, context, at=reference) or not delegation.readable_record(record):
                    return grounding.remember(record.id, trail, False)
                if record.provenance.get("atomic_fact_format") == _ATOMIC_FACT_FORMAT:
                    if evidence_resolver is None or record.id in invalid_atomic:
                        return False
                    assessment = evidence_resolver.assess(record.id)
                    if not assessment.eligible:
                        return False
                for identity in dict.fromkeys(record.relations.get("derives", [])):
                    if grounding.remaining <= 0:
                        return False
                    source = found.get(identity)
                    if source is None:
                        row = await session.get(MemoryRecordProjection, (self.org_id, identity))
                        source = MemoryRecord(**row.record) if row else None
                    versions = record.provenance.get("source_versions")
                    if (
                        source is None
                        or (
                            versions is not None
                            and (not isinstance(versions, dict) or versions.get(identity) != source.version)
                        )
                        or not await grounded(source, trail | {record.id})
                    ):
                        return grounding.remember(record.id, trail, False)
                return grounding.remember(record.id, trail, True)

            eligible = []
            for identity in sorted(ranks, key=lambda identity: (-ranks[identity], identity)):
                if await grounded(found[identity]):
                    eligible.append(found[identity])
                if len(eligible) >= limit:
                    break
            return eligible

    async def capture_turn(self, session_id: str, query: str, *, text_value="", scope: dict, event=None, payload=None):
        """Commit the raw turn and its source-bound profile updates together."""
        context = {**scope, "tenant": self.org_id}
        content = text_value or query
        episode = MemoryRecord(
            "turn-" + uuid.uuid4().hex,
            "episode",
            1,
            {
                "text": content,
                "query": query,
                "session_id": session_id,
                "event": event or "UserPromptSubmit",
                "payload": payload or {},
                "author": "user",
            },
            scope=context,
            created_at=now(),
            provenance={"authored_by": "user"},
        )
        episode = sanitize_record_content(episode)
        async with session_scope(self.factory, org_id=self.org_id) as session:
            await self._lock_owner(session)
            await self._put(session, episode)
            # Only the explicit human message is a preference source. Tool and assistant payloads are evidence.
            if text_value:
                for identity, value in preference_candidates(episode.payload["text"], context):
                    old = await session.get(MemoryRecordProjection, (self.org_id, identity))
                    prior = old.record["payload"] if old else {}
                    data = {**value, "observed_at": now(), "source_message_ids": [episode.id]}
                    if value["bucket"] == "dynamic":
                        data.update(preference_statistics(prior))
                    record = MemoryRecord(
                        identity,
                        "profile",
                        (old.version if old else 0) + 1,
                        data,
                        scope=context,
                        created_at=now(),
                        relations={"derives": [episode.id]},
                        provenance={"authored_by": "user", "source_versions": {episode.id: 1}},
                    )
                    await self._put(session, record)
        return episode

    async def runtime_profile(
        self, query: str, *, scope: dict, limit=10, occasion_id=None, include_preference_rankers=False
    ):
        from commontrace.record_visibility import moment

        if type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("profile result limit must be in 0..1000")
        rows = await self.search("", scope=scope, limit=1000, kinds=("profile", "directive"))
        result = {
            "static": [],
            "dynamic": [],
            "custom": [],
            "directives": [],
            "occasion_id": occasion_id,
            "withheld": [],
            "recent_activity": [],
        }
        if include_preference_rankers:
            result["preference_rankers"] = []
        for record in rows:
            if record.kind == "directive":
                result["directives"].append(record.to_dict())
            if record.kind != "profile":
                continue
            data = record.payload
            bucket = data.get("bucket", "custom")
            if bucket not in ("static", "dynamic", "custom"):
                bucket = "custom"
            age = max(
                0.0,
                (moment(now()) - moment(data.get("observed_at") or record.created_at or now())).total_seconds() / 86400,
            )
            half_life = data.get("half_life_days", 90)
            weight = data.get("weight", 1)
            if (
                type(half_life) not in (int, float)
                or not 0 < half_life <= 36500
                or not math.isfinite(half_life)
                or type(weight) not in (int, float)
                or not 0 <= weight <= 10000
                or not math.isfinite(weight)
            ):
                continue
            weight *= 1 if bucket == "static" else 2 ** (-age / half_life)
            if len(result[bucket]) < limit:
                entry = {
                    "id": record.id,
                    "text": data.get("text", ""),
                    "weight": weight,
                    "critical": bool(data.get("critical")),
                }
                result[bucket].append(entry)
                if (
                    include_preference_rankers
                    and bucket == "dynamic"
                    and data.get("author") == "user"
                    and record.provenance.get("authored_by") == "user"
                    and isinstance(data.get("source_message_ids"), list)
                    and any(isinstance(identity, str) and identity for identity in data["source_message_ids"])
                ):
                    result["preference_rankers"].append({**entry, "bucket": bucket, "author": "user"})
        return result

    async def runtime_turn(
        self, session_id: str, query: str, *, scope: dict, text_value="", budget=600, event=None, payload=None
    ):
        from commontrace.context_template import render_context
        from commontrace.memory_guard import scan_injection

        source = await self.capture_turn(
            session_id, query, text_value=text_value, scope=scope, event=event, payload=payload
        )
        profile = await self.runtime_profile(query, scope=scope, limit=10, include_preference_rankers=True)
        records = await self.search(query, scope=scope, limit=20)
        ranked_records = rerank_profile_results(
            [record.to_dict() for record in records], profile["preference_rankers"]
        )
        # Mandatory owner rules and explicit preferences do not require inferred relevance.
        # Sparse-only candidates remain visible in results, without guessed dense agreement or causal exposure.
        lines: list[str] = []
        identities: list[str] = []
        fixed = [
            *profile["directives"],
            *(
                entry
                for bucket in ("static", "dynamic", "custom")
                for entry in profile[bucket]
                if entry.get("critical") or event == "SessionStart"
            ),
        ]
        for entry in fixed:
            text_value = entry.get("text", entry.get("payload", {}).get("text", ""))
            if not isinstance(text_value, str) or scan_injection(text_value):
                continue
            line = "[" + entry["id"] + "] " + text_value
            rendered = render_context([*lines, line], budget=budget)
            if html.escape(memory_guard.sanitize_metadata({"text": line}, pii=True)[0]["text"]) in rendered:
                lines.append(line)
                identities.append(entry["id"])
        from hub import policies

        context, reference = {**scope, "tenant": self.org_id}, now()
        async with session_scope(self.factory, org_id=self.org_id) as session:
            # Session history is a read surface too. Apply the same policy and
            # capability admission as search before selecting the recent window.
            policy = await policies.active(session, self.org_id)
            statement = select(MemoryRecordProjection).where(
                MemoryRecordProjection.org_id == self.org_id,
                delegation.record_predicate(),
                MemoryRecordProjection.scope.op("<@")(context),
                MemoryRecordProjection.record["kind"].astext == "episode",
                MemoryRecordProjection.record["payload"]["session_id"].astext == session_id,
                MemoryRecordProjection.record.contains({"status": "active", "is_latest": True}),
            )
            if capabilities.CURRENT.get() is not None or delegation.enrolled() or policy is not None:
                await self._lock_owner(session)
                allowed_ids = await self._cap_allowed_ids(session, context, reference, ("episode",))
                statement = statement.where(MemoryRecordProjection.id.in_(sorted(allowed_ids)))
            rows = (await session.execute(statement.order_by(
                MemoryRecordProjection.record["created_at"].astext.desc(), MemoryRecordProjection.id,
            ).limit(16))).scalars().all()
            buffer = [
                {
                    "id": row.id,
                    "event": row.record["payload"]["event"],
                    "payload": row.record["payload"],
                    "scope": row.scope,
                }
                for row in rows
                if visible(MemoryRecord(**row.record), context, at=reference)
                and delegation.readable_record(MemoryRecord(**row.record))
            ]
        return {
            "context": render_context(lines, budget=budget),
            "injected": identities,
            "profile": {k: profile[k] for k in ("static", "dynamic", "custom")},
            "buffer": buffer,
            "session_id": session_id,
            "scope": source.scope,
            "results": ranked_records,
        }

    async def reap_due(self, *, limit: int = 16) -> list[dict]:
        """Erase a bounded batch of tenant-owned records whose forget_after has passed."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("expiration batch must be in 1..100")
        expires_at = cast(
            func.nullif(MemoryRecordProjection.record["payload"]["forget_after"].astext, ""),
            DateTime(timezone=True),
        )
        async with session_scope(self.factory, org_id=self.org_id) as session:
            # Match record_visibility.moment(): naive ISO timestamps mean UTC.
            await session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            rows = (
                await session.execute(
                    select(
                        MemoryRecordProjection.id,
                        MemoryRecordProjection.version,
                        MemoryRecordProjection.record,
                    )
                    .where(
                        MemoryRecordProjection.org_id == self.org_id,
                        expires_at <= datetime.now(timezone.utc),
                    )
                    .order_by(expires_at, MemoryRecordProjection.id)
                    .limit(limit)
                )
            ).all()

        results = []
        for identity, version, body in rows:
            record = MemoryRecord(**body)
            if record.version != version:
                raise ValueError("expiration projection version is inconsistent")
            reason = record.payload.get("forget_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("expiration requires a reason")
            try:
                results.append(
                    await self.forget(identity, reason=reason, expected_version=version)
                )
            except ValueError as exc:
                if str(exc) != "expiration record version changed":
                    raise
        return results

    async def forget(
        self, identity: str, *, reason: str, expected_version: int | None = None, scope: dict | None = None
    ) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("erasure requires a reason")
        if expected_version is not None and (
            type(expected_version) is not int or expected_version < 1
        ):
            raise ValueError("expected version must be a positive integer")
        async with session_scope(self.factory, org_id=self.org_id) as session:
            await self._lock_owner(session)
            from hub import retention

            if scope is not None:
                current = await session.get(MemoryRecordProjection, (self.org_id, identity))
                if current is None:
                    raise ValueError("record not found")
                if current.scope != {**scope, "tenant": self.org_id}:
                    raise PermissionError("only the owning scoped principal can erase this record")
            if capabilities.CURRENT.get() is not None:
                from commontrace import capability_io

                current = await session.get(MemoryRecordProjection, (self.org_id, identity))
                if current is None:
                    raise ValueError("record not found")
                capability_io.assert_forget(delegation.ROOT.get(),
                    capability_io.runtime_descriptor(MemoryRecord(**current.record)), namespace="records")
            if await retention.active_holds(session, self.org_id):
                raise PermissionError("an active legal hold blocks record erasure; release it explicitly")
            if expected_version is not None:
                current = await session.get(
                    MemoryRecordProjection,
                    (self.org_id, identity),
                    with_for_update=True,
                )
                if current is None or current.version != expected_version:
                    raise ValueError("expiration record version changed")
            rows = (
                (await session.execute(select(MemoryRecordEvent).where(MemoryRecordEvent.org_id == self.org_id)))
                .scalars()
                .all()
            )
            # Historical revisions, cited episodes and typed evidence receipts
            # all contribute edges; long chains must not rescan the tenant on
            # every step while holding its writer fence.
            deleted, source_episodes = _erasure_closure(rows, identity)
            if capabilities.CURRENT.get() is not None:
                # Naming a hidden episode as evidence must not grant permission
                # to erase that upstream source. Descendant invalidation remains
                # part of the authorized privacy cascade.
                for row in rows:
                    if row.id in source_episodes:
                        capability_io.assert_forget(delegation.ROOT.get(),
                            capability_io.runtime_descriptor(MemoryRecord(**row.record)), namespace="records")
            if self.vector_index is not None:
                await session.execute(
                    insert(MemoryVectorErasure)
                    .values([{"org_id": self.org_id, "record_id": value} for value in sorted(deleted)])
                    .on_conflict_do_nothing()
                )
            await redact_changes(session, self.org_id, deleted)
            versions: dict[str, int] = {}
            for row in rows:
                versions[row.id] = max(versions.get(row.id, 0), row.version)
            for removed in sorted(deleted):
                await append_change(session, self.org_id, None, record_id=removed,
                                    version=versions.get(removed, 1))
            for model in (MemoryRecordProjection, MemoryRecordEvent):
                await session.execute(delete(model).where(model.org_id == self.org_id, model.id.in_(deleted)))
            safe_reason = memory_guard.sanitize_metadata({"reason": reason}, pii=True)[0]["reason"]
            result = {"deleted": sorted(deleted), "reason": safe_reason, "at": now()}
            await audit.record(
                session,
                actor="memory-erasure",
                action="forget_memory",
                org_id=self.org_id,
                target_type="memory",
                target_id=identity,
                summary="records=" + str(len(deleted)) + " reason=" + safe_reason,
            )
        if self.vector_index is not None:
            result["vector_cleanup"] = await self.retry_vector_erasures()
        if self.projection_factory is not None:
            try:
                if hasattr(self.projection_factory, "erase_records"):
                    result["vector_cleanup"] = await self.projection_factory.erase_records(self.org_id, sorted(deleted))
                else:
                    async with self.projection_factory.acquire(self.org_id) as projection:
                        completed = await projection.erase_records(sorted(deleted))
                    result["vector_cleanup"] = {"completed": completed, "pending": False}
            except Exception:  # noqa: BLE001 - committed tombstones retain cleanup work for every projection
                result["vector_cleanup"] = {"completed": 0, "pending": True,
                                            "reason": "vector cleanup retry required"}
        if capabilities.CURRENT.get() is not None:
            result["deleted"] = [identity] if identity in result["deleted"] else []
        return result

    async def retry_vector_erasures(self, *, limit=256) -> dict:
        """Bounded, tenant-pinned outbox; safe to call from the vector worker after restart."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("vector cleanup batch must be in 1..10000")
        if self.vector_index is None:
            return {"completed": 0, "pending": True, "reason": "vector engine unavailable"}
        async with session_scope(self.factory, org_id=self.org_id) as session:
            await self._lock_owner(session)
            rows = (
                (
                    await session.execute(
                        select(MemoryVectorErasure.record_id)
                        .where(MemoryVectorErasure.org_id == self.org_id)
                        .order_by(MemoryVectorErasure.record_id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return {"completed": 0, "pending": False}
            try:
                await self.vector_index.delete(rows)
            except Exception:  # noqa: BLE001 - canonical erasure committed; external cleanup stays durably pending
                return {"completed": 0, "pending": True, "reason": "vector cleanup retry required"}
            await session.execute(
                delete(MemoryVectorErasure).where(
                    MemoryVectorErasure.org_id == self.org_id, MemoryVectorErasure.record_id.in_(rows)
                )
            )
            pending = (
                await session.execute(
                    select(MemoryVectorErasure.record_id).where(MemoryVectorErasure.org_id == self.org_id).limit(1)
                )
            ).first() is not None
            return {"completed": len(rows), "pending": pending}


def add_memory_routes(app, session_factory, *, authenticate, require_scope):
    """Expose records through authenticated Hub routes; tenant comes only from the API key."""
    import json

    from starlette.responses import JSONResponse

    from hub import scopes

    async def operation(request):
        action = request.path_params["operation"].removeprefix("record-")
        authenticated, denied = await authenticate(request)
        if denied is None:
            required_scope = (
                scopes.SCOPE_ADMIN
                if action == "reap-due"
                else scopes.SCOPE_READ
                if action in ("search", "profile")
                else scopes.SCOPE_WRITE
            )
            denied = require_scope(authenticated, required_scope)
        if denied is None and action == "turn":
            denied = require_scope(authenticated, scopes.SCOPE_READ)
        if denied is not None:
            return denied
        raw = await request.body()
        if len(raw) > 100000:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("record request must be an object")
            from commontrace.api_schema import validate_request

            name = request.path_params["operation"]
            if name in (
                "record-put",
                "record-search",
                "record-forget",
                "record-reap-due",
                "turn",
                "profile",
                "search",
            ):
                validate_request(name, payload)
            if "context" in payload:
                context = payload["context"]
                if not isinstance(context, list) or any(not isinstance(label, str) for label in context):
                    raise ValueError("context must be a list of scope labels")
                scope, labels = {}, []
                for label in context:
                    key, _, value = label.partition(":")
                    if key in ("tenant", "user", "agent", "session", "repo") and value:
                        if key in scope and scope[key] != value:
                            raise ValueError("ambiguous context dimension")
                        scope[key] = value
                    else:
                        labels.append(label)
                if labels:
                    scope["labels"] = sorted(labels)
                payload["scope"] = scope
            store = PostgresJournal(session_factory, authenticated.org_id,
                projection_factory=getattr(app.state, "memory_projection_factory", None))
            if action == "turn":
                return JSONResponse(
                    await store.runtime_turn(
                        payload["session_id"],
                        payload["query"],
                        scope=payload.get("scope", {}),
                        text_value=payload.get("text", ""),
                        budget=payload.get("budget", 600),
                        event=payload.get("event"),
                        payload=payload.get("payload"),
                    )
                )
            if action == "profile":
                return JSONResponse(
                    await store.runtime_profile(
                        payload.get("query", ""),
                        scope=payload.get("scope", {}),
                        limit=payload.get("limit", 10),
                        occasion_id=payload.get("occasion_id"),
                    )
                )
            if action == "put":
                record = MemoryRecord(**payload["record"])
                context = {**payload.get("scope", {}), "tenant": authenticated.org_id}
                if any(context.get(key) != value for key, value in record.scope.items()):
                    raise PermissionError("record scope cannot override request context")
                record = replace(record, scope=context)
                # Remote writes are evidence. Only controlled causal promotion can grant active lessons/rules.
                if record.kind in ("lesson", "directive") and record.status == "active":
                    record = replace(record, status="quarantined")
                return JSONResponse({"record": (await store.put(record)).to_dict()})
            if action == "search":
                return JSONResponse(
                    {
                        "results": [
                            record.to_dict()
                            for record in await store.search(
                                payload.get("query", ""), scope=payload.get("scope", {}), limit=payload.get("limit", 20)
                            )
                        ]
                    }
                )
            if action == "forget":
                return JSONResponse(await store.forget(
                    payload["id"], reason=payload["reason"], scope=payload.get("scope", {})))
            if action == "reap-due":
                return JSONResponse({"results": await store.reap_due(limit=payload.get("limit", 16))})
            return JSONResponse({"error": "unknown_operation"}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)

    app.add_route("/api/v1/memory/records/{operation}", operation, methods=["POST"])
    app.add_route("/v1/memory/{operation}", operation, methods=["POST"])
