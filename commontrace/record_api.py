"""Scoped record operations shared by local SDK and authenticated gateway."""

from dataclasses import replace

from commontrace.framework_middleware import scope_from_labels
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord
from commontrace.record_visibility import visible


def operate(root: str, operation: str, body: dict, *, context: list[str], untrusted=False) -> dict:
    journal = Journal(root)
    scope = scope_from_labels(context)
    if operation == "record-put":
        record = MemoryRecord(**body["record"])
        if untrusted:
            if any(scope.get(key) != value for key, value in record.scope.items()):
                raise PermissionError("record scope cannot override authenticated context")
            record = replace(record, scope=scope)
            if record.kind in ("lesson", "directive"):
                record = replace(record, status="quarantined")
        return {"record": journal.put(record).to_dict()}
    if operation == "record-search":
        return {
            "results": [
                record.to_dict()
                for record in journal.search(body.get("query", ""), scope=scope, limit=body.get("limit", 20))
            ]
        }
    if operation == "record-forget":
        history = journal.history(body["id"])
        if not history:
            raise ValueError("record not found")
        if untrusted and history[-1].scope != scope:
            raise PermissionError("only the owning scoped principal can erase this record")
        # Visibility excludes inactive versions, so erasure checks scope independently.
        if history and not visible(
            replace(history[-1], status="active", is_latest=True, valid_from=None, valid_until=None, payload={}), scope
        ):
            raise PermissionError("record does not belong to this context")
        return journal.forget(body["id"], reason=body["reason"])
    raise ValueError("unsupported record operation")
