"""Portable JSON hooks: recall on session start, capture explicit outcomes at end."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

from commontrace import assurance, memory_authority, paths, trace_io
from commontrace.client import MemoryClient


def handle(root: str, operation: str, payload: dict, *, agent_id: str) -> dict:
    with memory_authority.restricted_writer(agent_id, "agent"):
        return _handle(root, operation, payload, agent_id=agent_id)


def _handle(root: str, operation: str, payload: dict, *, agent_id: str) -> dict:
    if not isinstance(payload, dict) or not isinstance(agent_id, str) or not agent_id:
        raise ValueError("hooks require an agent id and JSON object")
    occasion = payload.get("occasion_id")
    if not isinstance(occasion, str) or not occasion or len(occasion) > 256:
        raise ValueError("hooks require a bounded occasion_id")
    memory = MemoryClient(root, agent_id=agent_id)
    if operation == "start":
        query = payload.get("query", "")
        if not isinstance(query, str) or len(query) > 20000:
            raise ValueError("query must be bounded text")
        return memory.reflect(query, budget=payload.get("budget", 600), occasion_id=occasion)
    if operation != "end":
        raise ValueError("unknown hook operation")
    context, solution = payload.get("context", ""), payload.get("solution", "")
    if not all(isinstance(v, str) and 0 < len(v) <= 20000 for v in (context, solution)):
        raise ValueError("session end requires bounded context and solution")
    succeeded = payload.get("succeeded")
    if succeeded is not None and not isinstance(succeeded, bool):
        raise ValueError("outcomes must be explicitly boolean")
    try:
        recalled = assurance.recall(root, memory._occasion(occasion))
        if recalled["scopes"] != memory.context:
            raise PermissionError("session recall scope mismatch")
        sources = recalled["source_traces"]
    except FileNotFoundError:
        sources = []  # A session may end without a preceding recall.
    extra = {"scopes": memory.context, "source_traces": sources, "extensions": {"profile": {"occasion_id": occasion}}}
    attribution = assurance.failure(root, memory._occasion(occasion),
        plan_valid=payload.get("plan_valid"), execution_valid=payload.get("execution_valid"),
        environment_valid=payload.get("environment_valid"), final=payload.get("final", True),
        outcome=float(succeeded) if succeeded is not None else None)
    extra["extensions"]["profile"]["failure_attribution"] = attribution["attribution"]
    # A completion or git commit is never silently labelled a success.
    if succeeded is not None and payload.get("final", True):
        extra["outcome"] = {"resolved": succeeded}
    filename = (trace_io.write_new(root, title=context[:200], context=context, solution=solution,
                                 tags=["agent-session"], agent_type="code", extra=extra,
                                 trace_id=hashlib.sha256((agent_id + "\0" + occasion).encode()).hexdigest())
                if payload.get("final", True) else None)
    return {"captured": bool(filename), "outcome_recorded":
            memory.outcome(occasion, succeeded) if succeeded is not None and payload.get("final", True) else False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("start", "end"))
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--dest")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(100001)
        if len(raw) > 100000:
            raise ValueError("hook payload too large")
        result = handle(paths.resolve_root(args.dest), args.operation, json.loads(raw), agent_id=args.agent_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
