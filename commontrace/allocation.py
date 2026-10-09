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
import hashlib
import json
import math
import os
from collections import deque
from dataclasses import asdict, dataclass, field

from commontrace import experiment, paths

EXPLORE_RATE = 0.5
MONITOR_RATE = 0.05
ERA_OCCASIONS = 250
EFFECTIVE_DELAY_S = 5.0
WINDOW = 400
BURN_IN = 50
SCHEDULE_DOMAIN = "commontrace-allocation-schedule-v1"
SCHEDULES_NAME = "schedules.jsonl"


# -- estimation ----------------------------------------------------------------------------------


def _mixture_rho2(alpha: float, horizon: float) -> float:
    """The normal-mixture precision that makes the sequence tightest at `horizon` unit-variance steps."""
    log_alpha = math.log(alpha)
    return (-2.0 * log_alpha + math.log(1.0 - 2.0 * log_alpha)) / max(1.0, horizon)


def confidence_sequence(scores: list[float], alpha: float = 0.05,
                        horizon: float = 2000.0) -> tuple[float, float, float]:
    """(mean, low, high): a two-sided asymptotic confidence sequence for the mean of `scores`.

    ``mean +- sqrt(2 (V rho^2 + 1) / rho^2 * log(sqrt(V rho^2 + 1) / alpha)) / t`` with
    ``V = t * sample variance``; ``rho`` is fixed by ``alpha`` and ``horizon`` before any data.
    """
    t = len(scores)
    if t < 2:
        return (scores[0] if scores else 0.0), -math.inf, math.inf
    mean = math.fsum(scores) / t
    variance = math.fsum((s - mean) ** 2 for s in scores) / (t - 1)
    rho2 = _mixture_rho2(alpha, horizon)
    v = t * variance * rho2 + 1.0
    radius = math.sqrt(2.0 * v / rho2 * math.log(math.sqrt(v) / alpha)) / t
    return mean, mean - radius, mean + radius


def running_intersection(scores: list[float], alpha: float = 0.05, horizon: float = 2000.0,
                         burn_in: int = BURN_IN) -> tuple[float, float, float]:
    """The confidence sequence intersected over every prefix from `burn_in` scores on.

    A time-uniform sequence covers at every time at once, so its running intersection
    covers too, and never widens: a verdict reached once is not undone when a later
    era's lower propensities make the per-occasion scores noisier.
    """
    t = len(scores)
    mean, low, high = confidence_sequence(scores, alpha, horizon)
    if t <= burn_in:
        return mean, low, high
    rho2 = _mixture_rho2(alpha, horizon)
    total = math.fsum(scores[:burn_in - 1])
    square = math.fsum(s * s for s in scores[:burn_in - 1])
    for k in range(burn_in, t + 1):
        s = scores[k - 1]
        total += s
        square += s * s
        m = total / k
        v = k * max(0.0, (square - k * m * m) / (k - 1)) * rho2 + 1.0
        radius = math.sqrt(2.0 * v / rho2 * math.log(math.sqrt(v) / alpha)) / k
        low, high = max(low, m - radius), min(high, m + radius)
    if low > high:  # the sequences disagree: only possible on a miss, so report the latest
        return confidence_sequence(scores, alpha, horizon)
    return mean, low, high


def fixed_interval(scores: list[float], alpha: float = 0.05) -> tuple[float, float, float, float]:
    """(mean, low, high, two-sided p) for ONE look: the central-limit interval of the AIPW mean."""
    t = len(scores)
    if t < 2:
        return (scores[0] if scores else 0.0), -math.inf, math.inf, 1.0
    mean = math.fsum(scores) / t
    se = math.sqrt(math.fsum((s - mean) ** 2 for s in scores) / (t - 1) / t)
    z = experiment._norm_ppf(1.0 - alpha / 2.0)
    if se == 0.0:
        return mean, mean, mean, (1.0 if mean == 0.0 else 0.0)
    p = 2.0 * (1.0 - experiment._norm_cdf(abs(mean) / se))
    return mean, mean - z * se, mean + z * se, p


@dataclass(frozen=True)
class Observation:
    injected: bool
    succeeded: bool
    rate: float  # probability of being withheld, as logged
    stratum: str = ""


class _RunningMean:
    """Mean of the last `window` outcomes, shrunk toward 1/2 by one pseudo-observation per side."""

    def __init__(self, window: int):
        self.values: deque[float] = deque(maxlen=window)
        self.total = 0.0

    def get(self, fallback: float | None = None) -> float:
        prior = 0.5 if fallback is None else fallback
        return (self.total + 2.0 * prior) / (len(self.values) + 2.0)

    def add(self, value: float) -> None:
        if len(self.values) == self.values.maxlen:
            self.total -= self.values[0]
        self.values.append(value)
        self.total += value


def aipw_scores(observations: list[Observation], window: int = WINDOW) -> list[float]:
    """One AIPW pseudo-outcome per randomized observation, in the order given.

    Observations whose rate is 0 or 1 were not randomized and carry no
    information about the effect; they are skipped.
    """
    pooled = {True: _RunningMean(window), False: _RunningMean(window)}
    by_stratum: dict[tuple[str, bool], _RunningMean] = {}
    scores = []
    for o in observations:
        if not 0.0 < o.rate < 1.0:
            continue
        means = {}
        for arm in (True, False):
            overall = pooled[arm].get()
            means[arm] = by_stratum[(o.stratum, arm)].get(overall) if (o.stratum, arm) in by_stratum else overall
        y = 1.0 if o.succeeded else 0.0
        pi = 1.0 - o.rate
        if o.injected:
            scores.append(means[True] - means[False] + (y - means[True]) / pi)
        else:
            scores.append(means[True] - means[False] - (y - means[False]) / o.rate)
        pooled[o.injected].add(y)
        if o.stratum:
            by_stratum.setdefault((o.stratum, o.injected), _RunningMean(window)).add(y)
    return scores


@dataclass(frozen=True)
class Estimate:
    effect: float
    ci_low: float
    ci_high: float
    p_value: float
    n_injected: int
    n_withheld: int
    method: str


def estimate(observations: list[Observation], *, alpha: float = 0.05, horizon: float = 2000.0,
             sequential: bool = True, window: int = WINDOW) -> Estimate:
    scores = aipw_scores(observations, window)
    randomized = [o for o in observations if 0.0 < o.rate < 1.0]
    n_inj = sum(o.injected for o in randomized)
    _mean, low, high, p = fixed_interval(scores, alpha)
    mean = _mean
    if sequential:
        mean, low, high = running_intersection(scores, alpha, horizon)
        if len(scores) < BURN_IN:
            # The sequence's coverage is asymptotic; before the burn-in it claims nothing.
            low, high = -math.inf, math.inf
    return Estimate(mean, max(-1.0, low), min(1.0, high), p, n_inj, len(randomized) - n_inj,
                    "aipw-asymptotic-cs" if sequential else "aipw-clt")


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


@dataclass
class Schedule:
    version: int
    salt: str
    effective_from: str
    default_rate: float
    rates: dict[str, float]
    policy: dict
    basis: dict
    previous: str
    created_at: str
    enabled: bool = True
    digest: str = field(default="", compare=False)

    def body(self) -> dict:
        out = asdict(self)
        out.pop("digest")
        return out

    def rate_for(self, lesson: str) -> float:
        return self.rates.get(lesson, self.default_rate)

    def effective_at(self) -> datetime.datetime:
        return datetime.datetime.fromisoformat(self.effective_from)


def schedule_digest(body: dict) -> str:
    """PROTOCOL 13.5: SHA-256 over the domain, U+001E, and the compact sorted-key JSON of the schedule."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((SCHEDULE_DOMAIN + "\x1e" + canonical).encode("utf-8")).hexdigest()


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


def in_force(schedules: list[Schedule], salt: str, at: datetime.datetime) -> Schedule | None:
    """The schedule governing an assignment made at `at` in the experiment `salt`, if any."""
    current = None
    for s in schedules:
        if s.salt != salt:
            continue
        if s.effective_at() <= at:
            current = s
    return current if current is not None and current.enabled else None


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


def unscheduled(rows: list, schedules: list[Schedule]) -> list:
    """Assignments whose logged rate is not the one their schedule set (empty when every rate is accounted for)."""
    out = []
    for r in rows:
        if r.at is None:
            out.append(r)
            continue
        governing = in_force(schedules, r.salt, r.at)
        if governing is not None and abs(governing.rate_for(r.lesson) - r.rate) > 1e-9:
            out.append(r)
    return out
