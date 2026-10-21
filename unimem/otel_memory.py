"""UniMem opt-in OpenTelemetry GenAI memory spans with admission first.

Convention snapshot: opentelemetry semantic-conventions commit below, GenAI
memory spans. Core does not require the SDK; the recorder returns schema data
without exporting when no tracer is configured. No span for invisible records.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal

CONVENTION_VERSION = "f3c6d0c9bb66ebd474668a3a0be25c61226f02c2"
OPERATIONS = {
    "create": "create_memory",
    "search": "search_memory",
    "update": "update_memory",
    "upsert": "upsert_memory",
    "delete": "delete_memory",
}
EXTENSIONS = {
    "writer.agent",
    "delegation.chain_digest",
    "lineage.root",
    "lineage.depth",
    "label.ceiling",
    "isolation.level",
    "read.version",
    "read.staleness_ms",
    "policy.decision",
    "holdout.arm",
    "holdout.propensity",
    "n_eff",
}


def _path(root: str) -> str:
    from unimem import paths

    filename = os.path.join(paths.memory_dir(root), "otel-memory.json")
    paths.enforce_boundary(root, filename)
    return filename


def configure(root: str, *, enabled: bool = False, include_content: bool = False) -> None:
    from unimem import _jsonl
    from unimem import nextwave_store as store

    store.operator()
    if type(enabled) is not bool or type(include_content) is not bool:
        raise ValueError("UniMem telemetry configuration requires boolean flags")
    if not enabled:
        try:
            os.unlink(_path(root))
        except FileNotFoundError:
            pass
        return
    _jsonl.write_json(_path(root), {"enabled": enabled, "include_content": include_content})


def config(root: str) -> dict[str, Any]:
    path = os.path.join(root, "memory", "otel-memory.json")
    if not os.path.isfile(path):
        return {"enabled": False, "include_content": False}
    path = _path(root)
    with open(path, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("UniMem telemetry configuration is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or any(
        type(value.get(k, False)) is not bool for k in ("enabled", "include_content")
    ):
        raise ValueError("UniMem telemetry flags must be boolean")
    return value


def attributes(
    root: str,
    operation: str,
    identities: Sequence[str],
    *,
    context: Sequence[str] = (),
    extensions: dict[str, Any] | None = None,
    query: str = "",
    include_content: bool = False,
) -> dict[str, Any]:
    from unimem import frontier_protocol as fp
    from unimem import lineage, memory_authority, telemetry
    from unimem import nextwave_store as store

    if operation not in OPERATIONS:
        raise ValueError("UniMem unknown memory span operation")
    fp.count(len(identities), 0, 512)
    visible = store.admit(root, identities, context=context)
    if not visible:
        return {}
    # Admission precedes every record attribute, including count, writer and
    # lineage roots. Do not attach a request's denied IDs or a denied query.
    ids = sorted(visible)
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": OPERATIONS[operation],
        "gen_ai.memory.store.id": fp.digest(os.path.realpath(root)),
        "gen_ai.memory.record.count": len(ids),
        "unimem.convention.version": CONVENTION_VERSION,
        "unimem.reader.agent": memory_authority.WRITER.get()[0],
    }
    if len(ids) == 1:
        attrs["gen_ai.memory.record.id"] = ids[0]
    else:
        attrs["unimem.memory.record.ids"] = ids
    supplied = extensions or {}
    if set(supplied) - EXTENSIONS:
        raise ValueError("UniMem unknown causal span extension")
    for key, value in supplied.items():
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError("UniMem causal span attributes must be scalar")
        if isinstance(value, float):
            fp.number(value, -1e12, 1e12)
        attrs["unimem." + key] = telemetry.redact(value)
    records = {i: lineage.record(root, i) for i in ids}
    roots = sorted({source for record in records.values() for source in record.get("root_origins", [])})
    actors = sorted({record.get("actor", "unknown") for record in records.values()})
    attrs["unimem.writer.agent"] = actors[0] if len(actors) == 1 else actors[:128]
    attrs["unimem.lineage.root"] = roots[:128]
    pending = [(i, 0) for i in ids]
    seen: dict[str, int] = {}
    depth = 0
    while pending:
        identity, level = pending.pop()
        if level <= seen.get(identity, -1):
            continue
        seen[identity] = level
        fp.count(len(seen), 0, 2000, "UniMem telemetry lineage nodes")
        depth = max(depth, level)
        if level < 16:
            record = records.get(identity) or lineage.record(root, identity)
            pending.extend((parent["id"], level + 1) for parent in record.get("inputs", []))
    attrs["unimem.lineage.depth"] = depth
    attrs["unimem.lineage.depth_truncated"] = depth >= 16
    from unimem import capabilities

    if capabilities.CURRENT.get() is not None:
        from unimem import capability_io

        delegated = capability_io.current(root)
        if delegated is None:
            raise PermissionError("UniMem delegated memory span lost its request context")
        attrs["unimem.delegation.chain_digest"] = delegated["verified"]["chain_digest"]
        attrs["unimem.label.ceiling"] = delegated["verified"]["caveats"]["ceiling"]
    from unimem import team_isolation

    team = team_isolation.REQUEST.get()
    if team and "validated_at" in team:
        from datetime import datetime

        attrs["unimem.isolation.level"] = team["level"]
        attrs["unimem.read.version"] = team["read_version"]
        attrs["unimem.read.staleness_ms"] = max(0.0, (
            datetime.fromisoformat(fp.now()) - datetime.fromisoformat(team["validated_at"])).total_seconds() * 1000)
    attrs["unimem.policy.decision"] = "allow"
    if include_content and len(visible) == len(set(identities)):
        # The non-standard content attributes are opt-in and bounded. The
        # caller owns query text; it is omitted when any requested ID is denied.
        attrs["gen_ai.memory.query.text"] = telemetry.redact(fp.text(query, "query", 2000)) if query else ""
        attrs["gen_ai.memory.records"] = json.dumps(
            [{"id": i, "content": telemetry.redact(value.get("text", ""))} for i, value in list(visible.items())[:64]],
            sort_keys=True,
            separators=(",", ":"),
        )
    return attrs


@contextmanager
def operation(
    root: str,
    action: str,
    identities: Sequence[str],
    *,
    context: Sequence[str] = (),
    extensions: dict[str, Any] | None = None,
    query: str = "",
    settings: dict[str, Any] | None = None,
    start_time: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Admit before opening a span; use around deletes while data is still live."""
    from unimem import telemetry

    settings = config(root) if settings is None else settings
    attrs = (
        attributes(
            root,
            action,
            identities,
            context=context,
            extensions=extensions,
            query=query,
            include_content=settings.get("include_content", False),
        )
        if settings.get("enabled")
        else {}
    )
    tracer_factory: Callable[[], Any] = telemetry._tracer
    tracer = tracer_factory() if attrs else None
    if tracer is None:
        yield {"span_created": False, "attributes": attrs}
        return
    with tracer.start_as_current_span(
        OPERATIONS[action], start_time=start_time, record_exception=False, set_status_on_exception=False
    ) as span:
        # telemetry.Span.set prefixes unimem.*; emit standard keys directly.
        for key, value in attrs.items():
            span.set_attribute(key, value)
        try:
            yield {"span_created": True, "attributes": attrs}
        except Exception as exc:
            span.set_attribute("error.type", type(exc).__name__)
            try:
                from opentelemetry.trace import Status, StatusCode
            except ImportError:
                pass
            else:
                if hasattr(span, "set_status"):
                    span.set_status(Status(StatusCode.ERROR))
            raise


def observed(action: str) -> Callable[..., Any]:
    """Opt-in post-operation instrumentation for fact writes and searches."""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(function)
        def call(root: str, *args: Any, **kwargs: Any) -> Any:
            from unimem import telemetry

            try:
                settings = config(root)
            except (OSError, ValueError, TypeError):
                telemetry.count("unimem_telemetry_failures", operation=action)
                return function(root, *args, **kwargs)
            if not settings.get("enabled"):
                return function(root, *args, **kwargs)
            started = time.time_ns()
            result = function(root, *args, **kwargs)
            try:
                bound = inspect.signature(function).bind(root, *args, **kwargs).arguments
                context = bound.get("context") or bound.get("scopes") or []
                if action == "search":
                    ids = [r["id"] for r in result]
                elif isinstance(result, list):
                    ids = [r[0].id for r in result]
                    context = sorted({scope for item in bound.get("items", []) for scope in item.get("scopes", [])})
                elif isinstance(result, tuple):
                    ids = [result[0].id]
                else:
                    ids = [result.id]
                with operation(
                    root,
                    action,
                    ids[:512],
                    context=context,
                    query=bound.get("query", ""),
                    settings=settings,
                    start_time=started,
                ):
                    pass
            except (
                OSError,
                ValueError,
                TypeError,
                PermissionError,
                RuntimeError,
                AttributeError,
                KeyError,
                IndexError,
            ):
                # Canonical work already succeeded. Do not turn an optional
                # telemetry failure into a false write failure or retry.
                telemetry.count("unimem_telemetry_failures", operation=action)
            return result

        return call

    return decorate


class _OptionalSpan:
    """Never let exporter enter/exit failures change canonical operation results."""

    def __init__(self, manager: Any, action: str) -> None:
        self.manager, self.action, self.started = manager, action, False

    def __enter__(self) -> None:
        from unimem import telemetry

        try:
            self.manager.__enter__()
            self.started = True
        except Exception:  # Optional exporter boundary; never alter the governed operation.
            telemetry.count("unimem_telemetry_failures", operation=self.action)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Literal[False]:
        from unimem import telemetry

        if self.started:
            try:
                self.manager.__exit__(exc_type, exc, traceback)
            except Exception:  # Optional exporter boundary; never alter the governed operation.
                telemetry.count("unimem_telemetry_failures", operation=self.action)
        return False


def observed_delete(function: Callable[..., Any]) -> Callable[..., Any]:
    """Instrument admitted deletion targets before revocation removes visibility."""

    @functools.wraps(function)
    def call(root: str, identity: str, *args: Any, **kwargs: Any) -> Any:
        from unimem import memory_control, telemetry

        try:
            settings = config(root)
        except (OSError, ValueError, TypeError):
            telemetry.count("unimem_telemetry_failures", operation="delete")
            return function(root, identity, *args, **kwargs)
        if not settings.get("enabled"):
            return function(root, identity, *args, **kwargs)
        required = memory_control.REQUIRED_PRINCIPAL_SCOPE.get()
        labels = [required] if required else []
        with _OptionalSpan(operation(root, "delete", [identity], context=labels, settings=settings), "delete"):
            return function(root, identity, *args, **kwargs)

    return call


def outcome(root: str, occasion_id: str, *, context: Sequence[str] = ()) -> None:
    """Opt-in vendor outcome spans from canonical arms, never a causal verdict."""
    from unimem import telemetry

    try:
        settings = config(root)
        if not settings.get("enabled"):
            return
        from unimem import allocation, integrity, memory_control
        from unimem.commands.experiment_cmd import _load

        rows, _, corrupt = _load(root)
        selected = [row for row in rows if row.occasion_id == occasion_id]
        if corrupt or not integrity.audit(selected, schedules=allocation.history(root)).readable:
            return
        required = memory_control.REQUIRED_PRINCIPAL_SCOPE.get()
        labels = list(context) + ([required] if required else [])
        for row in selected[:256]:
            if row.succeeded is None:
                continue
            attrs = attributes(root, "search", [row.lesson], context=labels)
            if not attrs:
                continue
            attrs.pop("gen_ai.operation.name", None)
            attrs.update(
                {
                    "unimem.occasion.id": occasion_id,
                    "unimem.holdout.arm": "treated" if row.injected else "control",
                    "unimem.holdout.propensity": 1 - row.rate,
                    "unimem.outcome.succeeded": row.succeeded,
                }
            )
            factory: Callable[[], Any] = telemetry._tracer
            tracer = factory()
            if tracer is None:
                return
            with tracer.start_as_current_span("unimem.task.outcome", record_exception=False) as span:
                for key, value in attrs.items():
                    span.set_attribute(key, value)
    except Exception:  # Telemetry observes a committed outcome and must never request a duplicate write.
        telemetry.count("unimem_telemetry_failures", operation="outcome")
