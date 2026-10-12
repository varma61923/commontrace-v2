"""Bounded, evidence-linked cold-start imports. Authorship never implies authority."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import re

from commontrace import memory_guard, paths
from commontrace.memory_journal import Journal, now
from commontrace.memory_record import MemoryRecord

MAX_HISTORY_BYTES = 20 * 1024 * 1024
MAX_DISCOVERY_ENTRIES = 20000
MAX_DISCOVERY_FILES = 128
MAX_DISCOVERY_BYTES = 100 * 1024 * 1024
MAX_DISCOVERY_PROBE_BYTES = 32 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024

_UNSUPPORTED_DISCOVERY = {
    "cursor": (
        "Cursor CLI host history is SQLite-backed (`store.db`) with an unversioned internal schema; "
        "the supplied importer documents no recorded session cwd or verified project-to-store mapping"
    ),
    "gemini": (
        "the supplied competitor sources provide no Gemini CLI cold-history layout or recorded "
        "session cwd; hook transcript parsing does not establish host-history project ownership"
    ),
}


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
    source_paths = re.findall(r"(?<![\w./-])([\w./-]+\.(?:py|ts|js|go|rs|java))\b", quote)
    snippets = [a or b for a, b in re.findall(r"`([^`]+)`|\b(def \w+\(\))", quote)]
    # Backticked file paths are citations, not symbols expected in file contents.
    snippets = [snippet for snippet in snippets if snippet not in source_paths]
    matched = set()
    for relative in source_paths:
        filename = os.path.realpath(os.path.join(root, relative))
        if not paths.is_within_directory(root, filename) or not os.path.isfile(filename):
            continue
        with open(filename, "rb") as handle:
            raw = handle.read(1000001)
        if len(raw) > 1000000:
            continue
        text = raw.decode("utf-8", errors="replace")
        found = {snippet for snippet in snippets if snippet in text}
        matched.update(found)
        if not snippets or found:
            verified.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "method": "path-and-quoted-symbol" if snippets else "path-exists-only",
                }
            )
    return verified if not snippets or matched == set(snippets) else []


def _within_project(cwd: str | None, root: str) -> bool:
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    try:
        return os.path.commonpath((os.path.realpath(root), os.path.realpath(cwd))) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def _session_cwd_from_stream(handle, agent: str, *, max_bytes: int = MAX_HEADER_BYTES) -> tuple[str | None, str, int]:
    """Read only a bounded transcript prefix for repo attribution."""
    maximum_lines = 400 if agent == "claude" else 10
    consumed = 0
    try:
        for _ in range(maximum_lines):
            remaining = max_bytes - consumed
            if remaining <= 0:
                return None, "header-limit", consumed
            line = handle.readline(remaining)
            if not line:
                break
            consumed += len(line)
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if len(line) == remaining and not line.endswith(b"\n"):
                    return None, "header-limit", consumed
                continue
            if not isinstance(row, dict):
                continue
            if agent == "codex" and row.get("type") != "session_meta":
                continue
            cwd = row.get("cwd")
            if not isinstance(cwd, str):
                payload = row.get("payload")
                cwd = payload.get("cwd") if isinstance(payload, dict) else None
            if isinstance(cwd, str) and cwd:
                return cwd, "ok", consumed
            if len(line) == remaining and not line.endswith(b"\n"):
                return None, "header-limit", consumed
    except OSError:
        return None, "unreadable", consumed
    return None, "unattributed", consumed


def _session_cwd(
    filename: str, agent: str, *, max_bytes: int = MAX_HEADER_BYTES
) -> tuple[str | None, str, int]:
    try:
        with open(filename, "rb") as handle:
            return _session_cwd_from_stream(handle, agent, max_bytes=max_bytes)
    except OSError:
        return None, "unreadable", 0


def discover_agent_history(
    root: str,
    *,
    home: str | None = None,
    max_entries: int = MAX_DISCOVERY_ENTRIES,
    max_files: int = MAX_DISCOVERY_FILES,
) -> tuple[list[dict], dict[str, dict]]:
    """Find explicitly requested host history attributable to this project.

    Claude and Codex have bounded file layouts whose recorded session cwd is
    authoritative for project assignment. Cursor and Gemini currently receive
    explicit unsupported coverage records because the available local format
    evidence cannot establish that ownership. The walk never follows symlinks
    and is bounded by directory entries, header probes and imported files.
    """
    if max_entries < 1 or max_files < 1:
        raise ValueError("history discovery limits must be positive")
    project = os.path.realpath(root)
    selected_home = os.path.realpath(os.path.expanduser("~") if home is None else home)
    encoded_project = re.sub(r"[^a-zA-Z0-9]", "-", project)
    sources: list[dict] = []
    reports: dict[str, dict] = {}

    def initial_report(agent: str) -> dict:
        return {
            "agent": agent,
            "entries_examined": 0,
            "candidate_files": 0,
            "attributed_files": 0,
            "selected_files": 0,
            "unattributed_files": 0,
            "out_of_scope_files": 0,
            "unreadable_files": 0,
            "header_limited_files": 0,
            "oversized_files": 0,
            "truncated": False,
            "max_entries": max_entries,
            "max_files": max_files,
            "max_bytes": MAX_DISCOVERY_BYTES,
            "selected_bytes": 0,
            "total_bytes_limit_reached": False,
            "max_probe_bytes": MAX_DISCOVERY_PROBE_BYTES,
            "probe_bytes": 0,
            "probe_limit_reached": False,
            "candidate_files_unprobed": 0,
        }

    for agent in ("claude", "codex", "cursor", "gemini"):
        report = initial_report(agent)
        reports[agent] = report
        if agent in _UNSUPPORTED_DISCOVERY:
            # Do not enumerate a host-private store until its layout can prove
            # project attribution. `None` means the candidate count is unknown,
            # not that a completed scan found zero files.
            report.update(
                candidate_files=None,
                candidate_count_known=False,
                scan_performed=False,
                blocker=_UNSUPPORTED_DISCOVERY[agent],
            )
            continue
        report["candidate_count_known"] = True
        report["scan_performed"] = True
        if agent == "claude":
            host_root = os.path.join(selected_home, ".claude", "projects")
            candidate_paths: list[str] = []
            try:
                with os.scandir(host_root) as entries:
                    for entry in itertools.islice(entries, max_entries + 1):
                        if report["entries_examined"] >= max_entries:
                            report["truncated"] = True
                            break
                        report["entries_examined"] += 1
                        if (
                            (entry.name == encoded_project or entry.name.startswith(encoded_project + "-"))
                            and entry.is_dir(follow_symlinks=False)
                        ):
                            try:
                                with os.scandir(entry.path) as files:
                                    for file_entry in files:
                                        report["entries_examined"] += 1
                                        if report["entries_examined"] > max_entries:
                                            report["entries_examined"] = max_entries
                                            report["truncated"] = True
                                            break
                                        if file_entry.name.endswith(".jsonl") and file_entry.is_file(
                                            follow_symlinks=False
                                        ):
                                            candidate_paths.append(file_entry.path)
                            except OSError:
                                report["unreadable_files"] += 1
                        if report["truncated"]:
                            break
            except FileNotFoundError:
                pass
            except OSError:
                report["unreadable_files"] += 1
        else:
            host_root = os.path.join(selected_home, ".codex", "sessions")
            candidate_paths = []
            pending = [host_root]
            while pending and not report["truncated"]:
                directory = pending.pop()
                try:
                    with os.scandir(directory) as entries:
                        remaining = max_entries - report["entries_examined"]
                        batch = list(itertools.islice(entries, remaining + 1))
                except FileNotFoundError:
                    continue
                except OSError:
                    report["unreadable_files"] += 1
                    continue
                if len(batch) > remaining:
                    batch = batch[:remaining]
                    report["truncated"] = True
                for entry in sorted(batch, key=lambda item: item.name):
                    if report["entries_examined"] >= max_entries:
                        report["truncated"] = True
                        break
                    report["entries_examined"] += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                            candidate_paths.append(entry.path)
                    except OSError:
                        report["unreadable_files"] += 1
        candidate_paths.sort()
        selected: list[str] = []
        selected_bytes = 0
        for candidate_index, filename in enumerate(candidate_paths):
            remaining_probe = MAX_DISCOVERY_PROBE_BYTES - report["probe_bytes"]
            if remaining_probe <= 0:
                report["truncated"] = True
                report["probe_limit_reached"] = True
                report["candidate_files_unprobed"] = len(candidate_paths) - candidate_index
                break
            report["candidate_files"] += 1
            cwd, state, probe_bytes = _session_cwd(
                filename, agent, max_bytes=min(MAX_HEADER_BYTES, remaining_probe)
            )
            report["probe_bytes"] += probe_bytes
            if state == "unreadable":
                report["unreadable_files"] += 1
            elif state == "header-limit":
                report["header_limited_files"] += 1
            elif not cwd:
                report["unattributed_files"] += 1
            elif not _within_project(cwd, project):
                report["out_of_scope_files"] += 1
            else:
                report["attributed_files"] += 1
                try:
                    file_size = os.path.getsize(filename)
                    if file_size > MAX_HISTORY_BYTES:
                        report["oversized_files"] += 1
                        file_size = 0
                    else:
                        if len(selected) >= max_files:
                            report["truncated"] = True
                            continue
                        if selected_bytes + file_size > MAX_DISCOVERY_BYTES:
                            report["truncated"] = True
                            report["total_bytes_limit_reached"] = True
                            continue
                        selected.append(filename)
                        selected_bytes += file_size
                except OSError:
                    report["unreadable_files"] += 1
            if report["probe_bytes"] >= MAX_DISCOVERY_PROBE_BYTES and candidate_index + 1 < len(candidate_paths):
                report["truncated"] = True
                report["probe_limit_reached"] = True
                report["candidate_files_unprobed"] = len(candidate_paths) - candidate_index - 1
                break
        report["selected_bytes"] = selected_bytes
        report["selected_files"] = len(selected)
        for filename in selected:
            sources.append(
                {
                    "agent": agent,
                    "path": filename,
                    "source_locator": os.path.relpath(filename, selected_home).replace(os.sep, "/"),
                }
            )
    return sources, reports


def record_discovery_coverage(root: str, *, agent: str, scope: dict, report: dict) -> str:
    if agent not in ("claude", "codex", "cursor", "gemini"):
        raise ValueError("unsupported host-history discovery source")
    fingerprint = hashlib.sha256(
        (agent + ":" + os.path.realpath(root) + ":host-history-discovery").encode()
    ).hexdigest()
    identity = "history-scan-coverage-" + fingerprint
    journal = Journal(root)
    previous = journal.history(identity)
    status = (
        "unsupported-project-attribution"
        if report.get("blocker")
        else "partial-limit-reached"
        if report["truncated"]
        else "scan-complete-with-skips"
        if any(
            report.get(key, 0)
            for key in (
                "unattributed_files",
                "unreadable_files",
                "header_limited_files",
                "oversized_files",
                "invalid_files",
            )
        )
        else "bounded-scan-complete"
    )
    report["status"] = status
    journal.put(
        MemoryRecord(
            identity,
            "episode",
            len(previous) + 1,
            {
                "subtype": "coverage-ledger",
                "text": "Bounded host history discovery coverage",
                "agent": agent,
                "status": status,
                "blocker": report.get("blocker"),
                "scan_performed": report.get("scan_performed", True),
                "candidate_count_known": report.get("candidate_count_known", True),
                "limits": {
                    "entries": report["max_entries"],
                    "files": report["max_files"],
                    "bytes": report["max_bytes"],
                    "probe_bytes": report["max_probe_bytes"],
                },
                "counts": {
                    key: value
                    for key, value in report.items()
                    if key not in (
                        "agent",
                        "max_entries",
                        "max_files",
                        "max_bytes",
                        "status",
                        "blocker",
                        "scan_performed",
                        "candidate_count_known",
                    )
                },
                "source_coverage_ids": list(report.get("source_coverage_ids", [])),
            },
            scope=scope,
            created_at=now(),
            provenance={"source_kind": "explicit_host_history_scan", "source_agent": agent},
        )
    )
    return identity


def import_history(
    root: str,
    filename: str,
    *,
    agent: str,
    scope: dict,
    source_locator: str | None = None,
    expected_project: str | None = None,
) -> dict:
    if agent not in ("claude", "codex", "cursor", "gemini"):
        raise ValueError("unsupported history format")
    with open(filename, "rb") as handle:
        raw = handle.read(MAX_HISTORY_BYTES + 1)
    if len(raw) > MAX_HISTORY_BYTES:
        raise ValueError("history exceeds 20 MiB; split the export")
    if expected_project is not None:
        cwd, state, _ = _session_cwd_from_stream(io.BytesIO(raw), agent)
        if state != "ok" or not _within_project(cwd, expected_project):
            raise ValueError("host history changed or does not identify this project")
    text = raw.decode("utf-8")
    if filename.endswith((".jsonl", ".ndjson")):
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
    source = hashlib.sha256((os.path.realpath(filename) + json.dumps(scope, sort_keys=True)).encode()).hexdigest()
    journal = Journal(root)
    claims, imported, seeded = [], 0, []
    for index, row in enumerate(messages(data)):
        row, _ = memory_guard.sanitize_metadata(row, pii=True)
        identity = hashlib.sha256(
            (source + ":" + str(row["external_id"] or index) + ":" + row["quote"]).encode()
        ).hexdigest()
        verification = verify_code(root, row["quote"])
        claim = {
            **row,
            "citation": {
                "source_digest": source,
                "message_index": index,
                **({"source_locator": source_locator} if source_locator else {}),
            },
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
        # A code-grounded instruction is a review candidate, not proven causal utility.
        actionable = re.search(r"\b(always|avoid|prefer|must|should|never|use|run|test|ensure)\b", row["quote"], re.I)
        authored_text = row["author"] in ("user", "assistant")
        if authored_text and claim["code_verified"] and actionable and not memory_guard.scan_injection(row["quote"]):
            lesson_id = "import-lesson-" + identity
            if not journal.history(lesson_id):
                journal.put(
                    MemoryRecord(
                        lesson_id,
                        "lesson",
                        1,
                        {
                            "text": row["quote"],
                            "lesson_kind": "procedure",
                            "operation": "create",
                            "trigger": {},
                            "source_message_ids": [identity],
                            "offline_prior": {"alpha": 1, "beta": 1, "validated": False},
                            "imported_claim": True,
                        },
                        status="quarantined",
                        scope=scope,
                        created_at=now(),
                        relations={"derives": [identity]},
                        provenance={
                            "source_versions": {identity: journal.history(identity)[-1].version},
                            "authored_by": row["author"],
                            "source_agent": agent,
                            "code_evidence": verification,
                            "source_digest": source,
                        },
                    )
                )
                seeded.append(lesson_id)
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
                **({"source_locator": source_locator} if source_locator else {}),
            },
            scope=scope,
            created_at=now(),
        )
    )
    return {
        "messages": imported,
        "claims": claims,
        "coverage_id": coverage_id,
        "source_digest": source,
        "messages_covered": len(claims),
        "bytes": len(raw),
        "lessons_seeded": len(seeded),
        "lesson_ids": seeded,
    }
