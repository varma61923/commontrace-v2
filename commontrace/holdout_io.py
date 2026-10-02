"""Randomized-holdout arm assignment and its append-only log."""
from __future__ import annotations

import datetime
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass, field

from commontrace import experiment, frontmatter, lesson_io, paths

DEFAULT_SALT = "default"


CONFIG_NAME = "experiment.json"


def config_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONFIG_NAME)


@dataclass(frozen=True)
class ExperimentConfig:
    """One store's experiment settings, read by EVERY retriever."""

    rate: float = experiment.DEFAULT_HOLDOUT_RATE
    salt: str = DEFAULT_SALT
    detect: float = experiment.DEFAULT_PRACTICAL_EFFECT
    started_at: str = ""
    note: str = ""

    @property
    def running(self) -> bool:
        return self.rate > 0.0


def load_config(root: str) -> ExperimentConfig:
    """The store's experiment settings, or the defaults if none were set."""
    path = config_path(root)
    if not os.path.isfile(path):
        return ExperimentConfig()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return ExperimentConfig()
        return ExperimentConfig(
            rate=_float_or(raw.get("rate"), experiment.DEFAULT_HOLDOUT_RATE),
            salt=str(raw.get("salt") or DEFAULT_SALT),
            detect=_float_or(raw.get("detect"), experiment.DEFAULT_PRACTICAL_EFFECT),
            started_at=str(raw.get("started_at") or ""),
            note=str(raw.get("note") or ""),
        )
    except (OSError, ValueError):
        return ExperimentConfig()


def configure(
    root: str,
    *,
    rate: float,
    detect: float = experiment.DEFAULT_PRACTICAL_EFFECT,
    note: str = "",
    salt: str | None = None,
) -> ExperimentConfig:
    """Start (or restart) this store's experiment. Returns the new settings."""
    if not 0.0 <= rate < 1.0:
        raise ValueError(f"holdout rate must be in [0.0, 1.0), got {rate}")
    if not 0.0 < detect < 1.0:
        raise ValueError(f"detectable effect must be in (0.0, 1.0), got {detect}")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    config = ExperimentConfig(
        rate=rate,
        salt=salt or f"{now[:19].replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}-{rate:g}",
        detect=detect,
        started_at=now,
        note=note,
    )
    path = config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"rate": config.rate, "salt": config.salt, "detect": config.detect,
                 "started_at": config.started_at, "note": config.note},
                fh, indent=2, sort_keys=True,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    return config


def holdout_log_path(root: str) -> str:
    """Append-only record of every holdout assignment a retriever made."""
    return os.path.join(paths.memory_dir(root), "holdout_log.jsonl")


def _append_lines(path: str, lines: list[str], durable: bool = True) -> None:
    needs_newline = False
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            needs_newline = fh.read(1) != b"\n"
    with open(path, "a", encoding="utf-8") as fh:
        if needs_newline:
            fh.write("\n")
        for line in lines:
            fh.write(line + "\n")
        fh.flush()
        if durable:
            os.fsync(fh.fileno())


def assign_and_log(
    root: str,
    slugs: list[str],
    *,
    occasion_id: str,
    rate: float,
    salt: str,
    relevance: dict[str, float] | None = None,
    scorer: str = "",
    floor: float | None = None,
    revisions: dict[str, str | None] | None = None,
    durable: bool = True,
) -> set[str]:
    """Decide which of `slugs` to withhold on this occasion, and record it."""
    withheld = {s for s in slugs if experiment.is_held_out(s, occasion_id, rate, salt)}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    path = holdout_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines: list[str] = []
    with frontmatter.locked(path):
        for rank, slug in enumerate(slugs, start=1):
            row = {
                "occasion_id": occasion_id,
                "lesson": slug,
                "injected": slug not in withheld,
                "rate": rate,
                "salt": salt,
                "at": now,
                "rank": rank,
                "revision": (
                    revisions[slug]
                    if revisions is not None and slug in revisions
                    else lesson_io.revision_for_slug(root, slug)
                ),
            }
            if relevance is not None and slug in relevance:
                row["relevance"] = round(float(relevance[slug]), 6)
            if scorer:
                row["scorer"] = scorer
            if floor is not None:
                row["floor"] = float(floor)
            lines.append(json.dumps(row))
        _append_lines(path, lines, durable)
    return withheld


def outcomes_log_path(root: str) -> str:
    """Append-only record of task outcomes reported by occasion id."""
    return os.path.join(paths.memory_dir(root), "occasion_outcomes.jsonl")


class ConflictingOutcome(ValueError):
    """An occasion's outcome was reported twice with different answers."""


def _parse_outcome_line(line: bytes | str, out: dict[str, bool]) -> None:
    try:
        row = json.loads(line)
    except ValueError:
        return
    if not isinstance(row, dict):
        return
    occasion, succeeded = row.get("occasion_id"), row.get("succeeded")
    if isinstance(occasion, str) and occasion and isinstance(succeeded, bool):
        out[occasion] = succeeded


_TAIL_CHECK = 64


@dataclass
class _OutcomeCache:
    identity: tuple
    offset: int = 0
    tail: bytes = b""
    outcomes: dict = field(default_factory=dict)


_outcome_cache: dict[str, _OutcomeCache] = {}
_outcome_cache_lock = threading.Lock()


def _outcomes_shared(path: str) -> dict[str, bool]:
    try:
        st = os.stat(path)
    except OSError:
        return {}
    identity = (st.st_dev, st.st_ino)
    with _outcome_cache_lock:
        cache = _outcome_cache.get(path)
        with open(path, "rb") as fh:
            valid = cache is not None and cache.identity == identity and st.st_size >= cache.offset
            if valid and cache.tail:
                fh.seek(cache.offset - len(cache.tail))
                valid = fh.read(len(cache.tail)) == cache.tail
            if not valid:
                cache = _OutcomeCache(identity=identity)
                _outcome_cache[path] = cache
            fh.seek(cache.offset)
            chunk = fh.read()
        whole = chunk.rfind(b"\n") + 1
        for line in chunk[:whole].splitlines():
            _parse_outcome_line(line, cache.outcomes)
        if whole:
            cache.offset += whole
            cache.tail = (cache.tail + chunk[:whole])[-_TAIL_CHECK:]
        pending = chunk[whole:]
        if not pending.strip():
            return cache.outcomes
        merged = dict(cache.outcomes)
        _parse_outcome_line(pending, merged)
        return merged


def read_outcomes(root: str) -> dict[str, bool]:
    return dict(_outcomes_shared(outcomes_log_path(root)))


def record_outcome(root: str, occasion_id: str, succeeded: bool, durable: bool = True) -> bool:
    """Record whether the task on `occasion_id` succeeded."""
    if not isinstance(occasion_id, str) or not occasion_id.strip():
        raise ValueError("occasion_id must be a non-empty string")
    if not isinstance(succeeded, bool):
        raise TypeError(f"succeeded must be a bool, got {type(succeeded).__name__}")
    path = outcomes_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        existing = _outcomes_shared(path).get(occasion_id)
        if existing is not None:
            if existing == succeeded:
                return False
            raise ConflictingOutcome(
                f"occasion {occasion_id!r} is already recorded as "
                f"{'succeeded' if existing else 'failed'}; refusing to change it"
            )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _append_lines(
            path, [json.dumps({"occasion_id": occasion_id, "succeeded": succeeded, "at": now})],
            durable,
        )
    return True


@dataclass(frozen=True)
class LogRecord:
    lesson: str
    occasion_id: str
    injected: bool
    rate: float
    salt: str
    at: datetime.datetime | None
    revision: str | None = None
    relevance: float | None = None
    rank: int | None = None
    scorer: str | None = None
    floor: float | None = None


def _parse_log_line(line: str) -> LogRecord | None:
    line = line.strip()
    try:
        raw = json.loads(line)
        lesson = str(raw["lesson"])
        occasion_id = str(raw["occasion_id"])
        injected = bool(raw["injected"])
    except (ValueError, KeyError, TypeError):
        return None
    at = None
    if raw.get("at"):
        try:
            at = datetime.datetime.fromisoformat(str(raw["at"]))
        except ValueError:
            at = None
    return LogRecord(
        lesson=lesson,
        occasion_id=occasion_id,
        injected=injected,
        rate=_float_or(raw.get("rate"), experiment.DEFAULT_HOLDOUT_RATE),
        salt=str(raw.get("salt") or DEFAULT_SALT),
        at=at,
        revision=(str(raw["revision"]) if raw.get("revision") else None),
        relevance=_opt_float(raw.get("relevance")),
        rank=_opt_int(raw.get("rank")),
        scorer=(str(raw["scorer"]) if raw.get("scorer") else None),
        floor=_opt_float(raw.get("floor")),
    )


@dataclass
class _LogCache:
    identity: tuple
    offset: int = 0
    tail: bytes = b""
    records: list = field(default_factory=list)
    corrupt: int = 0


_log_cache: dict[str, _LogCache] = {}
_log_cache_lock = threading.Lock()


def read_log(root: str) -> tuple[list[LogRecord], int]:
    """Every assignment ever logged, plus a count of unparseable lines."""
    path = holdout_log_path(root)
    try:
        st = os.stat(path)
    except OSError:
        return [], 0
    identity = (st.st_dev, st.st_ino)
    with _log_cache_lock:
        cache = _log_cache.get(path)
        with open(path, "rb") as fh:
            valid = cache is not None and cache.identity == identity and st.st_size >= cache.offset
            if valid and cache.tail:
                fh.seek(cache.offset - len(cache.tail))
                valid = fh.read(len(cache.tail)) == cache.tail
            if not valid:
                cache = _LogCache(identity=identity)
                _log_cache[path] = cache
            fh.seek(cache.offset)
            chunk = fh.read()
        whole = chunk.rfind(b"\n") + 1
        for raw_line in chunk[:whole].decode("utf-8", errors="replace").split("\n"):
            if not raw_line.strip():
                continue
            record = _parse_log_line(raw_line)
            if record is None:
                cache.corrupt += 1
            else:
                cache.records.append(record)
        if whole:
            cache.offset += whole
            cache.tail = (cache.tail + chunk[:whole])[-_TAIL_CHECK:]
        records, corrupt = list(cache.records), cache.corrupt
    pending = chunk[whole:].decode("utf-8", errors="replace")
    if pending.strip():
        record = _parse_log_line(pending)
        if record is None:
            corrupt += 1
        else:
            records.append(record)
    return records, corrupt


def injected_slugs_for_occasion(root: str, occasion_id: str) -> set[str]:
    if not occasion_id:
        return set()
    records, _corrupt = read_log(root)
    return {r.lesson for r in records if r.occasion_id == occasion_id and r.injected}


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def _float_or(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return out
