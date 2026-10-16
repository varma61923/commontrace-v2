"""Causal gate: inject each memory where randomized evidence says it helps, and only there.

The holdout experiment answers "does this memory help on average?". A memory can
help one kind of task and hurt another: a lesson about a test runner helps the
coding agent and distracts the support agent. Averaging hides that; withdrawing
the memory everywhere throws the good half away. The causal gate learns *where*
each memory helps and turns that into the injection policy itself, while every
decision stays a logged randomized assignment, so the evidence keeps flowing and
every estimate stays honest.

Three pieces, all stdlib:

1. **Hierarchical effect model** (`fit`). Each logged assignment carries its own
   withholding probability, so a lesson's effect in a context is estimated by the
   self-normalized (Hajek) inverse-propensity contrast, valid under any published
   rates. Estimates are then shrunk by empirical Bayes, normal-normal with
   DerSimonian-Laird between-unit variances, along the hierarchy
   *source -> lesson -> (lesson, context)*. Each level's prior is computed without
   the unit's own data (leave-one-out), so no data is counted twice. The source
   level is a causal reputation: a new lesson from a source whose lessons have
   measurably hurt starts from a pessimistic prior instead of a neutral one, which
   blunts low-and-slow memory poisoning (AgentPoison, MemoryGraft) and bad bulk
   imports. A source is the lesson's provenance (`lesson_source`).

2. **Thompson allocation** (`rates`). The probability of injecting a memory in a
   context is the posterior probability that it helps there (probability matching,
   Thompson 1933), clipped to ``[floor, 1 - floor]`` so both arms keep receiving
   evidence. The rates are published as an allocation schedule (hash-chained,
   fixed before the occasions it governs), and schedules may now carry per-context
   rates, so `integrity` re-checks every contextual rate and `aipw` estimates the
   average effect exactly as for any adaptive schedule.

3. **Two safeguards.** `drift` tests whether a memory's effect changed over time
   (a tool upgrade, a codebase migration, an edited dependency) with a two-window
   Hajek contrast at fixed split points, Bonferroni within a lesson and
   Benjamini-Hochberg across lessons; a drifted memory is then gated on its evidence
   since the change only (`forget_before_drift`). `evaluate` is an off-policy check before a
   gate is trusted: the gate is fitted on the earlier half of the occasions and its
   value on the later half is estimated by self-normalized importance sampling over
   each occasion's whole joint assignment, against what was actually delivered.
   Fitting and evaluating on disjoint occasions keeps the estimate from grading its
   own homework; effective sample size is reported, and a gate whose weights
   collapse is reported as such rather than scored.

What this is not: the posterior is an empirical-Bayes approximation (normal
likelihood for a difference of proportions), so `p_helps` is a calibrated-enough
allocation weight, not a proof. Billing and release decisions keep using
`experiment.analyze` and `integrity.audit`, which this module never replaces.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_TAU = 0.10          # prior sd of a memory's effect before any data (10 points of success rate)
TAU_FLOOR = 0.03            # lessons are never pooled into one another completely
CONTEXT_TAU_FLOOR = 0.02    # contexts deviate from their lesson only on real evidence
DEFAULT_FLOOR = 0.05        # least withholding probability; also 1 - the greatest
CONTEXT_RATE_DELTA = 0.01   # a context rate is published only when it differs this much
MAX_CONTEXTS_PER_LESSON = 64
DRIFT_SPLITS = (0.25, 0.5, 0.75)
RESET_ALPHA = 0.01          # evidence is only forgotten on stronger proof of drift than is reported
MIN_OVERLAP_ESS = 30.0
MAX_LOG_WEIGHT = 50.0       # importance weights beyond e**50 are treated as unbounded
UNKNOWN_SOURCE = "local"


# -- inputs --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One resolved, randomized (lesson, occasion) assignment."""

    lesson: str
    occasion_id: str
    injected: bool
    succeeded: bool
    rate: float               # probability the lesson was withheld
    context: str = ""
    at: str = ""              # ISO time, for ordering

    @property
    def propensity(self) -> float:
        """Probability of the arm actually taken."""
        return (1.0 - self.rate) if self.injected else self.rate


def rows_from_assignments(assignments: Iterable[Any]) -> list[Row]:
    """Resolved, randomized rows from `integrity.Assignment`-shaped records, oldest first, one per pair."""
    seen: dict[tuple[str, str], Row] = {}
    for a in assignments:
        if a.succeeded is None or not 0.0 < float(a.rate) < 1.0:
            continue
        key = (a.lesson, a.occasion_id)
        if key in seen:
            continue
        seen[key] = Row(lesson=a.lesson, occasion_id=a.occasion_id, injected=bool(a.injected),
                        succeeded=bool(a.succeeded), rate=float(a.rate),
                        context=getattr(a, "context", None) or "",
                        at=a.at.isoformat() if getattr(a, "at", None) is not None else "")
    return sorted(seen.values(), key=lambda r: (r.at, r.occasion_id, r.lesson))


def lesson_source(frontmatter: Mapping[str, Any]) -> str:
    """The provenance a lesson's reputation is pooled by.

    Marketplace publisher, then the recorded provenance (kind and agent), then
    "distilled" for lessons drawn from traces, else "local".
    """
    for key in ("publisher",):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return "publisher:" + value.strip()[:80]
    market = frontmatter.get("marketplace")
    if isinstance(market, Mapping) and isinstance(market.get("publisher"), str) and market["publisher"].strip():
        return "publisher:" + market["publisher"].strip()[:80]
    provenance = frontmatter.get("provenance")
    if isinstance(provenance, Mapping):
        kind = provenance.get("source_kind")
        agent = provenance.get("source_agent")
        if isinstance(kind, str) and kind.strip():
            return "kind:" + kind.strip()[:60] + (f"/{agent.strip()[:40]}" if isinstance(agent, str) and agent.strip()
                                                    else "")
    if frontmatter.get("source_traces"):
        return "distilled"
    return UNKNOWN_SOURCE


def sources_for(root: str, lessons: Iterable[str]) -> dict[str, str]:
    """`lesson_source` for each lesson slug that resolves to a readable lesson file."""
    from commontrace import frontmatter, lesson_io

    out: dict[str, str] = {}
    for slug in set(lessons):
        path = lesson_io.lesson_path(root, slug)
        if not path:
            continue
        try:
            fm, _body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - an unreadable lesson pools with the unknown source
            continue
        out[slug] = lesson_source(fm if isinstance(fm, Mapping) else {})
    return out


# -- estimation ----------------------------------------------------------------------------------


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


@dataclass(frozen=True)
class Contrast:
    """Hajek inverse-propensity estimate of P(success | injected) - P(success | withheld)."""

    effect: float
    variance: float
    n_injected: int
    n_withheld: int


def _arm(rows: Sequence[Row]) -> tuple[float, float, int] | None:
    """Self-normalized mean, its linearized variance (floored by a Jeffreys term), and the count."""
    if not rows:
        return None
    weights = [1.0 / r.propensity for r in rows]
    total = math.fsum(weights)
    mean = math.fsum(w * r.succeeded for w, r in zip(weights, rows)) / total
    variance = math.fsum((w * (r.succeeded - mean)) ** 2 for w, r in zip(weights, rows)) / total ** 2
    n_eff = total ** 2 / math.fsum(w * w for w in weights)
    smoothed = (mean * n_eff + 0.5) / (n_eff + 1.0)
    return mean, max(variance, smoothed * (1.0 - smoothed) / (n_eff + 1.0)), len(rows)


def contrast(rows: Sequence[Row]) -> Contrast | None:
    """The effect estimate over `rows`, or None until both arms have data."""
    injected = _arm([r for r in rows if r.injected])
    withheld = _arm([r for r in rows if not r.injected])
    if injected is None or withheld is None:
        return None
    return Contrast(injected[0] - withheld[0], injected[1] + withheld[1], injected[2], withheld[2])


def _dersimonian_laird(groups: Sequence[Sequence[tuple[float, float]]]) -> float | None:
    """Between-unit variance of estimates (effect, variance), each group around its own mean."""
    q = df = s1 = s2 = 0.0
    for estimates in groups:
        if len(estimates) < 2:
            continue
        weights = [1.0 / v for _e, v in estimates]
        total = math.fsum(weights)
        mean = math.fsum(w * e for w, (e, _v) in zip(weights, estimates)) / total
        q += math.fsum(w * (e - mean) ** 2 for w, (e, _v) in zip(weights, estimates))
        df += len(estimates) - 1
        s1 += total
        s2 += math.fsum(w * w for w in weights) / total
    if df == 0:
        return None
    denominator = s1 - s2
    return max(0.0, (q - df) / denominator) if denominator > 0 else 0.0


def _combine(prior_mean: float, prior_var: float, estimate: Contrast | None) -> tuple[float, float]:
    """Normal-normal posterior (mean, variance)."""
    if estimate is None:
        return prior_mean, prior_var
    precision = 1.0 / prior_var + 1.0 / estimate.variance
    mean = (prior_mean / prior_var + estimate.effect / estimate.variance) / precision
    return mean, 1.0 / precision


@dataclass(frozen=True)
class Posterior:
    lesson: str
    context: str              # "" for the lesson as a whole
    source: str
    n_injected: int
    n_withheld: int
    raw_effect: float | None
    raw_se: float | None
    mean: float
    sd: float
    p_helps: float


@dataclass(frozen=True)
class SourceReputation:
    source: str
    lessons: int
    mean: float
    sd: float
    p_helps: float


@dataclass
class Model:
    lessons: dict[str, Posterior] = field(default_factory=dict)
    contexts: dict[tuple[str, str], Posterior] = field(default_factory=dict)
    sources: dict[str, SourceReputation] = field(default_factory=dict)
    tau_lesson: float = DEFAULT_TAU
    tau_source: float = 0.0
    tau_context: float = CONTEXT_TAU_FLOOR
    rows: int = 0

    def posterior(self, lesson: str, context: str = "") -> Posterior | None:
        if context and (lesson, context) in self.contexts:
            return self.contexts[(lesson, context)]
        return self.lessons.get(lesson)

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "tau": {"lesson": self.tau_lesson, "source": self.tau_source, "context": self.tau_context},
            "lessons": [asdict(p) for p in sorted(self.lessons.values(), key=lambda p: p.lesson)],
            "contexts": [asdict(p) for p in sorted(self.contexts.values(), key=lambda p: (p.lesson, p.context))],
            "sources": [asdict(s) for s in sorted(self.sources.values(), key=lambda s: s.source)],
        }


def _posterior(lesson: str, context: str, source: str, estimate: Contrast | None, n: tuple[int, int],
               mean: float, variance: float) -> Posterior:
    sd = math.sqrt(variance)
    return Posterior(
        lesson=lesson, context=context, source=source, n_injected=n[0], n_withheld=n[1],
        raw_effect=None if estimate is None else estimate.effect,
        raw_se=None if estimate is None else math.sqrt(estimate.variance),
        mean=mean, sd=sd, p_helps=_norm_cdf(mean / sd) if sd > 0 else float(mean > 0))


def _counts(rows: Sequence[Row]) -> tuple[int, int]:
    injected = sum(r.injected for r in rows)
    return injected, len(rows) - injected


def fit(rows: Sequence[Row], sources: Mapping[str, str] | None = None) -> Model:
    """The hierarchical posterior of every lesson's effect, overall and per context."""
    sources = sources or {}
    by_lesson: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_lesson[r.lesson].append(r)
    source_of = {lesson: sources.get(lesson, UNKNOWN_SOURCE) for lesson in by_lesson}
    raw = {lesson: contrast(lesson_rows) for lesson, lesson_rows in by_lesson.items()}
    estimated = {lesson: c for lesson, c in raw.items() if c is not None}

    tau2 = _dersimonian_laird([[(c.effect, c.variance) for c in estimated.values()]])
    tau2 = max(TAU_FLOOR ** 2, tau2) if tau2 is not None else DEFAULT_TAU ** 2

    # Precision-weighted sums per source and overall, so each lesson's prior can leave it out.
    def weight(c: Contrast) -> float:
        return 1.0 / (c.variance + tau2)

    total_w = math.fsum(weight(c) for c in estimated.values())
    total_we = math.fsum(weight(c) * c.effect for c in estimated.values())
    source_w: dict[str, float] = defaultdict(float)
    source_we: dict[str, float] = defaultdict(float)
    for lesson, c in estimated.items():
        source_w[source_of[lesson]] += weight(c)
        source_we[source_of[lesson]] += weight(c) * c.effect

    source_means = [[(source_we[s] / source_w[s], 1.0 / source_w[s]) for s in source_w if source_w[s] > 0]]
    tau2_source = _dersimonian_laird(source_means) or 0.0

    def prior(lesson: str) -> tuple[float, float]:
        own = estimated.get(lesson)
        w_own = weight(own) if own is not None else 0.0
        we_own = w_own * own.effect if own is not None else 0.0
        rest_w, rest_we = total_w - w_own, total_we - we_own
        global_mean = rest_we / rest_w if rest_w > 1e-12 else 0.0
        global_var = 1.0 / rest_w if rest_w > 1e-12 else DEFAULT_TAU ** 2
        s = source_of.get(lesson, UNKNOWN_SOURCE)
        s_w, s_we = source_w.get(s, 0.0) - w_own, source_we.get(s, 0.0) - we_own
        if s_w > 1e-12 and tau2_source > 0:
            s_var = 1.0 / s_w
            precision = 1.0 / tau2_source + 1.0 / s_var
            mean = (global_mean / tau2_source + (s_we / s_w) / s_var) / precision
            return mean, tau2 + 1.0 / precision
        return global_mean, tau2 + global_var

    model = Model(tau_lesson=math.sqrt(tau2), tau_source=math.sqrt(tau2_source), rows=len(rows))
    for lesson, lesson_rows in by_lesson.items():
        mean, variance = _combine(*prior(lesson), raw[lesson])
        model.lessons[lesson] = _posterior(lesson, "", source_of[lesson], raw[lesson], _counts(lesson_rows),
                                           mean, variance)

    # Contexts: deviations from the lesson, with the lesson's evidence from *other* contexts as the prior.
    cells: dict[str, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.context:
            cells[r.lesson][r.context].append(r)
    cell_raw = {(lesson, ctx): contrast(cell_rows)
                for lesson, by_ctx in cells.items() for ctx, cell_rows in by_ctx.items()}
    tau2_context = _dersimonian_laird([
        [(c.effect, c.variance) for (lesson, _ctx), c in cell_raw.items() if lesson == name and c is not None]
        for name in cells])
    tau2_context = max(CONTEXT_TAU_FLOOR ** 2, tau2_context or 0.0)
    model.tau_context = math.sqrt(tau2_context)
    for lesson, by_ctx in cells.items():
        ranked = sorted(by_ctx.items(), key=lambda item: (-len(item[1]), item[0]))[:MAX_CONTEXTS_PER_LESSON]
        for ctx, cell_rows in ranked:
            others = [r for r in by_lesson[lesson] if r.context != ctx]
            base_mean, base_var = _combine(*prior(lesson), contrast(others))
            mean, variance = _combine(base_mean, base_var + tau2_context, cell_raw[(lesson, ctx)])
            model.contexts[(lesson, ctx)] = _posterior(lesson, ctx, source_of[lesson], cell_raw[(lesson, ctx)],
                                                       _counts(cell_rows), mean, variance)

    for s in sorted(set(source_of.values())):
        members = [lesson for lesson, src in source_of.items() if src == s]
        w, we = source_w.get(s, 0.0), source_we.get(s, 0.0)
        if w > 1e-12:
            mean, variance = we / w, 1.0 / w + tau2_source
        else:
            mean, variance = (total_we / total_w, 1.0 / total_w) if total_w > 1e-12 else (0.0, DEFAULT_TAU ** 2)
        sd = math.sqrt(variance)
        model.sources[s] = SourceReputation(s, len(members), mean, sd, _norm_cdf(mean / sd) if sd > 0 else 0.5)
    return model


# -- allocation ----------------------------------------------------------------------------------


def withhold_rate(p_helps: float, floor: float = DEFAULT_FLOOR) -> float:
    """Probability matching: withhold with the posterior probability the memory does not help."""
    if not 0.0 < floor < 0.5:
        raise ValueError("floor must be in (0, 0.5)")
    return round(min(1.0 - floor, max(floor, 1.0 - p_helps)), 6)


def rates(model: Model, floor: float = DEFAULT_FLOOR) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Per-lesson withholding rates, and per-context rates where a context differs from its lesson."""
    lesson_rates = {lesson: withhold_rate(p.p_helps, floor) for lesson, p in model.lessons.items()}
    context_rates: dict[str, dict[str, float]] = defaultdict(dict)
    for (lesson, ctx), p in model.contexts.items():
        rate = withhold_rate(p.p_helps, floor)
        if abs(rate - lesson_rates.get(lesson, 0.5)) > CONTEXT_RATE_DELTA:
            context_rates[lesson][ctx] = rate
    return lesson_rates, {lesson: dict(sorted(by.items())) for lesson, by in sorted(context_rates.items())}


# -- drift ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Drift:
    lesson: str
    verdict: str              # DRIFTED | STABLE | INSUFFICIENT
    split_at: str = ""
    before: float | None = None
    after: float | None = None
    change: float | None = None
    p_value: float | None = None
    q_value: float | None = None
    n_before: int = 0
    n_after: int = 0


def _benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """BH-adjusted q-values, in input order."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m / rank)
        q[i] = running
    return q


def drift(rows: Sequence[Row], *, alpha: float = 0.05, min_arm: int = 10,
          practical: float = 0.10) -> list[Drift]:
    """Has each lesson's effect changed between an earlier and a later window?"""
    by_lesson: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_lesson[r.lesson].append(r)
    tested: list[tuple[Drift, float]] = []
    out: list[Drift] = []
    for lesson in sorted(by_lesson):
        lesson_rows = by_lesson[lesson]
        best: tuple[float, Drift] | None = None
        candidates = 0
        for fraction in DRIFT_SPLITS:
            k = int(len(lesson_rows) * fraction)
            before_rows, after_rows = lesson_rows[:k], lesson_rows[k:]
            if min(_counts(before_rows) + _counts(after_rows)) < min_arm:
                continue
            before, after = contrast(before_rows), contrast(after_rows)
            if before is None or after is None:
                continue
            candidates += 1
            change = after.effect - before.effect
            z = change / math.sqrt(before.variance + after.variance)
            p = math.erfc(abs(z) / math.sqrt(2.0))
            found = Drift(lesson, "STABLE", lesson_rows[k].at, before.effect, after.effect, change, p, None,
                          len(before_rows), len(after_rows))
            if best is None or p < best[0]:
                best = (p, found)
        if best is None:
            out.append(Drift(lesson, "INSUFFICIENT", n_before=len(lesson_rows)))
            continue
        adjusted = min(1.0, best[0] * candidates)
        tested.append((best[1], adjusted))
    q_values = _benjamini_hochberg([p for _d, p in tested])
    for (found, p), q in zip(tested, q_values):
        verdict = "DRIFTED" if q < alpha and abs(found.change or 0.0) >= practical else "STABLE"
        out.append(Drift(found.lesson, verdict, found.split_at, found.before, found.after, found.change,
                         p, q, found.n_before, found.n_after))
    return sorted(out, key=lambda d: d.lesson)


def forget_before_drift(rows: Sequence[Row], findings: Iterable[Drift],
                        alpha: float = RESET_ALPHA) -> tuple[list[Row], dict[str, str]]:
    """Drop each drifted lesson's evidence from before its change point.

    A memory whose effect changed is judged on the world as it is now: the gate
    learns from the occasions after the change, so a memory that stopped helping
    is gated off promptly instead of being propped up by its old record (and one
    that started helping is not held back by it). Drift is re-tested every era, so
    forgetting needs stronger evidence (`alpha`, default 1%) than reporting does.
    """
    resets = {d.lesson: d.split_at for d in findings
              if d.verdict == "DRIFTED" and d.split_at and d.q_value is not None and d.q_value < alpha}
    kept = [r for r in rows if r.lesson not in resets or r.at >= resets[r.lesson]]
    return kept, resets


# -- off-policy evaluation -----------------------------------------------------------------------


@dataclass(frozen=True)
class OffPolicy:
    verdict: str              # IMPROVES | WORSENS | INCONCLUSIVE | INSUFFICIENT_OVERLAP | INSUFFICIENT_DATA
    fit_occasions: int
    eval_occasions: int
    logged_value: float | None = None
    policy_value: float | None = None
    lift: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    effective_sample_size: float | None = None


RateFor = Callable[[str, str], float]


def policy_value(rows: Sequence[Row], rate_for: RateFor, *, z: float = 1.959963984540054) -> OffPolicy:
    """Self-normalized importance-sampling value of `rate_for` against what the logged rows delivered."""
    occasions: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        occasions[r.occasion_id].append(r)
    weights: list[float] = []
    outcomes: list[float] = []
    for occasion_rows in occasions.values():
        log_ratio = 0.0
        for r in occasion_rows:
            new_rate = min(1.0, max(0.0, rate_for(r.lesson, r.context)))
            new_p = (1.0 - new_rate) if r.injected else new_rate
            if new_p <= 0.0:
                log_ratio = -math.inf
                break
            log_ratio += math.log(new_p) - math.log(r.propensity)
        weights.append(math.exp(min(log_ratio, MAX_LOG_WEIGHT)) if log_ratio > -math.inf else 0.0)
        outcomes.append(float(occasion_rows[0].succeeded))
    n = len(weights)
    if n == 0:
        return OffPolicy("INSUFFICIENT_DATA", 0, 0)
    total = math.fsum(weights)
    logged = math.fsum(outcomes) / n
    if total <= 0:
        return OffPolicy("INSUFFICIENT_OVERLAP", 0, n, logged_value=logged, effective_sample_size=0.0)
    ess = total ** 2 / math.fsum(w * w for w in weights)
    value = math.fsum(w * y for w, y in zip(weights, outcomes)) / total
    mean_w = total / n
    influence = [(w / mean_w) * (y - value) - (y - logged) for w, y in zip(weights, outcomes)]
    se = math.sqrt(math.fsum(d * d for d in influence)) / n
    lift = value - logged
    low, high = lift - z * se, lift + z * se
    if ess < MIN_OVERLAP_ESS:
        verdict = "INSUFFICIENT_OVERLAP"
    elif low > 0:
        verdict = "IMPROVES"
    elif high < 0:
        verdict = "WORSENS"
    else:
        verdict = "INCONCLUSIVE"
    return OffPolicy(verdict, 0, n, logged, value, lift, low, high, ess)


def evaluate(rows: Sequence[Row], sources: Mapping[str, str] | None = None, *,
             floor: float = DEFAULT_FLOOR, default_rate: float = 0.5) -> OffPolicy:
    """Fit the gate on the earlier half of the occasions; estimate its value on the later half."""
    order = list(dict.fromkeys(r.occasion_id for r in rows))
    if len(order) < 2:
        return OffPolicy("INSUFFICIENT_DATA", 0, len(order))
    cut = set(order[: len(order) // 2])
    fit_rows = [r for r in rows if r.occasion_id in cut]
    eval_rows = [r for r in rows if r.occasion_id not in cut]
    lesson_rates, context_rates = rates(fit(fit_rows, sources), floor)

    def rate_for(lesson: str, context: str) -> float:
        if context and context in context_rates.get(lesson, {}):
            return context_rates[lesson][context]
        return lesson_rates.get(lesson, default_rate)

    result = policy_value(eval_rows, rate_for)
    return OffPolicy(result.verdict, len(cut), result.eval_occasions, result.logged_value, result.policy_value,
                     result.lift, result.ci_low, result.ci_high, result.effective_sample_size)


# -- the store -----------------------------------------------------------------------------------


def current_rows(root: str) -> list[Row]:
    """Resolved randomized rows of the running experiment (current salt only)."""
    from commontrace.commands import experiment_cmd

    assignments, _rate, _corrupt = experiment_cmd._load(root)
    assignments, _salt, _other = experiment_cmd.scope_to_current_salt(root, assignments)
    return rows_from_assignments(assignments)


def plan_rates(root: str, floor: float) -> tuple[dict[str, float], dict[str, dict[str, float]], dict]:
    """The next era's rates for a Thompson allocation schedule, with a compact basis for the record."""
    rows = current_rows(root)
    gated, resets = forget_before_drift(rows, drift(rows))
    model = fit(gated, sources_for(root, {r.lesson for r in rows}))
    lesson_rates, context_rates = rates(model, floor)
    basis = {
        "mode": "thompson",
        "resolved": len(rows),
        "drift_resets": resets,
        "tau": {"lesson": round(model.tau_lesson, 6), "source": round(model.tau_source, 6),
                "context": round(model.tau_context, 6)},
        "p_helps": {lesson: round(p.p_helps, 4) for lesson, p in sorted(model.lessons.items())},
    }
    return lesson_rates, context_rates, basis


def report(root: str, *, alpha: float = 0.05, min_arm: int = 10, practical: float = 0.10,
           floor: float = DEFAULT_FLOOR) -> dict:
    """Everything the gate knows: posteriors, source reputations, proposed rates, drift and an off-policy check."""
    rows = current_rows(root)
    sources = sources_for(root, {r.lesson for r in rows})
    findings = drift(rows, alpha=alpha, min_arm=min_arm, practical=practical)
    gated, resets = forget_before_drift(rows, findings)
    model = fit(gated, sources)
    lesson_rates, context_rates = rates(model, floor)
    return {
        "model": model.to_dict(),
        "proposed_rates": {"lessons": lesson_rates, "contexts": context_rates, "floor": floor,
                           "drift_resets": resets},
        "drift": [asdict(d) for d in findings],
        "off_policy": asdict(evaluate(rows, sources, floor=floor)),
        "note": ("Posteriors are empirical-Bayes allocation weights, not proofs; "
                 "`commontrace experiment` remains the evidence of record."),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1%}"


def render(out: Mapping[str, Any]) -> str:
    """The `report` as text."""
    model = out["model"]
    lines = [f"[commontrace] causal gate over {model['rows']} resolved assignments "
             f"(between-lesson sd {model['tau']['lesson']:.1%}, between-context sd {model['tau']['context']:.1%})"]
    if not model["lessons"]:
        lines.append("  no resolved assignments yet: run an experiment and record outcomes first")
        return "\n".join(lines)
    proposed = out["proposed_rates"]
    lines.append("  memories (posterior effect, P(helps), proposed withholding):")
    for p in model["lessons"]:
        lines.append(f"    {p['lesson']}: {_pct(p['mean'])} +/- {p['sd']:.1%}, P(helps) {p['p_helps']:.0%}, "
                     f"withhold {proposed['lessons'].get(p['lesson'], 0.5):.0%} "
                     f"[{p['n_injected']} shown / {p['n_withheld']} withheld, source {p['source']}]")
        contexts = [c for c in model["contexts"] if c["lesson"] == p["lesson"]]
        for c in contexts if len(contexts) > 1 else []:
            rate = proposed["contexts"].get(p["lesson"], {}).get(c["context"])
            gate = f", withhold {rate:.0%}" if rate is not None else ""
            lines.append(f"      in {c['context']!r}: {_pct(c['mean'])} +/- {c['sd']:.1%}, "
                         f"P(helps) {c['p_helps']:.0%}{gate}")
    if len(model["sources"]) > 1:
        lines.append("  source reputation (pooled effect of a source's memories):")
        lines += [f"    {s['source']}: {_pct(s['mean'])} +/- {s['sd']:.1%} over {s['lessons']} memories, "
                  f"P(helps) {s['p_helps']:.0%}" for s in model["sources"]]
    drifted = [d for d in out["drift"] if d["verdict"] == "DRIFTED"]
    lines.append(f"  drift: {len(drifted)} of {len(out['drift'])} memories changed effect")
    lines += [f"    {d['lesson']}: {_pct(d['before'])} -> {_pct(d['after'])} "
              f"from {d['split_at']} (q={d['q_value']:.3g})" for d in drifted]
    ope = out["off_policy"]
    if ope["lift"] is None:
        lines.append(f"  off-policy check: {ope['verdict'].lower().replace('_', ' ')}")
    else:
        lines.append(f"  off-policy check (fit on {ope['fit_occasions']} occasions, scored on "
                     f"{ope['eval_occasions']} later ones): gate {ope['policy_value']:.1%} vs delivered "
                     f"{ope['logged_value']:.1%}, lift {_pct(ope['lift'])} "
                     f"[{_pct(ope['ci_low'])}, {_pct(ope['ci_high'])}], ESS {ope['effective_sample_size']:.0f}: "
                     f"{ope['verdict']}")
    lines.append("  " + out["note"])
    return "\n".join(lines)
