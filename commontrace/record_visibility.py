"""Pure temporal and scope admission shared by local and hosted records."""

from __future__ import annotations

from datetime import datetime, timezone

from commontrace.memory_record import MemoryRecord


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def visible(record: MemoryRecord, scope: dict, *, statuses=("active",), at: str | None = None) -> bool:
    stamp = moment(at or now())
    return (
        record.is_latest
        and record.status in statuses
        and all(scope.get(k) == v for k, v in record.scope.items() if k != "labels")
        and set(record.scope.get("labels", [])) <= set(scope.get("labels", []))
        and (not record.valid_from or stamp >= moment(record.valid_from))
        and (not record.valid_until or stamp < moment(record.valid_until))
        and (not record.retracted_at or stamp < moment(record.retracted_at))
        and (not record.payload.get("forget_after") or stamp < moment(record.payload["forget_after"]))
    )


def bind_capture_sources(record: MemoryRecord) -> MemoryRecord:
    """Normalize source citations into causal data links for both record engines."""
    from dataclasses import replace

    sources = record.payload.get("source_message_ids", [])
    if not isinstance(sources, list) or any(not isinstance(source, str) or not source for source in sources):
        raise ValueError("source_message_ids must be a list of capture ids")
    if not sources:
        return record
    return replace(
        record,
        relations={
            **record.relations,
            "caused_by": list(dict.fromkeys([*record.relations.get("caused_by", []), *sources])),
        },
    )


def sanitize_record_content(record: MemoryRecord) -> MemoryRecord:
    """Redact content without changing owner scopes or logical source identifiers."""
    from commontrace import memory_guard

    data, _ = memory_guard.sanitize_metadata(record.to_dict(), pii=True)
    data.update(id=record.id, scope=record.scope, relations=record.relations)
    if "source_message_ids" in record.payload:
        data["payload"]["source_message_ids"] = record.payload["source_message_ids"]
    for name in ("source_versions", "source_fact_ids", "source_message_ids"):
        if name in record.provenance:
            data["provenance"][name] = record.provenance[name]
    return MemoryRecord(**data)


def code_evidence_current(root: str, evidence: list[dict]) -> bool:
    """Recheck bounded local code citations; file existence alone proves no claim."""
    import hashlib
    import os

    from commontrace import paths

    if not isinstance(evidence, list) or not 0 < len(evidence) <= 16:
        return False
    for citation in evidence:
        if not isinstance(citation, dict) or citation.get("method") != "path-and-quoted-symbol":
            return False
        relative = citation.get("path")
        if not isinstance(relative, str) or os.path.isabs(relative):
            return False
        filename = os.path.realpath(os.path.join(root, relative))
        if not paths.is_within_directory(root, filename) or not os.path.isfile(filename):
            return False
        try:
            with open(filename, "rb") as stream:
                content = stream.read(1000001)
        except OSError:
            return False
        if len(content) > 1000000 or hashlib.sha256(content).hexdigest() != citation.get("sha256"):
            return False
    return True


def scope_from_labels(labels: list[str]) -> dict:
    result, extra = {}, []
    for label in labels:
        key, _, value = label.partition(":")
        if key in ("tenant", "user", "agent", "session", "repo") and value:
            if key in result and result[key] != value:
                raise ValueError("ambiguous memory scope dimension: " + key)
            result[key] = value
        else:
            extra.append(label)
    if extra:
        result["labels"] = sorted(extra)
    return result
