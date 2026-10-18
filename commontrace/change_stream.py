"""UniMem admission-filtered resumable change streams and deletion obligations.

Cursors acknowledge positions, not delivery to an external store. Erasure ACKs
are separate signed entries. Subscriptions and stream capture are explicitly
configured; a new default store never opens network transports.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

from commontrace import _jsonl, memory_authority, paths
from commontrace import frontier_protocol as fp

KINDS = {"assert", "retract", "contradict", "supersede", "erase", "quarantine"}
JOURNAL = "memory-changes.jsonl"


def _flag(root: str) -> str:
    path = os.path.join(paths.memory_dir(root), "change-stream.json")
    paths.enforce_boundary(root, path)
    return path


def enabled(root: str) -> bool:
    return os.path.isfile(os.path.join(paths.memory_dir(root), "change-stream.json"))


def subscribe(
    root: str,
    identity: str,
    *,
    context: Sequence[str] = (),
    namespace: str = "",
    entity: str = "",
    predicate: str = "",
    ack_seconds: int = 3600,
) -> dict[str, Any]:
    from commontrace import nextwave_store as store

    store.operator()
    fp.text(identity)
    fp.count(ack_seconds, 10, 7 * 86400)
    for label in (namespace, entity, predicate):
        if label:
            fp.text(label, "standing-query filter", 128)
    value = {
        "id": identity,
        "filters": {"namespace": namespace, "entity": entity, "predicate": predicate},
        "ack_seconds": ack_seconds,
        "principal": memory_authority.WRITER.get()[0],
        "created_at": fp.now(),
    }
    prior = store.latest(root, "stream-subscriptions").get(identity)
    if prior:
        old = store.get(root, "stream-subscriptions", identity, context=context)
        if any(old[k] != value[k] for k in ("filters", "ack_seconds", "principal")) or prior["scopes"] != sorted(
            set(context)
        ):
            raise ValueError("UniMem standing-query design is immutable")
        return old
    store.put(root, "stream-subscriptions", identity, value, scopes=context, immutable=True)
    _jsonl.write_json(_flag(root), {"enabled": True})
    return value


def publish(
    root: str,
    kind: str,
    identity: str,
    *,
    scopes: Sequence[str] = (),
    namespace: str = "",
    entity: str = "",
    predicate: str = "",
    revision: str = "",
    event_id: str = "",
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("UniMem unknown change-stream event")
    fp.text(identity)
    fp.count(len(scopes), 0, 128)
    labels = sorted(set(fp.text(s, "scope", 256) for s in scopes))
    for value in (namespace, entity, predicate, revision):
        if value:
            fp.text(value, "change metadata", 256)
    if event_id:
        fp.text(event_id)
    with _jsonl.locked(fp.filename(root, JOURNAL) + ".delivery"):
        if event_id:
            prior = [e for e in fp.journal(root, JOURNAL) if e.get("event_id") == event_id]
            if prior:
                expected = {
                    "kind": kind,
                    "target": identity,
                    "scopes": labels,
                    "namespace": namespace,
                    "entity": entity,
                    "predicate": predicate,
                    "revision": revision,
                }
                if any(prior[0][k] != v for k, v in expected.items()):
                    raise ValueError("UniMem change event identity is already bound")
                return prior[0]
        return dict(
            fp.append(
                root,
                JOURNAL,
                {
                    "kind": kind,
                    "target": identity,
                    "scopes": labels,
                    "namespace": namespace,
                    "entity": entity,
                    "predicate": predicate,
                    "revision": revision,
                    "at": fp.now(),
                    "event_id": event_id,
                },
            )["record"]
        )


def _subscription(root: str, identity: str, context: Sequence[str]) -> dict[str, Any]:
    from commontrace import nextwave_store as store

    value = store.get(root, "stream-subscriptions", identity, context=context)
    if value["principal"] != memory_authority.WRITER.get()[0]:
        raise PermissionError("UniMem stream belongs to another principal")
    if obligations(root, identity=identity, context=context)["cut_off"]:
        raise PermissionError("UniMem subscriber is cut off pending overdue erasure acknowledgement")
    return value


def _cursor(root: str, identity: str, position: int, filters: dict[str, Any]) -> str:
    signed = fp.sign(
        root,
        {
            "type": "unimem.stream-cursor",
            "version": 1,
            "subscription": identity,
            "position": position,
            "filters": fp.digest(filters),
            "expires_at": (datetime.fromisoformat(fp.now()) + timedelta(days=7)).isoformat(),
        },
    )
    return base64.urlsafe_b64encode(fp.wire(signed)).decode().rstrip("=")


def _position(root: str, cursor: str, identity: str, filters: dict[str, Any]) -> int:
    if not cursor:
        return 0
    fp.text(cursor, "cursor", 8192)
    try:
        signed = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        value = signed["record"]
        if (
            not fp.verify(root, signed)
            or value.get("type") != "unimem.stream-cursor"
            or value.get("version") != 1
            or datetime.fromisoformat(value["expires_at"]) <= datetime.fromisoformat(fp.now())
            or value.get("subscription") != identity
            or value.get("filters") != fp.digest(filters)
        ):
            raise ValueError("binding")
        return fp.count(value["position"], 0, fp.MAX_ROWS)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("UniMem invalid stream cursor") from exc


def _deliveries(root: str, identity: str) -> dict[int, dict[str, Any]]:
    from commontrace import nextwave_store as store

    return {r["sequence_number"]: r for r in store.events(root, "stream-deliveries") if r["subscriber"] == identity}


def poll(
    root: str,
    identity: str,
    *,
    cursor: str = "",
    context: Sequence[str] = (),
    limit: int = 100,
    record_delivery: bool = True,
) -> dict[str, Any]:
    recover_pending(root)
    with _jsonl.locked(fp.filename(root, JOURNAL) + ".delivery"):
        return _poll(root, identity, cursor=cursor, context=context, limit=limit, record_delivery=record_delivery)


def _poll(
    root: str,
    identity: str,
    *,
    cursor: str = "",
    context: Sequence[str] = (),
    limit: int = 100,
    record_delivery: bool = True,
) -> dict[str, Any]:
    from commontrace import nextwave_store as store

    fp.count(limit, 1, 256)
    sub = _subscription(root, identity, context)
    position = _position(root, cursor, identity, sub["filters"])
    events = fp.journal(root, JOURNAL)
    if position > len(events):
        raise ValueError("UniMem cursor is ahead of this stream")
    delivered = _deliveries(root, identity)
    known = {r["target"] for r in delivered.values()}
    result: list[dict[str, Any]] = []
    bytes_used = 0
    for event in events[position:]:
        # Advance through denied events too; the returned opaque cursor is
        # bound to this subscription, never another caller's visible list.
        position = event["sequence"] + 1
        labels_visible = store.visible(event["scopes"], context)
        filters = sub["filters"]
        if filters["namespace"] and filters["namespace"] != event["namespace"]:
            continue
        admitted = store.admit(root, [event["target"]], context=context) if labels_visible else {}
        matches = True
        if filters["predicate"]:
            from commontrace import hierarchical

            fact = hierarchical.load_facts(root).get(event["target"]) if admitted else None
            actual_predicate = fact.category if fact is not None else event["predicate"]
            matches = filters["predicate"] == actual_predicate
        if filters["entity"]:
            import re

            text = str(admitted.get(event["target"], {}).get("text", ""))
            matches = matches and (
                filters["entity"] == event["entity"]
                or bool(re.search(r"(?<!\w)" + re.escape(filters["entity"].casefold()) + r"(?!\w)", text.casefold()))
            )
        removal = event["kind"] in {"erase", "retract", "quarantine", "supersede"}
        current_match = bool(admitted and matches and not removal)
        if not current_match and event["target"] not in known:
            continue
        n = event["sequence"]
        removal = event["kind"] in {"erase", "quarantine", "supersede", "retract"}
        kind = event["kind"] if current_match or removal else "retract"
        entry = {
            "cursor_sequence": n,
            "kind": kind,
            "id": event["target"],
            "revision": admitted[event["target"]].get("revision", "") if current_match else "",
            "provenance": {
                "event_digest": fp.digest(event),
                "payload": "current admitted view; not historical content",
            },
        }
        if current_match:
            entry["memory"] = admitted[event["target"]]
        size = len(fp.wire(entry))
        if bytes_used + size > 512 * 1024:
            if not result:
                raise ValueError("UniMem single change exceeds the stream batch bound")
            position = event["sequence"]
            break
        bytes_used += size
        if n not in delivered and record_delivery:
            value = {
                "subscriber": identity,
                "sequence_number": n,
                "target": event["target"],
                "event_digest": fp.digest(event),
                "erase_ack_required": event["kind"] == "erase",
                "deadline": (datetime.fromisoformat(fp.now()) + timedelta(seconds=sub["ack_seconds"])).isoformat(),
            }
            store.event(root, "stream-deliveries", fp.digest([identity, n]), value)
        known.add(event["target"])
        result.append(entry)
        if len(result) == limit:
            break
    return {
        "events": result,
        "cursor": _cursor(root, identity, position, sub["filters"]),
        "semantics": "resume after this cursor; replay the previous cursor for retry, not a delivery ACK",
    }


def obligations(
    root: str, *, identity: str = "", targets: Sequence[str] = (), context: Sequence[str] = ()
) -> dict[str, Any]:
    from commontrace import nextwave_store as store

    if not identity:
        store.operator()
    acknowledgements = {(r["subscriber"], r["sequence_number"]) for r in store.events(root, "stream-acks")}
    erased = [e for e in fp.journal(root, JOURNAL) if e["kind"] == "erase"]
    deliveries = store.events(root, "stream-deliveries")
    pending = []
    for sub_id, metadata in store.latest(root, "stream-subscriptions").items():
        if identity and sub_id != identity:
            continue
        if metadata.get("status") != "active":
            continue
        # Obligations exist as soon as a previously delivered target is erased,
        # including a disconnected subscriber that has not polled the tombstone.
        seen = {d["target"] for d in deliveries if d["subscriber"] == sub_id}
        sub = store.get(root, "stream-subscriptions", sub_id, context=context, owner=not bool(identity))
        if identity and sub["principal"] != memory_authority.WRITER.get()[0]:
            raise PermissionError("UniMem stream belongs to another principal")
        for target in sorted(seen):
            if targets and target not in targets:
                continue
            if (memory_authority.lineage_blocked(root, target, canonical_only=True)
                    and not any(e["target"] == target for e in erased)):
                pending.append(
                    {
                        "subscriber": sub_id,
                        "sequence_number": None,
                        "target": target,
                        "deadline": fp.now(),
                        "overdue": True,
                        "status": "erase publication pending",
                    }
                )
        for event in erased:
            target = event["target"]
            if target not in seen or targets and target not in targets:
                continue
            n = event["sequence"]
            if (sub_id, n) in acknowledgements:
                continue
            deadline = (datetime.fromisoformat(event["at"]) + timedelta(seconds=sub["ack_seconds"])).isoformat()
            pending.append(
                {
                    "subscriber": sub_id,
                    "sequence_number": n,
                    "target": target,
                    "deadline": deadline,
                    "overdue": deadline <= fp.now(),
                }
            )
    return {"pending": pending, "cut_off": any(p["overdue"] for p in pending), "all_acknowledged": not pending}


def acknowledge(
    root: str, identity: str, sequence_number: int, *, receipt_digest: str, context: Sequence[str] = ()
) -> dict[str, Any]:
    # ACK must remain available after cutoff; receiving more content may not.
    from commontrace import nextwave_store as store

    sub = store.get(root, "stream-subscriptions", identity, context=context)
    if sub["principal"] != memory_authority.WRITER.get()[0]:
        raise PermissionError("UniMem erasure acknowledgement principal mismatch")
    pending = obligations(root, identity=identity, context=context)["pending"]
    if sequence_number not in {r["sequence_number"] for r in pending}:
        raise ValueError("UniMem acknowledgement requires a pending delivered erasure obligation")
    if len(receipt_digest) != 64 or any(c not in "0123456789abcdef" for c in receipt_digest):
        raise ValueError("UniMem downstream erasure requires a SHA-256 receipt commitment")
    value = {
        "subscriber": identity,
        "sequence_number": sequence_number,
        "receipt_digest": receipt_digest,
        "acknowledged_at": fp.now(),
        "classification": "authenticated downstream claim, not remote byte verification",
    }
    store.event(root, "stream-acks", fp.digest([identity, sequence_number]), value)
    return value


def sse(
    root: str, identity: str, *, cursor: str = "", context: Sequence[str] = (), limit: int = 100
) -> Iterator[bytes]:
    """A bounded SSE batch; reconnect with Last-Event-ID for the next batch."""
    result = poll(root, identity, cursor=cursor, context=context, limit=limit)
    yield from render_sse(root, identity, result, context=context)


def render_sse(root: str, identity: str, result: dict[str, Any], *, context: Sequence[str] = ()) -> Iterator[bytes]:
    from commontrace import nextwave_store as store

    expected = {e["id"]: fp.digest(e["memory"]) for e in result["events"] if "memory" in e}
    # The single resumable resource diff is atomic at cursor level. HTTP may
    # split bytes; clients commit the cursor only after parsing a complete event.
    wire = (
        "id: "
        + result["cursor"]
        + "\nevent: beliefs\ndata: "
        + json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n\n"
    ).encode()
    for start in range(0, len(wire), 64 * 1024):
        _subscription(root, identity, context)
        current = store.admit(root, list(expected), context=context)
        if set(current) != set(expected) or any(fp.digest(current[i]) != digest for i, digest in expected.items()):
            raise PermissionError("UniMem standing-query data changed before deferred delivery")
        yield wire[start : start + 64 * 1024]


def resource_uri(identity: str) -> str:
    from urllib.parse import quote

    return "unimem://beliefs/" + quote(fp.text(identity), safe="")


def notification(identity: str) -> dict[str, Any]:
    return {"method": "notifications/resources/updated", "params": {"uri": resource_uri(identity)}}


def kafka_send(producer: Any, topic: str, result: dict[str, Any]) -> int:
    """Producer-compatible adapter; callers own broker authentication/residency."""
    fp.text(topic, "topic", 249)
    fp.wire(result)
    for event in result["events"]:
        producer.produce(topic, key=fp.digest(event).encode(), value=fp.wire(event))
    if producer.flush(10):
        raise ConnectionError("UniMem Kafka delivery remains pending")
    return len(result["events"])


def forward_kafka(
    root: str,
    identity: str,
    producer: Any,
    topic: str,
    *,
    cursor: str = "",
    context: Sequence[str] = (),
    limit: int = 100,
) -> dict[str, Any]:
    """Governed forwarding; successful flush acknowledges transport, never erasure.

    The caller configures authenticated brokers and authorized topic readers.
    Retries may duplicate messages; consumers deduplicate by the event key.
    """
    from commontrace import nextwave_store as store

    fp.text(topic, "topic", 249)
    result = poll(root, identity, cursor=cursor, context=context, limit=limit)
    for event in result["events"]:
        _subscription(root, identity, context)
        if "memory" in event:
            admitted = store.admit(root, [event["id"]], context=context)
            if event["id"] not in admitted or fp.digest(admitted[event["id"]]) != fp.digest(event["memory"]):
                raise PermissionError("UniMem Kafka data changed before delivery")
        producer.produce(topic, key=fp.digest(event).encode(), value=fp.wire(event))
    if producer.flush(10):
        raise ConnectionError("UniMem Kafka delivery remains pending; retry the input cursor")
    return {"cursor": result["cursor"], "sent": len(result["events"]), "delivery": "broker transport acknowledgement"}


def kafka_producer(config: dict[str, Any]) -> Any:
    try:
        from confluent_kafka import Producer
    except ImportError:
        raise RuntimeError("install commontrace[kafka] for Kafka/Redpanda delivery") from None
    return Producer(config)


async def notify(root: str, identity: str, session: Any, *, cursor: str = "", context: Sequence[str] = ()) -> bool:
    """Standard MCP resource notification after an admitted standing-query diff."""
    result = poll(root, identity, cursor=cursor, context=context, record_delivery=False)
    if not result["events"]:
        return False
    await session.send_resource_updated(resource_uri(identity))
    return True


async def watch(
    root: str, identity: str, session: Any, *, cursor: str = "", context: Sequence[str] = (), seconds: int = 60
) -> None:
    """Bounded subscription lease; reconnect/renew rather than retaining dead sessions."""
    import asyncio
    import time

    fp.count(seconds, 1, 300)
    deadline = time.monotonic() + seconds
    checkpoint = None
    while time.monotonic() < deadline:
        from commontrace.async_workers import STORE_WORKERS

        result = await STORE_WORKERS.run(poll, root, identity, cursor=cursor, context=context, record_delivery=False)
        visible = fp.digest(result["events"])
        if result["events"] and visible != checkpoint:
            await session.send_resource_updated(resource_uri(identity))
        checkpoint = visible
        await asyncio.sleep(0.25)


def intent_path(root: str, namespace: str) -> str:
    if namespace not in {"facts", "lessons"}:
        raise ValueError("UniMem unknown canonical stream namespace")
    filename = os.path.join(paths.memory_dir(root), namespace + "-stream-intent.json")
    paths.enforce_boundary(root, filename)
    return filename


def guard(root: str, namespace: str) -> Any:
    return _jsonl.locked(intent_path(root, namespace))


def snapshot(values: Any) -> dict[str, dict[str, Any]]:
    """Bounded metadata commitments, never raw content in the durable outbox."""
    fp.count(len(values), 0, 10000, "UniMem streamed canonical namespace size")
    result = {}
    if hasattr(values, "connection") and hasattr(values, "before"):
        ids = {r[0] for r in values.connection.execute("SELECT id FROM facts")}
        ids.update(i for i, old in values.before.items() if old is None)
        ids.difference_update(values.removed)
        items = ((i, values[i]) for i in sorted(ids))
    else:
        items = values.items()
    for identity, value in items:
        body = value.to_dict() if hasattr(value, "to_dict") else value
        result[identity] = {
            "scopes": body.get("scopes", []),
            "revision": body.get("revision", ""),
            "status": body.get("status", "active"),
            "forgotten": body.get("forgotten", False),
            "content_hash": fp.digest(body),
            "predicate": body.get("category", ""),
        }
    return result


def state_hash(state: dict[str, dict[str, Any]]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for identity, value in sorted(state.items()):
        digest.update(fp.wire([identity, value]))
        digest.update(b"\n")
    return digest.hexdigest()


def stage(root: str, namespace: str, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> bool:
    """Durably stage metadata while holding guard through canonical commit."""
    changes = []
    for identity in sorted(set(before) | set(after)):
        old, new = before.get(identity), after.get(identity)
        if old == new:
            continue
        value = new or old or {}
        kind = (
            "erase"
            if new is None
            else "assert"
            if old is None
            else "supersede"
            if new.get("status") == "superseded"
            else "retract"
            if new.get("forgotten") or new.get("status") in {"retracted", "retired"}
            else "quarantine"
            if new.get("status") in {"quarantined", "review"}
            else "assert"
        )
        changes.append(
            {
                "kind": kind,
                "identity": identity,
                "scopes": value["scopes"],
                "namespace": namespace,
                "revision": value["revision"],
                "predicate": value["predicate"],
            }
        )
    if not changes:
        return False
    path = intent_path(root, namespace)
    if os.path.exists(path):
        raise ValueError("UniMem pending canonical change outbox must recover before another write")
    intent = {
        "type": "unimem.canonical-change-intent",
        "namespace": namespace,
        "before": state_hash(before),
        "after": state_hash(after),
        "changes": changes,
    }
    intent["transaction"] = fp.digest(intent)
    signed = fp.sign(root, intent)
    fp.wire(signed)
    _jsonl.write_json(path, signed)
    _sync_directory(os.path.dirname(path))
    return True


def _intent(root: str, namespace: str) -> dict[str, Any] | None:
    path = intent_path(root, namespace)
    try:
        with open(path, "rb") as stream:
            raw = stream.read(fp.MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("UniMem canonical change outbox exceeds its bound")
    signed = json.loads(raw)
    if not fp.verify(root, signed) or signed["record"].get("namespace") != namespace:
        raise PermissionError("UniMem canonical change outbox signature is invalid")
    value: dict[str, Any] = signed["record"]
    if value.get("type") != "unimem.canonical-change-intent" or value.get("transaction") != fp.digest(
        {k: v for k, v in value.items() if k != "transaction"}
    ):
        raise ValueError("UniMem canonical change outbox domain or commitment is invalid")
    fp.count(len(value["changes"]), 1, 2048, "UniMem canonical outbox changes")
    return value


def complete(root: str, namespace: str) -> bool:
    """After commit, retryable publication never falsely reports a failed write."""
    from commontrace import telemetry

    try:
        intent = _intent(root, namespace)
        if intent is None:
            return True
        directory = paths.lessons_dir(root) if namespace == "lessons" else os.path.join(paths.memory_dir(root), "facts")
        if os.path.isdir(directory):
            _sync_directory(directory)
        for i, change in enumerate(intent["changes"]):
            publish(root, **change, event_id=fp.digest([intent["transaction"], i]))
        _sync_directory(paths.memory_dir(root))
        os.unlink(intent_path(root, namespace))
        _sync_directory(paths.memory_dir(root))
        return True
    except (OSError, ValueError, PermissionError, TypeError):
        telemetry.count("unimem_stream_pending", namespace=namespace)
        return False


def recover(root: str, namespace: str, current: dict[str, dict[str, Any]]) -> None:
    """Caller holds guard before reading the canonical state or mutating it."""
    intent = _intent(root, namespace)
    if intent is None:
        return
    current_hash = state_hash(current)
    if current_hash == intent["after"]:
        if not complete(root, namespace):
            raise RuntimeError("UniMem canonical change publication is pending; repair the stream before another write")
    elif current_hash == intent["before"]:
        # Atomic canonical replacement/transaction did not commit.
        os.unlink(intent_path(root, namespace))
    else:
        raise RuntimeError("UniMem canonical source differs from both outbox states; operator reconciliation required")


def lesson_snapshot(root: str) -> dict[str, dict[str, Any]]:
    from pathlib import Path

    from commontrace import frontmatter, lesson_io, revision
    from commontrace import nextwave_store as store

    values = {}
    for filename in Path(paths.lessons_dir(root)).glob("*.md"):
        if filename.name == "README.md":
            continue
        paths.enforce_boundary(root, str(filename))
        if filename.stat().st_size > store.MAX_BLOB:
            raise ValueError("UniMem streamed lesson exceeds its body bound")
        fm, body = frontmatter.read(str(filename))
        identity = lesson_io.canonical_slug(filename.name)
        values[identity] = {
            "scopes": fm.get("scopes", []),
            "status": fm.get("status", "active"),
            "revision": revision.revision_of(fm, body),
        }
    return snapshot(values)


def recover_pending(root: str) -> None:
    if not enabled(root):
        return
    from commontrace import hierarchical

    for namespace in ("facts", "lessons"):
        if not os.path.exists(intent_path(root, namespace)):
            continue
        with guard(root, namespace):
            current = snapshot(hierarchical.load_facts(root)) if namespace == "facts" else lesson_snapshot(root)
            recover(root, namespace, current)

    if os.path.isfile(os.path.join(paths.memory_dir(root), "runtime.sqlite3")):
        from commontrace.memory_journal import Journal

        Journal(root).drain_changes()
    reconcile_conflicts(root)
    reconcile_revocations(root)


def reconcile_conflicts(root: str) -> None:
    """Recover typed contradictions from the durable canonical conflict queue.

    Each endpoint gets its own scoped event; no cross-scope pair is disclosed.
    These events identify current admitted beliefs, not historical statements.
    """
    from commontrace import fact_conflicts, hierarchical

    conflicts = fp.rows(fact_conflicts._queue_path(root))
    if not conflicts:
        return
    fp.count(len(conflicts), 0, 2048, "UniMem canonical conflict reconciliation bound")
    facts = hierarchical.load_facts(root)
    published = {event.get("event_id") for event in fp.journal(root, JOURNAL)}
    for conflict in conflicts:
        commitment = fp.digest(conflict)
        for identity in (conflict["older"], conflict["newer"]):
            fact = facts.get(identity)
            if fact is not None:
                event_id = fp.digest(
                    ["canonical-conflict", commitment, identity, fact.scopes, fact.revision, fact.category]
                )
                if event_id in published:
                    continue
                publish(
                    root,
                    "contradict",
                    identity,
                    scopes=fact.scopes,
                    namespace="facts",
                    revision=fact.revision,
                    predicate=fact.category,
                    event_id=event_id,
                )


def reconcile_revocations(root: str) -> None:
    """Emit erase tombstones for delivered sources revoked outside a fact write.

    Lessons, traces and their descendants can lose source authority without
    another canonical fact transaction. Only prior deliveries create this
    reconciliation duty; an unseen revoked object is never newly disclosed.
    """
    from commontrace import nextwave_store as store

    known = {row["target"] for row in store.events(root, "stream-deliveries")}
    fp.count(len(known), 0, 2048, "UniMem delivered revocation reconciliation bound")
    latest = {event["target"]: event for event in fp.journal(root, JOURNAL)}
    for identity in sorted(known):
        event = latest.get(identity)
        if (event is None or event["kind"] == "erase"
                or not memory_authority.lineage_blocked(root, identity, canonical_only=True)):
            continue
        publish(
            root,
            "erase",
            identity,
            scopes=event["scopes"],
            namespace=event["namespace"],
            event_id=fp.digest(["unimem.delivered-source-revocation.v1", event]),
        )


def _sync_directory(directory: str) -> None:
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def fact_transactions(factory: Any) -> Any:
    """Outbox around SQLite commits, including direct canonical erasure SQL."""
    import functools
    from contextlib import contextmanager

    @functools.wraps(factory)
    @contextmanager
    def wrapped(root: str, *, write: bool = False) -> Iterator[Any]:
        if not write or not enabled(root):
            with factory(root, write=write) as values:
                yield values
            return
        with guard(root, "facts"):
            with factory(root, write=write) as values:
                before = snapshot(values)
                recover(root, "facts", before)
                yield values
                stage(root, "facts", before, snapshot(values))
            complete(root, "facts")

    return wrapped


def fact_mutations(factory: Any) -> Any:
    """Outbox around legacy atomic JSONL replacement; SQLite uses its own hook."""
    import functools
    from contextlib import contextmanager

    @functools.wraps(factory)
    @contextmanager
    def wrapped(root: str) -> Iterator[Any]:
        if not enabled(root):
            with factory(root) as values:
                yield values
            return
        with guard(root, "facts"):
            with factory(root) as values:
                before = snapshot(values)
                recover(root, "facts", before)
                yield values
                stage(root, "facts", before, snapshot(values))
            complete(root, "facts")

    return wrapped


def lesson_writes(factory: Any) -> Any:
    import functools

    from commontrace import lesson_io, revision

    @functools.wraps(factory)
    def wrapped(path: str, fm: dict[str, Any], body: str, *, root: str, actor: str = "", reason: str = "") -> Any:
        fm = lesson_io.prepare_delegated(root, path, fm, body)
        if not enabled(root):
            return factory(path, fm, body, root=root, actor=actor, reason=reason)
        with guard(root, "lessons"):
            before = lesson_snapshot(root)
            recover(root, "lessons", before)
            identity = lesson_io.canonical_slug(os.path.basename(path))
            after = dict(before)
            after[identity] = snapshot(
                {
                    identity: {
                        "scopes": fm.get("scopes", []),
                        "status": fm.get("status", "active"),
                        "revision": revision.revision_of(fm, body),
                    }
                }
            )[identity]
            stage(root, "lessons", before, after)
            result = factory(path, fm, body, root=root, actor=actor, reason=reason)
            complete(root, "lessons")
            return result

    return wrapped
