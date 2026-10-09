"""Adaptive holdout allocation, and the estimator that stays valid under it.

A fixed holdout rate is a poor compromise. At 10% a new memory takes years to
prove; at 50% a memory already proven to help is withheld from half the
occasions forever. Adaptive allocation runs in *eras*: at the start of each
era, a published schedule sets every memory's rate from the evidence so far,
using only data from before the era:

- still uncertain: ``explore`` (default 50%, the most informative split);
- proven to help: ``monitor`` (default 5%; enough to notice if it stops helping);
- proven to have no effect worth acting on: ``monitor``;
- proven to hurt: ``1 - monitor`` (it is mostly withheld while the harm policy
  decides whether to withdraw it entirely).

Assignment stays the deterministic hash of PROTOCOL 13.1; only the rate it is
compared against comes from the schedule. Schedules are hash-chained and take
effect a few seconds after they are written, so every assignment can be
re-checked against the schedule that was in force when it was made.

Once rates vary, a plain difference in means is no longer an honest estimate
(an arm's share of easy occasions can change between eras). Every assignment
logs its own rate, so the effect is estimated by augmented inverse-propensity
weighting (AIPW): with injection probability ``pi = 1 - rate`` and running arm
means ``m1, m0`` computed only from earlier occasions,

    phi = m1 - m0 + Z (Y - m1) / pi - (1 - Z) (Y - m0) / (1 - pi)

is unbiased for the effect on that occasion whatever the earlier data were,
so the ``phi`` form a martingale-difference sequence. The interval is the
asymptotic confidence sequence of Waudby-Smith, Arbour, Sinha, Kennedy and
Ramdas (Annals of Statistics, 2024): time-uniform, so it may be read after
every occasion, and valid under the adaptive schedule because each rate is
fixed before the occasions it governs. Its coverage is asymptotic; the
simulation in ``benchmarks/adaptive_allocation_bench.py`` measures it.

Optional strata (a short pre-treatment label such as a task type or difficulty
band) let the running means condition on the occasion, which narrows the
interval when the label predicts the outcome. Strata never change what is
estimated, only how precisely.
"""
from __future__ import annotations

import datetime
import json
import os
from dataclasses import asdict, dataclass

from commontrace import experiment, paths
from commontrace.aipw import (  # noqa: F401 - re-exported
    BURN_IN,
    WINDOW,
    Estimate,
    Observation,
    _mixture_rho2,
    aipw_scores,
    confidence_sequence,
    estimate,
    fixed_interval,
    running_intersection,
)
from commontrace.allocation_schedule import (  # noqa: F401 - re-exported
    SCHEDULE_DOMAIN,
    Schedule,
    in_force,
    schedule_digest,
    unscheduled,
)

EXPLORE_RATE = 0.5
MONITOR_RATE = 0.05
ERA_OCCASIONS = 250
EFFECTIVE_DELAY_S = 5.0
SCHEDULES_NAME = "schedules.jsonl"


# -- policy and schedules --------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    explore: float = EXPLORE_RATE
    monitor: float = MONITOR_RATE
    era_occasions: int = ERA_OCCASIONS

    def __post_init__(self):
        if not 0.0 < self.monitor < self.explore < 1.0:
            raise ValueError("rates must satisfy 0 < monitor < explore < 1")
        if self.era_occasions < 1:
            raise ValueError("an era needs at least one occasion")


def next_rate(verdict: str, policy: Policy) -> float:
    if verdict in (experiment.VERDICT_HELPS, experiment.VERDICT_NO_EFFECT):
        return policy.monitor
    if verdict == experiment.VERDICT_HURTS:
        return round(1.0 - policy.monitor, 6)
    return policy.explore


def _path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), "allocation", SCHEDULES_NAME)


_CACHE: dict[str, tuple[tuple, list[Schedule]]] = {}


def history(root: str) -> list[Schedule]:
    """Every published schedule, oldest first. Cached on the file's identity, so the assignment path stays cheap."""
    path = _path(root)
    try:
        st = os.stat(path)
    except OSError:
        return []
    ident = (st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == ident:
        return cached[1]
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                raw = json.loads(line)
                out.append(Schedule(**{k: v for k, v in raw.items()}))
    _CACHE[path] = (ident, out)
    return out


def verify(root: str) -> list[str]:
    """Problems with the schedule chain; empty when every digest and link holds."""
    problems, previous = [], ""
    for n, s in enumerate(history(root)):
        if schedule_digest(s.body()) != s.digest:
            problems.append(f"schedule {n} does not hash to its recorded digest")
        if s.previous != previous:
            problems.append(f"schedule {n} does not link to schedule {n - 1}")
        previous = s.digest
    return problems


def active(root: str, salt: str, now: datetime.datetime | None = None) -> Schedule | None:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return in_force(history(root), salt, now)


def _publish(root: str, *, salt: str, default_rate: float, rates: dict[str, float], policy: Policy,
             basis: dict, enabled: bool = True, now: datetime.datetime | None = None) -> Schedule:
    from commontrace import frontmatter, holdout_io

    now = now or datetime.datetime.now(datetime.timezone.utc)
    path = _path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        past = history(root)
        schedule = Schedule(
            version=len(past), salt=salt,
            effective_from=(now + datetime.timedelta(seconds=EFFECTIVE_DELAY_S)).isoformat(),
            default_rate=default_rate, rates=dict(sorted(rates.items())), policy=asdict(policy), basis=basis,
            previous=past[-1].digest if past else "", created_at=now.isoformat(), enabled=enabled)
        schedule.digest = schedule_digest(schedule.body())
        holdout_io._append_lines(path, [json.dumps({**schedule.body(), "digest": schedule.digest},
                                                   sort_keys=True, ensure_ascii=False)])
    _CACHE.pop(path, None)
    return schedule


def enable(root: str, policy: Policy | None = None, *, now: datetime.datetime | None = None) -> Schedule:
    """Start adaptive allocation for the running experiment: every memory begins at the explore rate."""
    from commontrace import holdout_io

    config = holdout_io.load_config(root)
    if not (config.started_at and config.running):
        raise ValueError("start an experiment first (commontrace experiment --configure)")
    policy = policy or Policy()
    return _publish(root, salt=config.salt, default_rate=policy.explore, rates={}, policy=policy,
                    basis={"reason": "enabled"}, now=now)


def disable(root: str, *, now: datetime.datetime | None = None) -> Schedule:
    """Return to the experiment's configured fixed rate from the next assignment on."""
    from commontrace import holdout_io

    config = holdout_io.load_config(root)
    return _publish(root, salt=config.salt, default_rate=config.rate, rates={}, policy=Policy(),
                    basis={"reason": "disabled"}, enabled=False, now=now)


def plan(root: str, *, now: datetime.datetime | None = None, alpha: float = 0.05) -> Schedule:
    """Publish the next era's rates from every outcome recorded so far."""
    from commontrace import holdout_io
    from commontrace.commands import experiment_cmd

    config = holdout_io.load_config(root)
    current = active(root, config.salt, now)
    if current is None:
        raise ValueError("adaptive allocation is not enabled for this experiment (commontrace allocate enable)")
    policy = Policy(**current.policy)
    rows, _rate, _corrupt = experiment_cmd._load(root)
    rows, _salt, _other = experiment_cmd.scope_to_current_salt(root, rows)
    effects = experiment.analyze(experiment_cmd._observations(rows), sequential=True, alpha=alpha)
    rates = {e.lesson_slug: next_rate(e.verdict, policy) for e in effects}
    resolved = sum(r.succeeded is not None for r in rows)
    basis = {"assignments": len(rows), "resolved": resolved,
             "verdicts": {e.lesson_slug: e.verdict for e in effects}, "previous_version": current.version}
    return _publish(root, salt=config.salt, default_rate=policy.explore, rates=rates, policy=policy,
                    basis=basis, now=now)


def due(root: str, salt: str) -> bool:
    """Has the current era seen its planned number of occasions?"""
    from commontrace import holdout_io

    current = active(root, salt)
    if current is None:
        return False
    records, _ = holdout_io.read_log(root)
    since = current.effective_at()
    seen = {r.occasion_id for r in records if r.salt == salt and r.at is not None and r.at >= since}
    return len(seen) >= current.policy.get("era_occasions", ERA_OCCASIONS)
