"""Watch cascade: single-pass mtime scan + lesson-cache reconciliation."""
from __future__ import annotations

import glob
import json
import math
import os
import threading
import time

DEFAULT_STATE_REL = os.path.join("memory", ".cache", "watch_state.json")


def default_state_file(root: str) -> str:
    return os.path.join(os.path.abspath(root), DEFAULT_STATE_REL)


def _watched_files(root: str) -> list[str]:
    root = os.path.abspath(root)
    patterns = [
        os.path.join(root, "memory", "lessons", "*.md"),
        os.path.join(root, "memory", "traces", "*.md"),
        os.path.join(root, "memory", "pages", "*.md"),
        os.path.join(root, "memory", "records", "*.md"),
        os.path.join(root, "memory", "facts", "*.jsonl"),
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
    from commontrace import _jsonl, paths

    path = paths.safe_prepare_output_path(state_file)
    _jsonl.write_json(path, snapshot)


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
    """Commit the observed generation only after a successful cascade.

    The pre-build snapshot is recorded: edits made during the build trigger
    another pass. Failures retain the previous watermark and remain retryable.
    """
    root = os.path.abspath(root)
    try:
        from commontrace import _jsonl

        state_path = os.path.abspath(state_file) if state_file else default_state_file(root)
        with _jsonl.locked(state_path):
            previous, current = load_state(state_path), _snapshot(root)
            changed = sorted(k for k in previous.keys() | current.keys() if previous.get(k) != current.get(k))
            if not changed:
                return {"changed": [], "rebuilt": False,
                        "cache": {"ok": True, "method": "none", "reason": "no changes"}}
            cache = _rebuild_lesson_cache(root)
            if cache.get("ok"):
                from commontrace.wiki import enqueue_changed

                enqueue_changed(root)
                save_state(state_path, current)
            return {"changed": changed, "rebuilt": bool(cache.get("ok")), "cache": cache}
    except Exception as exc:  # noqa: BLE001
        return {"changed": [], "rebuilt": False, "cache": {"ok": False, "error": str(exc)}}


def run_forever(root: str, *, state_file: str | None = None, debounce: float = .5,
                interval: float = .1, stop: threading.Event | None = None, on_result=None) -> None:
    """Portable polling daemon. Quiet-period debounce; synchronous work drains before exit."""
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0
           for v in (debounce, interval)):
        raise ValueError("watch intervals must be finite and positive")
    stop = stop or threading.Event()
    last = _snapshot(root)
    pending = True
    changed_at = time.monotonic()
    while not stop.wait(interval):
        current = _snapshot(root)
        if current != last:
            last, changed_at, pending = current, time.monotonic(), True
        if pending and time.monotonic()-changed_at >= debounce:
            result = reconcile(root, state_file)
            if on_result:
                on_result(result)
            pending = not result["cache"].get("ok", False)
            changed_at = time.monotonic()
