"""Bounded, evidence-linked cold-start imports. Authorship never implies authority."""

from __future__ import annotations

import hashlib
import json
import os
import re

from commontrace import memory_guard, paths
from commontrace.memory_journal import Journal, now
from commontrace.memory_record import MemoryRecord


def messages(data):
    if isinstance(data, list):
        for item in data:
            yield from messages(item)
    elif isinstance(data, dict):
        if isinstance(data.get("messages"), list):
            yield from messages(data["messages"])
            return
        payload = (
            data.get("payload") if data.get("type") in ("response_item", "event_msg") else data.get("message", data)
        )
        if isinstance(payload, dict) and data.get("type") == "event_msg":
            subtype = payload.get("type")
            if subtype in ("user_message", "agent_message"):
                payload = {
                    **payload,
                    "role": "user" if subtype == "user_message" else "assistant",
                    "text": payload.get("message", ""),
                }
        if not isinstance(payload, dict):
            return
        role = payload.get("role") or (data.get("type") if data.get("type") in ("user", "assistant") else None)
        role = {"model": "assistant", "gemini": "assistant", "ai": "assistant", "human": "user"}.get(role, role)
        if role not in ("user", "assistant", "tool", "system"):
            return
        content = payload.get("content", payload.get("parts", payload.get("text", "")))
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        if isinstance(content, str) and content.strip():
            yield {
                "author": role,
                "quote": content[:20000],
                "external_id": data.get("uuid", payload.get("id")),
                "observed_at": data.get("timestamp", payload.get("timestamp", now())),
            }


def verify_code(root: str, quote: str) -> list[dict]:
    verified = []
    for match in re.finditer(r"\b([\w./-]+\.(?:py|ts|js|go|rs|java))\b", quote):
        relative = match.group(1)
        filename = os.path.realpath(os.path.join(root, relative))
        if not paths.is_within_directory(root, filename) or not os.path.isfile(filename):
            continue
        with open(filename, "rb") as handle:
            raw = handle.read(1000001)
        if len(raw) > 1000000:
            continue
        text = raw.decode("utf-8", errors="replace")
        snippets = re.findall(r"`([^`]+)`|\b(def \w+\(\))", quote)
        if not snippets or any((a or b) in text for a, b in snippets):
            verified.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "method": "path-and-quoted-symbol" if snippets else "path-exists-only",
                }
            )
    return verified


def import_history(root: str, filename: str, *, agent: str, scope: dict) -> dict:
    if agent not in ("claude", "codex", "cursor", "gemini"):
        raise ValueError("unsupported history format")
    with open(filename, "rb") as handle:
        raw = handle.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("history exceeds 20 MiB; split the export")
    text = raw.decode("utf-8")
    if filename.endswith((".jsonl", ".ndjson")):
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
    source = hashlib.sha256((os.path.realpath(filename) + json.dumps(scope, sort_keys=True)).encode()).hexdigest()
    journal = Journal(root)
    claims, imported = [], 0
    for index, row in enumerate(messages(data)):
        row, _ = memory_guard.sanitize_metadata(row, pii=True)
        identity = hashlib.sha256(
            (source + ":" + str(row["external_id"] or index) + ":" + row["quote"]).encode()
        ).hexdigest()
        verification = verify_code(root, row["quote"])
        claim = {
            **row,
            "citation": {"source_digest": source, "message_index": index},
            "code_verified": any(v["method"] == "path-and-quoted-symbol" for v in verification),
            "code_evidence": verification,
            "status": "unreviewed",
            "text": row["quote"],
        }
        claims.append(claim)
        if not journal.history(identity):
            journal.put(
                MemoryRecord(
                    identity,
                    "episode",
                    1,
                    claim,
                    status="quarantined",
                    scope=scope,
                    created_at=now(),
                    relations={"derives": ["coverage-" + source]},
                    provenance={
                        "source_agent": agent,
                        "source_digest": source,
                        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
                        "authored_by": row["author"],
                    },
                )
            )
            journal.capture(source, "HistoryMessage", claim, scope=scope, event_id=identity)
            imported += 1
    coverage_id = "coverage-" + source
    history = journal.history(coverage_id)
    journal.put(
        MemoryRecord(
            coverage_id,
            "episode",
            len(history) + 1,
            {
                "subtype": "coverage-ledger",
                "text": "History import coverage",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "messages_covered": len(claims),
                "bytes": len(raw),
                "agent": agent,
            },
            scope=scope,
            created_at=now(),
        )
    )
    return {"messages": imported, "claims": claims, "coverage_id": coverage_id, "lessons_seeded": 0}
