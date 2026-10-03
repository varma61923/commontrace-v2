"""Consolidation daemon: cron/idle/event triggers, file lock, crash recovery."""
from __future__ import annotations

import json
import os
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - platform fallback
    fcntl = None  # type: ignore[assignment]

LOCK_REL = os.path.join("memory", ".cache", "daemon.lock")
MARKER_REL = os.path.join("memory", ".cache", "daemon.crash")
STATE_REL = os.path.join("memory", ".cache", "daemon_state.json")


def lock_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), LOCK_REL)


def marker_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), MARKER_REL)


def state_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), STATE_REL)


class _Lock:
    def __init__(self, path: str, fh=None, excl_created: bool = False):
        self.path = path
        self.fh = fh
        self.excl_created = excl_created
        self.held = True

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        try:
            if self.fh is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                except (OSError, ValueError):
                    pass
                try:
                    self.fh.close()
                except (OSError, ValueError):
                    pass
            if self.excl_created:
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
        finally:
            self.fh = None

    def __enter__(self) -> "_Lock":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def acquire_lock(root: str) -> _Lock | None:
    """Non-blocking acquire; returns None when another pass holds the lock."""
    path = lock_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return None
    if fcntl is not None:
        try:
            fh = open(path, "a+b")
        except OSError:
            return None
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            try:
                fh.close()
            except OSError:
                pass
            return None
        return _Lock(path, fh=fh)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    except OSError:
        return None
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return _Lock(path, excl_created=True)


def _as_trigger(trigger) -> dict:
    if trigger is None:
        return {"type": "once"}
    if isinstance(trigger, str):
        return {"type": trigger}
    if isinstance(trigger, dict):
        out = dict(trigger)
        out.setdefault("type", "once")
        return out
    return {"type": str(trigger)}


def should_run(trigger, state: dict | None = None) -> bool:
    """Whether a pass should fire for *trigger* given daemon *state*."""
    trig = _as_trigger(trigger)
    kind = str(trig.get("type", "once")).lower()
    state = state or {}
    now = time.time()
    if kind in ("once", "manual", "cron", "schedule"):
        min_interval = trig.get("min_interval_s", state.get("min_interval_s", 0))
        try:
            min_interval = float(min_interval or 0)
        except (TypeError, ValueError):
            min_interval = 0
        if min_interval > 0:
            try:
                last = float(state.get("last_run") or 0)
            except (TypeError, ValueError):
                last = 0
            if last and (now - last) < min_interval:
                return False
        return True
    if kind == "event":
        pending = state.get("pending_changes", trig.get("pending_changes", 0))
        if isinstance(pending, bool):
            return pending
        try:
            return int(pending or 0) > 0
        except (TypeError, ValueError):
            return True
    if kind == "idle":
        try:
            idle_s = float(trig.get("idle_s", state.get("idle_s", 0)) or 0)
        except (TypeError, ValueError):
            idle_s = 0
        try:
            threshold = float(trig.get("threshold_s", 0) or 0)
        except (TypeError, ValueError):
            threshold = 0
        if threshold > 0:
            return idle_s >= threshold
        return True
    return True


def _load_daemon_state(root: str) -> dict:
    try:
        with open(state_path(root), encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_daemon_state(root: str, state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(state_path(root)), exist_ok=True)
        tmp = state_path(root) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, state_path(root))
    except OSError:
        pass


def _invoke_hooks(root: str) -> dict:
    results: dict = {}
    try:
        from commontrace.commands import dream_cmd
        main_dream = getattr(dream_cmd, "main_dream", None)
        if callable(main_dream):
            results["dream"] = main_dream(root, draft=False)
        else:
            results["dream"] = "skipped: main_dream missing"
    except Exception as exc:  # noqa: BLE001 - one hook must not kill the pass
        results["dream"] = f"error: {exc}"
    try:
        from commontrace import consolidate as consolidate_mod
        from commontrace import evidence_io
        lessons = evidence_io.load_active_lessons(root)
        report = consolidate_mod.build_report(lessons)
        results["consolidate"] = {
            "n_active": report.n_active,
            "fuse": len(report.fuse),
            "contradict": len(report.contradict),
            "archive": len(report.archive),
        }
    except Exception as exc:  # noqa: BLE001
        results["consolidate"] = f"skipped: {exc}"
    try:
        from commontrace import jobs

        if os.path.exists(jobs.db_path(root)):
            budget = float(os.environ.get("COMMONTRACE_JOBS_SECONDS", "300") or 300)
            results["jobs"] = jobs.run_pending(root, limit=100, time_budget=budget)
    except Exception as exc:  # noqa: BLE001
        results["jobs"] = f"error: {exc}"
    return results


def run_once(root: str, triggers=None) -> dict:
    """One guarded consolidation pass. Never raises; returns a status dict."""
    root = os.path.abspath(root)
    if triggers is None:
        trigger_list = [{"type": "once"}]
    elif isinstance(triggers, list):
        trigger_list = triggers
    else:
        trigger_list = [triggers]
    state = _load_daemon_state(root)
    if not any(should_run(t, state) for t in trigger_list):
        return {"ok": True, "ran": False, "skipped": True,
                "reason": "no trigger fired", "root": root}
    lock = acquire_lock(root)
    if lock is None:
        return {"ok": False, "ran": False, "reason": "locked",
                "error": "another daemon pass holds the lock", "root": root}
    try:
        mpath = marker_path(root)
        recovered = False
        try:
            if os.path.exists(mpath):
                recovered = True
            os.makedirs(os.path.dirname(mpath), exist_ok=True)
            with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"pid": os.getpid(), "started": time.time()}, fh)
        except OSError:
            pass
        hooks = _invoke_hooks(root)
        now = time.time()
        state.update({"last_run": now, "last_hooks": {
            k: (v if isinstance(v, (int, float, str, bool)) else "ok")
            for k, v in hooks.items()
        }})
        _save_daemon_state(root, state)
        try:
            os.unlink(mpath)
        except OSError:
            pass
        return {"ok": True, "ran": True, "recovered": recovered,
                "hooks": hooks, "last_run": now, "root": root}
    finally:
        lock.release()
