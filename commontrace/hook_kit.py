"""One bounded capture/injection core with generated coding-agent adapters."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import shlex
import sys
import time

from commontrace import injection_guard, kg_similarity, memory_guard, paths
from commontrace.configuration import getenv as _configuration_getenv
from commontrace.context_template import render_context as render_context
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord
from commontrace.search_recipes import _bm25, terms

EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PreCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)
AGENTS = ("claude", "codex", "cursor", "cline", "gemini", "kimi", "windsurf", "openhands")


def bootstrap_priority(record: MemoryRecord) -> float:
    """Weak offline Beta prior orders evaluation; it never grants relevance or promotion."""
    prior = record.payload.get("offline_prior", {})
    if record.status != "shadow" or not isinstance(prior, dict):
        return 1.0
    alpha, beta = prior.get("alpha", 1), prior.get("beta", 1)
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 10001
        for value in (alpha, beta)
    ):
        return 1.0
    return 1 + 0.02 * (alpha / (alpha + beta) - 0.5)


class HookKit:
    def __init__(self, root: str, agent_id: str, *, scope: dict | None = None, embedder=None):
        if not agent_id or len(agent_id) > 128:
            raise ValueError("hook agent id must be bounded")
        self.root, self.agent_id = root, agent_id
        self.scope = {**(scope or {}), "agent": agent_id}
        self.journal = Journal(root)
        if embedder is None:
            from commontrace import configuration

            try:
                embedder = (
                    _configuration_getenv("COMMONTRACE_GRAPH_EMBEDDER")
                    or configuration.load(root)["options"]["embedding"]
                )
            except (OSError, ValueError):
                embedder = "none"  # Capture remains available even with an invalid model configuration.
        self.embedder = embedder

    def handle(
        self, event: str, payload: dict, *, deadline_ms: float = 50, defer_delivery=False, capture_only=False
    ) -> dict:
        started = time.monotonic()
        empty = {"context": "", "injected": [], "shadow": [], "event": event}
        if event not in EVENTS or not isinstance(payload, dict):
            return empty
        session = (
            payload.get("session_id")
            or payload.get("occasion_id")
            or payload.get("thread_id")
            or payload.get("conversation_id")
        )
        # Normalize native host vocabulary before capture; keep the original fields as evidence.
        payload = {**payload}
        for canonical, aliases in {
            "tool_name": ("tool", "toolName"),
            "tool_input": ("arguments", "tool_args"),
            "error": ("error_message", "tool_error"),
            "prompt": ("user_prompt",),
        }.items():
            if canonical not in payload:
                for alias in aliases:
                    if alias in payload:
                        payload[canonical] = payload[alias]
                        break
        if event in ("SubagentStart", "SubagentStop") and not payload.get("parent_session_id"):
            child = payload.get("agent_id")
            if isinstance(session, str) and isinstance(child, str) and child:
                payload["parent_session_id"] = session
                session = session + ":" + child
                payload["session_id"] = session
        if not isinstance(session, str) or not session or len(session) > 256:
            return empty
        # An agent cannot spoof another principal, or inherit another agent's claims.
        run = hashlib.sha256(json.dumps([self.scope, session], sort_keys=True).encode()).hexdigest()

        def remaining():
            return (time.monotonic() - started) * 1000 < deadline_ms

        if not remaining():
            return empty
        source_identity = payload.get("event_id")
        payload, _ = memory_guard.sanitize_metadata(payload, pii=True)
        if source_identity is not None:
            payload["event_id"] = source_identity  # An identifier must remain stable across content redaction.
        captured = self.journal.capture(run, event, payload, scope=self.scope, event_id=payload.get("event_id"))
        if event == "SubagentStart":
            parent = payload.get("parent_session_id")
            if isinstance(parent, str):
                self.journal.inherit(
                    run, hashlib.sha256(json.dumps([self.scope, parent], sort_keys=True).encode()).hexdigest()
                )
            return empty
        if event in ("Stop", "SessionEnd", "SubagentStop"):
            from commontrace import jobs

            if event == "SessionEnd" and type(payload.get("succeeded")) is bool:
                from commontrace.client import MemoryClient

                MemoryClient(self.root, agent_id=self.agent_id).outcome(self.agent_id + ":" + run, payload["succeeded"])
            jobs.enqueue(
                self.root,
                "learn-session",
                {"session": run, "scope": self.scope},
                dedupe_key="learn:" + run + ":" + str(captured["sequence"]),
            )
            return empty
        if capture_only or event == "PostToolUse":
            return empty
        budget = payload.get("budget", 600)
        if not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= 8000:
            return empty
        query = " ".join(
            str(payload.get(key, ""))[:10000] for key in ("prompt", "query", "tool_name", "tool_input", "error")
        )
        relevant = self.journal.search(query, scope=self.scope, statuses=("active", "shadow"), limit=200)
        always = self.journal.search("", scope=self.scope, statuses=("active",), always=True, limit=1000)
        records = list({record.id: record for record in [*always, *relevant]}.values())
        studies = self.journal.evaluations(self.scope)
        candidates, fixed = [], []
        for record in records:
            if record.payload.get("canonical_path"):
                from commontrace import frontmatter, lesson_cache

                canonical = os.path.join(self.root, record.payload["canonical_path"])
                try:
                    paths.enforce_boundary(self.root, canonical)
                    fm, body = frontmatter.read(canonical)
                    digest = hashlib.sha256((json.dumps(fm, sort_keys=True, default=str) + body).encode()).hexdigest()
                    if digest != record.payload.get("source_digest") or not lesson_cache.fresh_eligible(
                        canonical, fm, slug=os.path.basename(canonical), body=body, root=self.root
                    ):
                        continue
                except (OSError, ValueError, TypeError):
                    continue
            text = record.payload.get("text", "")
            if not isinstance(text, str) or injection_guard.injection_labels({"text": text}):
                continue
            trigger = record.payload.get("trigger", {})
            if trigger.get("event") and trigger["event"] != event:
                continue
            if trigger.get("tool") and trigger["tool"] != payload.get("tool_name"):
                continue
            if trigger.get("error") and trigger["error"] not in str(payload.get("error", "")):
                continue
            if trigger.get("path") and trigger["path"] not in str(payload.get("tool_input", "")):
                continue
            if record.status == "active" and (
                record.kind == "directive"
                or record.kind == "lesson"
                and record.payload.get("lesson_kind") == "directive"
                or record.kind == "profile"
                and record.payload.get("critical")
            ):
                fixed.append((record, text))
            elif query.strip():
                candidates.append((record, text))
        fixed.sort(key=lambda pair: (-pair[0].payload.get("priority", 0), pair[0].id))
        selected, shadows = [], []
        provider = None
        if candidates and remaining():
            try:
                provider = kg_similarity.embedder(self.embedder)
                if provider is not None:
                    sparse = _bm25(terms(query), [terms(text) for _, text in candidates])
                    model = str(
                        getattr(
                            getattr(provider, "spec", None), "tag", getattr(provider, "model", type(provider).__name__)
                        )
                    )
                    vectors = self.journal.vectors(candidates, model)
                    missing = [i for i, vector in enumerate(vectors) if vector is None]
                    if missing:
                        for i, vector in zip(
                            missing, kg_similarity.embed(provider, [candidates[i][1] for i in missing], query=False)
                        ):
                            vectors[i] = vector
                            self.journal.cache_vector(candidates[i][0].id, model, candidates[i][1], vector)
                    qvector = kg_similarity.embed(provider, [query], query=True)[0]
                    dense = [kg_similarity.cosine(qvector, vector) for vector in vectors]
                    lexical_order = sorted(range(len(candidates)), key=lambda i: (-sparse[i], candidates[i][0].id))
                    dense_order = sorted(range(len(candidates)), key=lambda i: (-dense[i], candidates[i][0].id))
                    lex_rank, dense_rank = (
                        {i: rank + 1 for rank, i in enumerate(order)} for order in (lexical_order, dense_order)
                    )
                    scores = {
                        i: 1 / (60 + lex_rank[i]) + 1 / (60 + dense_rank[i])
                        for i in range(len(candidates))
                        if sparse[i] > 0 and dense[i] >= 0.6
                    }
                    top = max(scores.values(), default=0)
                    for i in sorted(
                        scores, key=lambda i: (-scores[i] * bootstrap_priority(candidates[i][0]), candidates[i][0].id)
                    ):
                        if scores[i] >= 0.012 and scores[i] >= 0.93 * top:
                            record, text = candidates[i]
                            from commontrace.measure import content_revision

                            controlled = studies.get(record.id) == content_revision(record.payload.get("text", ""))
                            if record.status == "shadow" and not controlled:
                                shadows.append(record.id)
                            else:
                                selected.append((record, text))
            except Exception:  # noqa: BLE001 - optional model/provider failures must not stop an agent
                selected, shadows = [], []
        if not remaining():
            return {**empty, "timed_out": True}
        template = payload.get("template")
        if template is not None:
            from commontrace.context_template import select

            slots = {name: [] for name in ("lessons", "profiles", "directives", "edges", "buffer")}
            by_id = {record.id: (record, text) for record, text in selected}
            for record, text in selected:
                slot = {"lesson": "lessons", "profile": "profiles", "directive": "directives"}.get(record.kind, "edges")
                slots[slot].append(
                    {"id": record.id, "text": text, "kind": record.payload.get("lesson_kind", record.kind)}
                )
            for entry in self.journal.buffer(run, scope=self.scope, limit=16):
                if entry["id"] == captured["id"]:
                    continue
                text = (
                    "Unextracted " + entry["event"] + " evidence: " + json.dumps(entry["payload"], ensure_ascii=False)
                )
                if memory_guard.scan_injection(text):
                    continue
                identity = "buffer:" + entry["id"]
                row = {"id": identity, "text": text, "kind": "episode"}
                slots["buffer"].append(row)
                by_id[identity] = (MemoryRecord(identity, "episode", 1, {"text": text}, scope=self.scope), text)
            chosen = select(template, slots)
            selected = [by_id[row["id"]] for row in chosen if not row["id"].startswith("buffer:")]
            fixed += [by_id[row["id"]] for row in chosen if row["id"].startswith("buffer:")]
        ordered = [*fixed, *selected]
        # Budget before claiming: unseen items that don't fit may be tried later.
        lines, eligible = [], []
        for record, text in ordered:
            line = "[" + record.id + "] " + text
            block = render_context([*lines, line], budget=budget)
            if html.escape(memory_guard.sanitize_metadata({"text": line}, pii=True)[0]["text"]) in block:
                lines.append(line)
                eligible.append(record.id)
        from commontrace.measure import CausalMemory

        measured = CausalMemory(
            lambda _: [
                record
                for record, _ in selected
                if record.id in eligible
                and (event == "PreCompact" or record.id in self.journal.unclaimed(run, eligible))
            ],
            root=self.root,
            key=lambda record: record.id,
            text=lambda record: record.payload["text"],
            screen=True,
            check_every=1,
        ).recall_detailed(query, occasion_id=self.agent_id + ":" + run, record=False)
        allowed = {record.id for record in measured.items} | {record.id for record, _ in fixed}
        eligible_lines = [(identity, line) for identity, line in zip(eligible, lines) if identity in allowed]
        eligible, lines = [pair[0] for pair in eligible_lines], [pair[1] for pair in eligible_lines]
        if not remaining():
            return {**empty, "timed_out": True}
        fresh = eligible if event == "PreCompact" else self.journal.unclaimed(run, eligible)
        undecided = set(self.journal.unclaimed(run, [row["lesson"] for row in measured.assignments]))
        assignments = [row for row in measured.assignments if row["lesson"] in undecided]
        token = self.journal.offer_delivery(run, fresh, assignments, scope=self.scope) if fresh or assignments else None
        if token and not defer_delivery:
            self.journal.commit_delivery(token, scope=self.scope)
        lines = [line for line, identity in zip(lines, eligible) if identity in fresh]
        context = render_context(lines, budget=budget)
        if shadows:
            self.journal.capture(run, "ShadowRecall", {"ids": shadows, "injected": False}, scope=self.scope)
        return {
            **empty,
            "context": context,
            "delivery_token": token if defer_delivery else None,
            "injected": fresh,
            "shadow": shadows,
            "elapsed_ms": (time.monotonic() - started) * 1000,
            "dense_available": provider is not None,
        }

    def manifest(self, agent: str) -> dict:
        if agent not in AGENTS:
            raise ValueError("unsupported coding agent")
        import shutil

        node = shutil.which("node")
        executable = (
            [node, os.path.join(os.path.dirname(__file__), "ui", "hook-bridge.mjs")]
            if node
            else [sys.executable, "-m", "commontrace.hook_kit"]
        )
        command = shlex.join(
            [
                *executable,
                *(["--python", sys.executable] if node else []),
                "--dest",
                os.path.abspath(self.root),
                "--agent-id",
                self.agent_id,
                "--scope",
                json.dumps(self.scope, sort_keys=True),
            ]
        )
        return {
            "version": 1,
            "agent": agent,
            "events": {event: command + " --event " + event for event in EVENTS},
            "timeout_ms": 50,
            "fallback": "no injection",
            "input": "JSON stdin",
            "output": "JSON context",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--event", choices=EVENTS, required=True)
    parser.add_argument("--format", choices=AGENTS)
    parser.add_argument("--scope", default="{}")
    parser.add_argument("--capture-only", action="store_true")
    args = parser.parse_args(argv)
    if args.capture_only:
        try:
            from commontrace.native_hooks import normalize

            payload = json.loads(sys.stdin.read(100001))
            event, payload = normalize(args.format, args.event, payload)
            HookKit(paths.resolve_root(args.dest), args.agent_id, scope=json.loads(args.scope)).handle(
                event, payload, deadline_ms=10000, capture_only=True
            )
        except Exception:  # noqa: BLE001 - detached capture must not break host execution
            return 0
        return 0
    # Bound the whole operation, including slow providers, without waiting for a timed-out thread.
    import queue
    import threading

    result = queue.Queue()

    def work():
        try:
            payload = json.loads(sys.stdin.read(100001))
            from commontrace.native_hooks import normalize

            event, payload = normalize(args.format, args.event, payload)
            root, scope = paths.resolve_root(args.dest), json.loads(args.scope)
            from commontrace.hook_service import request

            reply = request(root, args.agent_id, scope, event, payload)
            if reply is None:
                reply = HookKit(root, args.agent_id, scope=scope).handle(event, payload)
            result.put(reply)
        except Exception:  # noqa: BLE001 - hook failures must not break the agent
            result.put({"context": "", "injected": []})

    threading.Thread(target=work, daemon=True).start()
    try:
        reply = result.get(timeout=0.05)
    except queue.Empty:
        reply = {"context": "", "injected": [], "timed_out": True}
    from commontrace.native_hooks import format_reply

    output = format_reply(args.format, args.event, reply)
    print(output if isinstance(output, str) else json.dumps(output), flush=True)
    if reply.get("delivery_token"):
        from commontrace.hook_service import acknowledge

        acknowledge(paths.resolve_root(args.dest), args.agent_id, json.loads(args.scope), reply["delivery_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
