"""Idempotent project-scoped onboarding and generated hook manifests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

from commontrace import _jsonl, fact_store, frontmatter, lesson_cache, paths
from commontrace.hook_kit import AGENTS, HookKit
from commontrace.memory_journal import Journal, now
from commontrace.memory_record import MemoryRecord

CURSOR = {
    "SessionStart": "sessionStart",
    "UserPromptSubmit": "beforeSubmitPrompt",
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "PostToolUseFailure": "postToolUseFailure",
    "PreCompact": "preCompact",
    "Stop": "stop",
    "SessionEnd": "sessionEnd",
}
GEMINI = {
    "SessionStart": "SessionStart",
    "UserPromptSubmit": "BeforeAgent",
    "PreToolUse": "BeforeTool",
    "PostToolUse": "AfterTool",
    "PreCompact": "PreCompress",
    "SessionEnd": "SessionEnd",
}


def native_hooks(manifest: dict) -> dict:
    agent = manifest["agent"]
    if agent in ("claude", "codex"):
        return {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": command + " --format " + agent, "timeout": 1}]}]
                for event, command in manifest["events"].items()
            }
        }
    if agent == "cursor":
        return {
            "version": 1,
            "hooks": {
                CURSOR[event]: [{"command": command + " --format cursor", "timeout": 1}]
                for event, command in manifest["events"].items()
                if event in CURSOR
            },
        }
    if agent == "gemini":
        return {
            "hooks": {
                GEMINI[event]: [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "name": "commontrace",
                                "type": "command",
                                "command": command + " --format gemini",
                                "timeout": 1000,
                            }
                        ],
                    }
                ]
                for event, command in manifest["events"].items()
                if event in GEMINI
            }
        }
    from commontrace.native_hooks import special_json

    return special_json(manifest) or {}


def merge_hooks(filename: str, generated: dict):
    with _jsonl.locked(filename):
        existing = {}
        if os.path.exists(filename):
            with open(filename, encoding="utf-8") as stream:
                existing = json.load(stream)
        if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
            raise ValueError("existing hook configuration is not an object")
        hooks = existing.setdefault("hooks", {})
        for event, entries in generated["hooks"].items():
            previous = hooks.get(event, [])
            if not isinstance(previous, list):
                raise ValueError("existing hook entries must be a list")
            hooks[event] = [
                entry
                for entry in previous
                if "commontrace.hook_kit" not in json.dumps(entry) and "hook-bridge.mjs" not in json.dumps(entry)
            ] + entries
        _jsonl.write_json(filename, {**generated, **existing})


def bridge_lessons(root: str, *, scope: dict) -> int:
    journal = Journal(root)
    lessons, _ = lesson_cache.load_active_with_terms(root, None, reader=frontmatter.read)
    count = 0
    for path, fm in lessons:
        _, body = frontmatter.read(path)
        if not lesson_cache.fresh_eligible(path, fm, slug=os.path.basename(path), body=body, root=root):
            continue
        identity = "lesson-" + hashlib.sha256((path + json.dumps(scope, sort_keys=True)).encode()).hexdigest()
        digest = hashlib.sha256((json.dumps(fm, sort_keys=True, default=str) + body).encode()).hexdigest()
        history = journal.history(identity)
        if history and history[-1].payload.get("source_digest") == digest:
            continue
        record = MemoryRecord(
            identity,
            "lesson",
            len(history) + 1,
            {
                "text": fm.get("description", "") + "\n" + body[:10000],
                "canonical_path": os.path.relpath(path, root),
                "source_digest": digest,
                "lesson_kind": "procedure",
            },
            scope=scope,
            provenance={"source_traces": fm.get("evidence", []), "canonical_lesson": fm.get("id")},
            created_at=now(),
        )
        journal.put(record)
        count += 1
    return count


def setup(root: str, *, config: dict, agents: list[str] | None = None) -> dict:
    started = time.monotonic()
    storage = fact_store.migrate(root) if not fact_store.enabled(root) else {"backend": "sqlite-wal"}
    repo = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    selected = agents or list(config.get("agents", {})) or [agent for agent in AGENTS if shutil.which(agent)]
    installed, imported = [], []
    for agent in selected:
        if agent not in AGENTS:
            raise ValueError("unsupported coding agent")
        identity = agent + "-" + repo
        kit = HookKit(root, identity, scope={"repo": repo})
        manifest = kit.manifest(agent)
        directory = os.path.join(root, "distribution", "agents", identity)
        filename = os.path.join(directory, "hooks.json")
        paths.enforce_boundary(root, filename)
        _jsonl.write_json(filename, manifest)
        generated = native_hooks(manifest)
        agent_config = config.get("agents", {}).get(agent, {})
        from commontrace.native_hooks import PROJECT_CONFIGS, install_special

        target = agent_config.get("config_path", PROJECT_CONFIGS[agent])
        special = install_special(root, agent, os.path.join(root, target), manifest)
        native = bool(target and generated) or bool(special)
        if target and generated:
            target = os.path.join(root, target)
            paths.enforce_boundary(root, target)
            merge_hooks(target, generated)
        count = bridge_lessons(root, scope={"agent": identity, "repo": repo})
        installed.append(
            {
                "agent": agent,
                "agent_id": identity,
                "manifest": filename,
                "native_hooks_installed": native,
                "lessons_bridged": count,
                "integration": "native" if native else "portable event/middleware bridge",
                "context_native": agent != "windsurf",
                **(special or {}),
            }
        )
    from commontrace.history_import import import_history

    history_sources = list(config.get("histories", []))
    if not history_sources:
        # Project exports only; external host locations must be supplied explicitly.
        for agent in ("claude", "codex", "cursor", "gemini"):
            directory = os.path.join(root, ".commontrace", "history", agent)
            if os.path.isdir(directory):
                for name in sorted(os.listdir(directory))[:16]:
                    if name.endswith((".json", ".jsonl", ".ndjson")):
                        history_sources.append(
                            {"agent": agent, "path": os.path.relpath(os.path.join(directory, name), root)}
                        )
    for source in history_sources:
        if not isinstance(source, dict) or set(source) != {"agent", "path"}:
            raise ValueError("history entries require agent and path")
        identity = source["agent"] + "-" + repo
        result = import_history(
            root, os.path.join(root, source["path"]), agent=source["agent"], scope={"agent": identity, "repo": repo}
        )
        imported.append({"agent": source["agent"], "messages": result["messages"]})
    first_lesson = None
    for item in installed:
        rows = Journal(root).search("", scope={"agent": item["agent_id"], "repo": repo}, kinds=("lesson",), limit=1)
        if rows:
            first_lesson = {"id": rows[0].id, "agent": item["agent"], "text": rows[0].payload["text"]}
            break
    return {
        "first_lesson": first_lesson,
        "first_value_status": "lesson ready" if first_lesson else "waiting for an evaluated run or reviewed import",
        "storage": storage["backend"],
        "agents": installed,
        "history": imported,
        "elapsed_seconds": time.monotonic() - started,
        "repo": repo,
        "fabricated_lessons": 0,
    }
