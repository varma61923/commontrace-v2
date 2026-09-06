"""One store's retrieval settings, read by EVERY retriever.

Modeled deliberately on commontrace/holdout_io.py's ExperimentConfig, for the
same reason it exists: a setting that lives as a CLI flag default on `query`
and a separate constant in the MCP server is a setting the two surfaces can
silently disagree about. Retrieval settings are worse in that respect than the
holdout rate, because they decide which lessons are ELIGIBLE at all -- so a
disagreement between surfaces doesn't just change what an agent sees, it
changes the denominator of the causal experiment measuring whether any of it
helped (commontrace/integrity.py's check_scorer_drift).

THE BACKWARD-COMPATIBILITY RULE HERE IS LOAD-BEARING. A store that already has
holdout assignments on disk was scored by the historical raw-additive scorer
with no floor. Switching it to IDF scoring mid-flight would change which
lessons clear the bar, which changes eligibility, which makes the assignments
before and after the upgrade two different experiments -- pooled into one
comparison, silently, by the act of upgrading. holdout_io.read_log's own
comment on the `salt` field states the principle:

    "Backward compatibility for a measurement is not a nicety: the
     alternative is a fleet's entire experiment history becoming unreadable
     on upgrade."

So `load_config` pins such a store to the historical scorer and prints how to
opt in, rather than upgrading it silently. A store with no experiment running
has nothing to invalidate and gets the better scorer immediately.
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass

from commontrace import paths, retrieval

CONFIG_NAME = "retrieval.json"


def config_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONFIG_NAME)


@dataclass(frozen=True)
class RetrievalConfig:
    scorer: str = retrieval.SCORER_IDF
    floor: float = retrieval.DEFAULT_FLOOR
    configured_at: str = ""
    note: str = ""
    # True when these settings were inferred for an existing store rather than
    # chosen by anyone, so callers can say so once instead of pretending the
    # store opted into them.
    pinned_for_running_experiment: bool = False


def _float_or(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out


def has_recorded_assignments(root: str) -> bool:
    """Whether this store has already logged holdout assignments.

    Checked by size rather than by parsing: this runs on every retrieval, and
    the question is only "is there history here to protect".
    """
    from commontrace import holdout_io

    path = holdout_io.holdout_log_path(root)
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _last_logged_settings(root: str) -> tuple[str, float] | None:
    """The scorer and floor the most recent assignment was made under.

    None when there is no log, or when the last line predates these fields --
    which is what identifies a genuine pre-upgrade store.

    Reads only the tail of the file. This runs on every retrieval, and a
    fleet's log grows without bound, so parsing all of it here would make
    retrieval get slower the longer the pilot runs.
    """
    from commontrace import holdout_io

    path = holdout_io.holdout_log_path(root)
    try:
        size = os.path.getsize(path)
        if size == 0:
            return None
        with open(path, "rb") as fh:
            # A logged row is a few hundred bytes; 8 KiB covers the last one
            # comfortably without reading a large log into memory.
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed([ln for ln in tail.splitlines() if ln.strip()]):
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # a torn final line, or a partial first line from the seek
        scorer = raw.get("scorer")
        floor = raw.get("floor")
        if scorer and floor is not None:
            try:
                return str(scorer), float(floor)
            except (TypeError, ValueError):
                return None
        return None  # a complete row that simply predates these fields
    return None


def load_config(root: str) -> RetrievalConfig:
    """This store's retrieval settings, or the right defaults if unset.

    Never raises. A malformed config falls back to defaults rather than
    failing the retrieval that asked for it -- refusing to serve a lesson
    because a settings file is corrupt trades a working fleet for a tidy
    error (holdout_io.load_config takes the same position).
    """
    path = config_path(root)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                scorer = str(raw.get("scorer") or retrieval.SCORER_IDF)
                if scorer not in (retrieval.SCORER_IDF, retrieval.SCORER_COUNT):
                    scorer = retrieval.SCORER_IDF
                return RetrievalConfig(
                    scorer=scorer,
                    floor=_float_or(raw.get("floor"), retrieval.DEFAULT_FLOOR),
                    configured_at=str(raw.get("configured_at") or ""),
                    note=str(raw.get("note") or ""),
                )
        except (OSError, ValueError):
            pass

    # Unconfigured. A store with assignments already on disk keeps the scorer
    # those assignments were made under; see this module's docstring.
    #
    # WHICH scorer that is has to come from the log, not from the mere
    # EXISTENCE of a log. Treating "there are assignments" as "this is a
    # pre-upgrade store" was wrong in the case that matters most: a brand-new
    # store writes its first assignment under the current scorer, and from the
    # second query onward the log exists -- so every new fleet was silently
    # downgraded to the historical scorer after one query, and its log then
    # held two scorers, which check_scorer_drift correctly reports as an
    # INVALIDATED experiment. The mechanism meant to protect an upgrade was
    # breaking every fresh pilot instead.
    #
    # New rows record `scorer`/`floor`; pre-upgrade rows do not. That
    # distinction is exactly the question being asked, so ask it directly.
    logged = _last_logged_settings(root)
    if logged is not None:
        scorer, floor = logged
        return RetrievalConfig(
            scorer=scorer,
            floor=floor,
            pinned_for_running_experiment=True,
        )
    if has_recorded_assignments(root):
        return RetrievalConfig(
            scorer=retrieval.SCORER_COUNT,
            floor=0.0,
            pinned_for_running_experiment=True,
        )
    return RetrievalConfig()


def configure(root: str, *, scorer: str | None = None, floor: float | None = None,
              note: str = "") -> RetrievalConfig:
    """Persist this store's retrieval settings. Returns the new settings.

    Callers that change `scorer` or `floor` on a store with a running
    experiment must rotate the holdout salt afterwards
    (holdout_io.configure): both change which lessons are eligible, so the
    assignments before and after describe two different treatments, exactly
    as a changed holdout rate does.
    """
    current = load_config(root)
    new_scorer = current.scorer if scorer is None else scorer
    if new_scorer not in (retrieval.SCORER_IDF, retrieval.SCORER_COUNT):
        raise ValueError(
            f"unknown scorer {new_scorer!r}: expected "
            f"{retrieval.SCORER_IDF!r} or {retrieval.SCORER_COUNT!r}"
        )
    new_floor = current.floor if floor is None else float(floor)
    if not 0.0 <= new_floor <= 1.0:
        raise ValueError(f"relevance floor must be in [0.0, 1.0], got {new_floor}")

    config = RetrievalConfig(
        scorer=new_scorer,
        floor=new_floor,
        configured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        note=note,
    )
    os.makedirs(paths.memory_dir(root), exist_ok=True)
    tmp = config_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "scorer": config.scorer,
                "floor": config.floor,
                "configured_at": config.configured_at,
                "note": config.note,
            },
            fh,
            indent=2,
        )
        fh.write("\n")
    os.replace(tmp, config_path(root))
    return config
