"""Idempotent project-scoped onboarding and generated hook manifests."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time

from unimem import _jsonl, fact_store, frontmatter, lesson_cache, paths
from unimem.hook_kit import AGENTS, HookKit
from unimem.memory_journal import Journal, now
from unimem.memory_record import MemoryRecord

# Codex reports tool failures in PostToolUse.tool_response instead of a separate event.
CODEX_NATIVE_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "SessionEnd",
    }
)

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

# `up` discovers only the hosts this package knows how to wire. A CLI on PATH
# or an existing host configuration directory is installation evidence; it is
# not permission to edit that host's global configuration.
AGENT_BINARIES = {
    "claude": ("claude",),
    "codex": ("codex",),
    "cursor": ("cursor-agent", "cursor"),
    "cline": ("cline", "cline-cli"),
    "gemini": ("gemini",),
    "kimi": ("kimi",),
    "windsurf": ("windsurf",),
    "openhands": ("openhands",),
}
AGENT_CONFIG_ROOTS = {
    "claude": (".claude",),
    "codex": (".codex",),
    "cursor": (".cursor",),
    "cline": (".clinerules", ".cline"),
    "gemini": (".gemini",),
    "kimi": (".kimi-code",),
    "windsurf": (".windsurf",),
    "openhands": (".openhands",),
}
GLOBAL_ONLY_AUTO_AGENTS = frozenset({"kimi"})


def detect_installed_agents(root: str, *, path: str | None = None, home: str | None = None) -> dict:
    """Detect supported hosts from PATH and known config roots, without writing files."""
    import shutil

    resolved_root = os.path.abspath(root)
    selected_path = os.environ.get("PATH", "") if path is None else path
    selected_home = os.path.expanduser("~") if home is None else os.path.abspath(home)
    evidence: dict[str, list[str]] = {}
    for agent in AGENTS:
        found = []
        for binary in AGENT_BINARIES[agent]:
            if shutil.which(binary, path=selected_path):
                found.append("PATH:" + binary)
        for config_root in AGENT_CONFIG_ROOTS[agent]:
            project_path = os.path.join(resolved_root, config_root)
            if os.path.isdir(project_path):
                found.append("project-config:" + config_root)
            home_path = os.path.join(selected_home, config_root)
            if os.path.isdir(home_path):
                found.append("host-config:" + config_root)
        if agent == "kimi" and os.environ.get("KIMI_CODE_HOME"):
            kimi_home = os.path.expanduser(os.path.expandvars(os.environ["KIMI_CODE_HOME"]))
            if os.path.isfile(os.path.join(kimi_home, "config.toml")):
                found.append("host-config:KIMI_CODE_HOME")
        if found:
            evidence[agent] = found
    detected = [agent for agent in AGENTS if agent in evidence]
    project_scoped = [agent for agent in detected if agent not in GLOBAL_ONLY_AUTO_AGENTS]
    return {
        "detected": detected,
        "project_scoped": project_scoped,
        "requires_global_opt_in": [agent for agent in detected if agent in GLOBAL_ONLY_AUTO_AGENTS],
        "evidence": evidence,
    }


def native_hooks(manifest: dict) -> dict:
    agent = manifest["agent"]
    if agent in ("claude", "codex"):
        events = manifest["events"].items()
        if agent == "codex":
            events = ((event, command) for event, command in events if event in CODEX_NATIVE_EVENTS)
        return {
            "hooks": {
                event: [{"hooks": [{"type": "command", "command": command + " --format " + agent, "timeout": 1}]}]
                for event, command in events
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
                                "name": "unimem",
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
    from unimem.native_hooks import special_json

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
                if not any(
                    name in json.dumps(entry)
                    for name in ("unimem.hook_kit", "hook-bridge.mjs", "hook_bridge.py")
                )
            ] + entries
        _jsonl.write_json(filename, {**generated, **existing})


def stage_project_rule(root: str, filename: str) -> dict:
    """Stage exact, explicitly supplied text; only lesson approve can admit it."""
    from unimem import approval, lesson_io, memory_guard, templates
    from unimem.ingest import _read_bytes

    source = os.path.abspath(filename)
    raw = _read_bytes(source, 10000)
    if not raw.strip() or len(raw) > 10000:
        raise ValueError("source must contain between 1 and 10000 bytes")
    body = raw.decode("utf-8")
    if not body.strip():
        raise ValueError("source must contain non-whitespace text")
    if memory_guard.scan_fields({"body": body}).should_block:
        raise ValueError("source failed the memory content guard")
    digest = hashlib.sha256(raw).hexdigest()
    slug = "project_rule_" + digest[:24]
    target = os.path.join(paths.lessons_dir(root), "lesson_" + slug + ".md")
    paths.enforce_boundary(root, target)
    with frontmatter.locked(target):
        if os.path.exists(target):
            fm, _ = frontmatter.read(target)
            if fm.get("provenance", {}).get("source_sha256") != digest:
                raise ValueError("candidate identity conflicts with an existing lesson")
        else:
            fm = templates.lesson_frontmatter(
                slug=slug, description=next(line.strip() for line in body.splitlines() if line.strip())[:240],
                agent_type=paths.store_agent_type(root), domain="other", tags=["project-rule"],
                applies_when="Working in this repository.", do_not_apply_when="Working outside this repository.",
                importance_rationale="Project rule supplied for review; no measured lift is claimed.",
                source_traces=[], status="review",
            )
            fm["lesson_kind"] = "directive"
            fm["provenance"] = {"source_kind": "explicit_project_rule", "source_path": source, "source_sha256": digest}
            lesson_io.write_lesson(
                target, fm, body, root=root, actor=approval.AGENT_ACTOR_PREFIX + "unimem-up",
                reason="Explicit project rule staged for review",
            )
    return {
        "slug": slug, "path": target, "status": fm["status"], "source_sha256": digest,
        "approval_command": shlex.join([
            "unimem", "lesson", "approve", slug, "--dest", root,
            "--rationale", "Reviewed this project rule.",
        ]),
    }


def bridge_lessons(root: str, *, scope: dict) -> int:
    from unimem import lesson_io

    journal = Journal(root)
    lessons, _ = lesson_cache.load_active_with_terms(root, None, reader=frontmatter.read)
    count = 0
    for path, _cached_fm in lessons:
        # Cached metadata omits the receipt; admission and hashing need the full file.
        fm, body = frontmatter.read(path)
        if fm.get("provenance", {}).get("source_kind") == "explicit_project_rule" and not fm.get("approval_receipt"):
            continue
        if not lesson_cache.fresh_eligible(
            path, fm, slug=lesson_io.canonical_slug(os.path.basename(path)), body=body, root=root,
        ):
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
                "lesson_kind": fm.get("lesson_kind", "procedure"),
            },
            scope=scope,
            provenance={"source_traces": fm.get("source_traces", []), "canonical_lesson": fm.get("name")},
            created_at=now(),
        )
        journal.put(record)
        count += 1
    return count


def lessons_signature(root: str) -> tuple[int, int]:
    """(lesson file count, newest mtime): a cheap stamp that changes when any lesson is added or edited."""
    try:
        entries = [e for e in os.scandir(paths.lessons_dir(root)) if e.name.endswith(".md")]
    except OSError:
        return (0, 0)
    newest = 0
    for entry in entries:
        try:
            newest = max(newest, entry.stat().st_mtime_ns)
        except OSError:
            continue
    return (len(entries), newest)


def bridge_all(root: str) -> int:
    """Bridge approved lessons for every agent identity this store has onboarded.

    `setup` bridges once, so a lesson approved afterwards would never reach the hook injector. The
    broker calls this whenever `lessons_signature` changes; each lesson is journaled once per content
    digest, so repeating it is cheap and idempotent. Revocation and edits need no help here: the
    injector re-checks each lesson's canonical file at read time.
    """
    directory = os.path.join(root, "distribution", "agents")
    try:
        identities = sorted(e.name for e in os.scandir(directory) if e.is_dir())
    except OSError:
        return 0
    repo = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    return sum(bridge_lessons(root, scope={"agent": identity, "repo": repo}) for identity in identities)


def setup(
    root: str,
    *,
    config: dict,
    agents: list[str] | None = None,
    allow_global_agent_config: bool = False,
    import_agent_history: bool = False,
) -> dict:
    started = time.monotonic()
    storage = fact_store.migrate(root) if not fact_store.enabled(root) else {"backend": "sqlite-wal"}
    repo = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    configured_agents = config.get("agents", {})
    detection = detect_installed_agents(root)
    if agents is not None:
        selected = list(agents)
        selection_source = "explicit"
    elif configured_agents:
        # A named entry in the project's UniMem config is an explicit
        # selection, so preserve the existing configuration-driven behavior.
        selected = list(configured_agents)
        selection_source = "configuration"
    else:
        selected = list(detection["project_scoped"])
        if allow_global_agent_config:
            selected.extend(detection["requires_global_opt_in"])
        selection_source = "auto-detected"
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
        agent_config = configured_agents.get(agent, {})
        from unimem.native_hooks import PROJECT_CONFIGS, install_special

        if agent == "kimi":
            from unimem.native_hooks import resolve_kimi_config_target

            target, allow_external = resolve_kimi_config_target(root, agent_config.get("config_path"))
            named_selection = agents is not None and agent in agents or agent in configured_agents
            if allow_external and not (allow_global_agent_config or named_selection):
                raise ValueError(
                    "Kimi hooks target host-global config; select --agent kimi or pass "
                    "--allow-global-agent-config to permit that write"
                )
            special = install_special(
                root,
                agent,
                target,
                manifest,
                allow_external=allow_external and (allow_global_agent_config or named_selection),
            )
        else:
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
    from unimem.history_import import (
        discover_agent_history,
        import_history,
        record_discovery_coverage,
    )

    history_sources = list(config.get("histories", []))
    if not history_sources:
        # Only project exports are discoverable here. Host history locations
        # remain opt-in through explicit `histories` entries.
        for agent in ("claude", "codex", "cursor", "gemini"):
            directory = os.path.join(root, ".unimem", "history", agent)
            if os.path.isdir(directory):
                for name in sorted(os.listdir(directory))[:16]:
                    if name.endswith((".json", ".jsonl", ".ndjson")):
                        history_sources.append(
                            {"agent": agent, "path": os.path.relpath(os.path.join(directory, name), root)}
                        )
    discovery_sources: list[dict] = []
    discovery_reports: dict[str, dict] = {}
    if import_agent_history:
        discovery_sources, discovery_reports = discover_agent_history(root)
        for report in discovery_reports.values():
            report.update(
                imported_files=0,
                imported_messages=0,
                messages_covered=0,
                invalid_files=0,
                already_configured_files=0,
                source_coverage_ids=[],
            )
    configured_paths = {
        (source.get("agent"), os.path.realpath(os.path.join(root, source.get("path", ""))))
        for source in history_sources
        if isinstance(source, dict) and isinstance(source.get("path"), str)
    }
    for source in discovery_sources:
        key = (source["agent"], os.path.realpath(source["path"]))
        if key in configured_paths:
            discovery_reports[source["agent"]]["already_configured_files"] += 1
            continue
        history_sources.append(source)
        configured_paths.add(key)
    for source in history_sources:
        if not isinstance(source, dict) or set(source) not in (
            {"agent", "path"},
            {"agent", "path", "source_locator"},
        ):
            raise ValueError("history entries require agent and path")
        discovered = "source_locator" in source
        identity = source["agent"] + "-" + repo
        try:
            result = import_history(
                root,
                source["path"] if os.path.isabs(source["path"]) else os.path.join(root, source["path"]),
                agent=source["agent"],
                scope={"agent": identity, "repo": repo},
                source_locator=source.get("source_locator"),
                expected_project=root if discovered else None,
            )
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            if not discovered:
                raise
            discovery_reports[source["agent"]]["invalid_files"] += 1
            continue
        imported.append({"agent": source["agent"], "messages": result["messages"]})
        if discovered:
            report = discovery_reports[source["agent"]]
            report["imported_files"] += 1
            report["imported_messages"] += result["messages"]
            report["messages_covered"] += result["messages_covered"]
            report["source_coverage_ids"].append(result["coverage_id"])
    history_discovery = []
    for agent, report in discovery_reports.items():
        identity = agent + "-" + repo
        coverage_id = record_discovery_coverage(
            root, agent=agent, scope={"agent": identity, "repo": repo}, report=report,
        )
        history_discovery.append({**report, "coverage_id": coverage_id})
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
        "agent_detection": {
            **detection,
            "selected": [item["agent"] for item in installed],
            "selection_source": selection_source,
        },
        "history": imported,
        "history_discovery": history_discovery,
        "elapsed_seconds": time.monotonic() - started,
        "repo": repo,
        "fabricated_lessons": 0,
    }
