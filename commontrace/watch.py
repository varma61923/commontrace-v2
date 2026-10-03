"""Watch cascade: single-pass mtime scan + lesson-cache reconciliation."""
from __future__ import annotations

import glob
import json
import os

DEFAULT_STATE_REL = os.path.join("memory", ".cache", "watch_state.json")


def default_state_file(root: str) -> str:
    return os.path.join(os.path.abspath(root), DEFAULT_STATE_REL)


def _watched_files(root: str) -> list[str]:
    root = os.path.abspath(root)
    patterns = [
        os.path.join(root, "memory", "lessons", "*.md"),
        os.path.join(root, "memory", "graph", "*.jsonl"),
    ]
    out: list[str] = []
    for pat in patterns:
        try:
            out.extend(sorted(glob.glob(pat)))
        except OSError:
            continue
    filtered = []
    for p in out:
        base = os.path.basename(p)
        if base.startswith(".") or base.endswith(".tmp"):
            continue
        if base == "lesson_template.md":
            continue
        filtered.append(p)
    return sorted(filtered)


def _snapshot(root: str) -> dict[str, list[int]]:
    root = os.path.abspath(root)
    snap: dict[str, list[int]] = {}
    for path in _watched_files(root):
        try:
            st = os.stat(path)
        except OSError:
            continue
        rel = os.path.relpath(path, root)
        snap[rel] = [st.st_mtime_ns, st.st_size]
    return snap


def load_state(state_file: str) -> dict:
    try:
        with open(state_file, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def save_state(state_file: str, snapshot: dict) -> None:
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        tmp = state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(snapshot, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, state_file)
    except OSError:
        pass


def scan(root: str, state_file: str | None = None) -> list[str]:
    """Diff live mtimes against *state_file*; persist the new snapshot."""
    root = os.path.abspath(root)
    state_path = os.path.abspath(state_file) if state_file else default_state_file(root)
    previous = load_state(state_path)
    current = _snapshot(root)
    changed: list[str] = []
    for rel, stamp in current.items():
        if previous.get(rel) != stamp:
            changed.append(rel)
    for rel in previous:
        if rel not in current:
            changed.append(rel)
    changed = sorted(changed)
    save_state(state_path, current)
    return changed


def _rebuild_lesson_cache(root: str) -> dict:
    try:
        from commontrace import lesson_cache as lc
    except Exception as exc:  # noqa: BLE001 - watch must survive broken imports
        return {"ok": False, "method": "none", "error": str(exc)}
    try:
        rebuild = getattr(lc, "rebuild", None)
        if callable(rebuild):
            rebuild(root)
            return {"ok": True, "method": "rebuild"}
        lessons = lc.load_projected(root)
        return {"ok": True, "method": "load_projected", "lessons": len(lessons)}
    except Exception as exc:  # noqa: BLE001
        try:
            ldir = os.path.join(root, "memory", "lessons")
            names = os.listdir(ldir) if os.path.isdir(ldir) else []
            return {"ok": False, "method": "rescan",
                    "error": str(exc), "files": len(names)}
        except Exception as exc2:  # noqa: BLE001
            return {"ok": False, "method": "rescan", "error": str(exc2)}


def reconcile(root: str, state_file: str | None = None) -> dict:
    """Single pass: ``scan`` + conditional cache rebuild."""
    root = os.path.abspath(root)
    try:
        changed = scan(root, state_file)
    except Exception as exc:  # noqa: BLE001
        return {"changed": [], "rebuilt": False, "cache": {"ok": False, "error": str(exc)}}
    if not changed:
        return {"changed": [], "rebuilt": False,
                "cache": {"ok": True, "method": "none", "reason": "no changes"}}
    cache = _rebuild_lesson_cache(root)
    return {"changed": changed, "rebuilt": bool(cache.get("ok")), "cache": cache}
