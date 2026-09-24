"""Each lesson's measured causal evidence, for the local retrieval surfaces.

The Hub's search results carry this (hub/crud.py:causal_evidence); a fleet
running on the local store deserved the same answer from `retrieve`, not a
second-class one. So a lesson proven to help, one never measured and one
measured to make outcomes WORSE stop looking identical to the agent
choosing among them.

`analyse` is the one place the local experiment is computed -- load, scope
to the current randomization, audit, estimate -- and `experiment_status`
uses it too, so the verdict on a retrieved lesson is always the verdict the
experiment report gives. The rules match the Hub's: nothing is added until
there is experiment data, and a COMPROMISED audit shows no numbers.

The analysis re-reads the holdout log and every episode and trace file, so
it is cached per store and recomputed when any of its inputs changes (the
outcome log, the experiment settings, a file added to or removed from the
episode and trace directories -- not the holdout log, see _key) and in any
case after TTL_SECONDS, because the audit judges pending occasions by their
age.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from commontrace import experiment, harm, holdout_io, integrity, paths

TTL_SECONDS = 300.0
_cache: dict[str, tuple[tuple, float, dict]] = {}


@dataclass(frozen=True)
class Analysis:
    all_rows: list
    rows: list
    rate: float
    corrupt: int
    wanted_salt: str
    n_other_salt: int
    report: object | None
    effects: list


def analyse(root: str) -> Analysis:
    """The local experiment, computed the one way it is computed."""
    from commontrace.commands import experiment_cmd  # a command module; imported lazily

    all_rows, rate, corrupt = experiment_cmd._load(root)
    if not all_rows:
        return Analysis(all_rows, [], rate, corrupt, "", 0, None, [])
    rows, wanted_salt, n_other_salt = experiment_cmd.scope_to_current_salt(root, all_rows)
    if not rows:
        return Analysis(all_rows, rows, rate, corrupt, wanted_salt, n_other_salt, None, [])
    report = integrity.audit(rows)
    # sequential=True for the reason hub/crud.py:causal_effects gives: these
    # surfaces are looks at a RUNNING experiment, taken on every retrieval
    # and whenever an agent asks, and a fixed threshold tested that often is
    # crossed by luck (measured on this estimator: a false verdict in over a
    # quarter of null runs). A store that withdraws harmful lessons acts on
    # this verdict automatically (commontrace/harm.py), so it has to be one
    # that survives having been watched.
    effects = experiment.analyze(experiment_cmd._observations(rows), sequential=True)
    return Analysis(all_rows, rows, rate, corrupt, wanted_salt, n_other_salt, report, effects)


def _stat(path: str) -> tuple:
    try:
        st = os.stat(path)
    except OSError:
        return (None, None)
    return (st.st_size, st.st_mtime_ns)


def _key(root: str) -> tuple:
    # Not the holdout log: `retrieve` appends to it on every call made with
    # an occasion_id, so keying on it would rebuild the analysis on every
    # retrieval of a store running an experiment. Effects come from resolved
    # occasions only, and those arrive through the outcome log or a new
    # episode or trace file, all of which are watched. Its EXISTENCE is
    # watched, because that decides whether evidence is shown at all.
    return (
        os.path.exists(holdout_io.holdout_log_path(root)),
        _stat(holdout_io.outcomes_log_path(root)),
        _stat(holdout_io.config_path(root)),
        _stat(paths.episodes_dir(root)),
        _stat(paths.traces_dir(root)),
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def for_lessons(root: str) -> dict:
    """{"available", "reason", "measured_at", "by_lesson"} for this store.

    `reason` is "no experiment data" when nothing should be attached at all.
    """
    root = os.path.abspath(root)
    key = _key(root)
    cached = _cache.get(root)
    now = time.monotonic()
    if cached is not None and cached[0] == key and now - cached[1] < TTL_SECONDS:
        return cached[2]

    measured_at = datetime.now(timezone.utc).isoformat()
    analysis = analyse(root)
    if analysis.report is None:
        evidence = {"available": False, "reason": "no experiment data", "measured_at": measured_at, "by_lesson": {}}
    elif not analysis.report.readable:
        evidence = {
            "available": False,
            "reason": (
                "The experiment is COMPROMISED, so no effects are shown. Call "
                "`experiment_status` for what to fix."
            ),
            "measured_at": measured_at,
            "by_lesson": {},
        }
    else:
        evidence = {
            "available": True,
            "reason": "",
            "measured_at": measured_at,
            "by_lesson": {
                e.lesson_slug: {
                    "verdict": e.verdict,
                    "effect": _round(e.effect),
                    "ci_95": [_round(e.ci_low), _round(e.ci_high)],
                    "n_injected": e.n_injected,
                    "n_withheld": e.n_withheld,
                }
                for e in analysis.effects
            },
        }
    _cache[root] = (key, now, evidence)
    return evidence


def attach(root: str, result: dict, *lists: str) -> None:
    """Add evidence to the lesson dicts under each named key of `result`.

    Nothing is added for a store that has never run an experiment: every
    field costs the calling agent tokens on every retrieval.
    """
    evidence = for_lessons(root)
    if not evidence["available"] and evidence["reason"] == "no experiment data":
        return
    result["evidence"] = {k: evidence[k] for k in ("available", "reason", "measured_at")}
    if not evidence["available"]:
        return
    for name in lists:
        for lesson in result.get(name) or []:
            lesson["evidence"] = evidence["by_lesson"].get(lesson.get("slug"), {"verdict": "NOT_MEASURED"})


def withdrawn(root: str, policy: str) -> dict[str, dict]:
    """slug -> evidence, for every lesson this store's harm policy withdraws
    (commontrace/harm.py).

    Empty unless the policy is `withdraw` AND the evidence is readable, so
    the default policy never runs the analysis on its behalf. Never raises:
    this runs on every retrieval, and failing to read the evidence must
    leave retrieval exactly as it was, not stop it.
    """
    if policy != harm.POLICY_WITHDRAW:
        return {}
    try:
        evidence = for_lessons(root)
    except Exception:  # noqa: BLE001 - see docstring
        return {}
    if not evidence.get("available"):
        return {}
    return harm.hurts(evidence.get("by_lesson", {}))
