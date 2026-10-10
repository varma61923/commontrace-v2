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
