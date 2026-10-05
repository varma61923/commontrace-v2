"""Portable conversation archives. Source ids are remapped, never trusted as local ids.

Version 2 retains exact rule/model/manual beliefs instead of extracting them
again with today's heuristics. All sessions and facts commit together, and fact
insertion follows archive order so equal-time reversions keep their chronology.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from commontrace import memory_guard
from commontrace.conversation import profile
from commontrace.conversation.store import FACT_KINDS, ConversationError, _iso, _moment, fact_hash, write_txn

VERSION = 2


def export(store):
    with store.read_snapshot():
        summaries = store.summaries()
        for session in store.sessions():
            sid = session["id"]
            turns = [dict(r) for r in store.db.execute("SELECT * FROM turns WHERE session=? ORDER BY idx", (sid,))]
            facts = [dict(r) for r in store.db.execute(
                "SELECT f.* FROM facts f JOIN turns t ON t.id=f.turn WHERE t.session=? ORDER BY f.id", (sid,))]
            sources = store.fact_source_ids(f["id"] for f in facts)
            speakers = {t["id"]: t["speaker"] for t in turns}
            done = store.get_meta(f"extracted:{sid}")
            processed = [t["id"] for t in turns if done is not None and t["idx"] <= int(done)]
            summary = summaries.get(sid)
            yield {
                "format_version": VERSION, "session": sid, "started_at": session["started_at"],
                "summary": summary["text"] if summary else None,
                "summary_meta": {k: summary[k] for k in ("method", "at")} if summary else None,
                "extracted_source_id": processed[-1] if processed else None,
                "messages": [{"source_id": t["id"], "id": t["ref"], "speaker": t["speaker"],
                              "role": t["role"], "text": t["text"], "at": t["at"],
                              "expires": t.get("expires")} for t in turns],
                "memories": [{"id": f["id"], "anchor_source_id": f["turn"],
                              "source_turn_ids": sources[f["id"]], "kind": f["kind"],
                              "subject": f["subject"], "text": f["statement"], "at": f["at"],
                              "slot": f.get("slot"), "source": f.get("source", "rule"),
                              "owner": f.get("owner", speakers[f["turn"]].lower())} for f in facts],
            }


def _clean(text):
    text, _ = memory_guard.redact_secrets(str(text or "").strip())
    text, _ = memory_guard.redact_pii(text)
    return text


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def restore(store, rows: Iterable[Mapping]) -> dict:
    added = memories = sessions = 0
    pending, checkpoints, summaries = [], [], []
    with store._lock, write_txn(store.db):
        seen_sessions = set()
        for row in rows:
            sessions += 1
            if not isinstance(row, Mapping) or not isinstance(row.get("messages"), list):
                raise ConversationError("each line must be a session object with a messages list")
            version = row.get("format_version", 1)
            if not _integer(version) or version not in (1, VERSION):
                raise ConversationError("unsupported conversation archive version")
            session = str(row.get("session") or "")
            if session in seen_sessions:
                raise ConversationError("an archive must contain each session only once")
            seen_sessions.add(session)
            messages = row["messages"]
            if any(not isinstance(m, Mapping) for m in messages):
                raise ConversationError("messages must be objects")
            added += store.add(session, messages, session_at=row.get("started_at"),
                               extract_profile=version == 1)["added"]
            local = [dict(r) for r in store.db.execute("SELECT * FROM turns WHERE session=? ORDER BY idx", (session,))]
            if version == 1:
                if row.get("summary") and len(local) == len(messages):
                    store.set_summary(session, str(row["summary"]), "imported")
                continue
            mapping, order, seen_local_ids = {}, [], set()
            by_ref = {t["ref"]: t for t in local if t["ref"]}
            by_content = {(t["speaker"], t["at"], t["text"]): t for t in local}
            started = store.db.execute("SELECT started_at FROM sessions WHERE id=?", (session,)).fetchone()[0]
            for message in messages:
                source_id = message.get("source_id")
                if not _integer(source_id) or source_id in mapping:
                    raise ConversationError("messages require distinct positive source_id values")
                ref = str(message["id"])[:200] if message.get("id") not in (None, "") else None
                speaker = str(message.get("speaker") or message.get("name") or message.get("role") or "user") \
                    .strip()[:120]
                at = _iso(_moment(message.get("at") or message.get("timestamp") or started))
                text = _clean(message.get("text", message.get("content", "")))
                role = str(message.get("role") or "user").strip().lower()
                expires = _iso(_moment(message.get("expires")))
                turn = by_ref.get(ref) if ref else by_content.get((speaker, at, text))
                if turn is None or (turn["speaker"], turn["at"], turn["text"], turn["role"], turn["expires"]) \
                        != (speaker, at, text, role, expires):
                    raise ConversationError("archive message conflicts with an existing message id")
                if turn["id"] in seen_local_ids:
                    raise ConversationError("archive source ids must refer to distinct messages")
                seen_local_ids.add(turn["id"])
                mapping[source_id] = turn["id"]
                order.append(turn["id"])
            exact = order == [t["id"] for t in local]
            facts = row.get("memories")
            if not isinstance(facts, list):
                raise ConversationError("version 2 archives require a memories list")
            for fact in facts:
                if not isinstance(fact, Mapping) or not _integer(fact.get("id")):
                    raise ConversationError("memories require positive archive ids")
                pending.append((fact["id"], session, mapping, fact))
            checkpoint = row.get("extracted_source_id")
            if checkpoint is not None:
                if not _integer(checkpoint) or checkpoint not in mapping:
                    raise ConversationError("extraction checkpoint must cite an archived message")
                if exact:
                    checkpoints.append((session, next(t["idx"] for t in local if t["id"] == mapping[checkpoint])))
            if exact and row.get("summary"):
                meta = row.get("summary_meta") or {}
                if not isinstance(meta, Mapping):
                    raise ConversationError("summary_meta must be an object")
                summaries.append((session, str(row["summary"]), str(meta.get("method") or "imported"),
                                  _iso(_moment(meta.get("at")))))

        # Source archive ids specify insertion order across sessions, including
        # facts extracted later from older messages. Local ids may differ.
        used, archive_ids = set(), set()
        for archive_id, _session, mapping, fact in sorted(pending, key=lambda item: item[0]):
            if archive_id in archive_ids:
                raise ConversationError("memory archive ids must be unique")
            archive_ids.add(archive_id)
            raw_sources = fact.get("source_turn_ids")
            anchor = fact.get("anchor_source_id")
            if not isinstance(raw_sources, list) or not raw_sources \
                    or any(not _integer(t) or t not in mapping for t in raw_sources) \
                    or not _integer(anchor) or anchor not in raw_sources:
                raise ConversationError("memory sources and anchor must cite messages in their session")
            source_ids = {mapping[t] for t in raw_sources}
            turn = mapping[anchor]
            text = _clean(fact.get("text"))
            kind = str(fact.get("kind") or "fact").strip().lower()
            if not text or len(text) > profile.MAX_STATEMENT or kind not in FACT_KINDS:
                raise ConversationError("invalid archived memory text or kind")
            slot = str(fact["slot"]).strip().lower()[:80] if fact.get("slot") else None
            owner = str(fact.get("owner") or "").strip().lower()[:120]
            subject = _clean(fact.get("subject") or profile.subject_of(text))
            source = str(fact.get("source") or "imported")
            at = _iso(_moment(fact.get("at")))
            matches = [r[0] for r in store.db.execute(
                "SELECT id FROM facts WHERE turn=? AND kind=? AND subject=? AND owner=? "
                "AND statement_hash=? AND at IS ? AND slot IS ? AND source=? ORDER BY id",
                (turn, kind, subject, owner, fact_hash(text), at, slot, source)) if r[0] not in used]
            evidence = store.fact_source_ids(matches)
            fid = next((f for f in matches if set(evidence.get(f, [])) == source_ids), None)
            if fid is None:
                fid = store._insert_fact(turn, kind, subject, text, at, slot, source, owner)
                store.db.execute("DELETE FROM fact_sources WHERE fact=?", (fid,))
                store.db.executemany("INSERT INTO fact_sources VALUES (?, ?)", [(fid, tid) for tid in source_ids])
                memories += 1
            used.add(fid)
        for session, idx in checkpoints:
            store.set_meta(f"extracted:{session}", str(max(idx, int(store.get_meta(f"extracted:{session}") or -1))))
        for session, text, method, at in summaries:
            store.set_summary(session, text, method)
            if at:
                store.db.execute("UPDATE summaries SET at=? WHERE session=?", (at, session))
    return {"space": store.space, "sessions": sessions, "added": added, "memories": memories}
