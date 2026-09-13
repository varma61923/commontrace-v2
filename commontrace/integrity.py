"""Is the causal number trustworthy? -- validity checks over a holdout run.

WHY THIS EXISTS
---------------
`commontrace/experiment.py` estimates each lesson's causal effect correctly:
two-proportion test, 95% CI, Benjamini-Hochberg across lessons, and an
explicit UNDERPOWERED verdict so a small sample never reads as "no effect".
The arithmetic is right.

But an estimate is only as good as the sample it was computed on, and
`analyze()` can only see the occasions that HAVE an outcome. What it cannot
see -- structurally, because they are not in its input -- is everything that
went missing on the way. Both tiers already noticed the hazard and stopped
one step short of it. hub/models.py, on `succeeded` being NULL:

    An observation with no outcome is excluded from the analysis rather
    than counted as a failure: an agent that crashed before reporting is
    missing data, and scoring it as a loss would bias the arm that
    crashed more.

That is the correct handling. It is unbiased **only if the missingness is
the same in both arms**, and nothing anywhere checked that. Differential
attrition is the single most common way a randomized experiment silently
produces a confident wrong answer, and it is *especially* likely here: the
withheld arm is, by construction, the arm working without its memory, so it
is the arm more likely to run long, escalate, or be abandoned before anyone
records how it went. That is not a hypothetical -- it is the treatment
effect itself, leaking into who gets measured.

The failure mode is the dangerous kind. It does not error, it does not look
empty, and it does not read as underpowered. It reads as a clean,
significant, well-powered result with a plausible effect size, and the
product's strongest claim is built on exactly that output.
tests/test_integrity.py contains a fleet where the memory does nothing at
all and `analyze()` reports HELPS at p<0.01, purely from a 25-point gap in
who got an outcome recorded.

WHAT THIS IS AND IS NOT
-----------------------
These checks say whether the comparison was run on a sound sample. They do
NOT re-estimate the effect, correct it, or replace it. A compromised
experiment does not get a fixed number here -- it gets a report saying the
number should not be read, and why, which is the honest output.

Each finding carries a severity, and the severities mean different things:

  INVALIDATES -- a specific, identified mechanism is biasing the estimate.
                 The reported effect is not an estimate of the causal
                 effect. Do not quote it.
  WEAKENS     -- the sample is degraded but not demonstrably biased
                 (usually: power lost). The number is still an estimate,
                 with less behind it than its confidence interval implies.
  OK          -- checked, nothing found. Stated explicitly rather than
                 omitted, so a silent report is never mistaken for a clean
                 one.

Absence of a finding is not proof of validity: these detect the failures
that leave a trace in the assignment log. Contamination -- an agent that
used a lesson it was told to withhold -- leaves none, and no amount of
analysis here can find it. That one is honoured by the client or not at
all, which is why it is stated in the tool descriptions and the skill
rather than checked here.
"""

from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass, field

from commontrace import experiment, survival

# A p-value below this on an arm-comparison check means the imbalance is
# unlikely to be chance. Deliberately LOOSER than the 0.05 the effect
# analysis uses, because the two tests are asked in opposite directions: for
# an effect, a false positive is the expensive error, so the bar is high;
# for a validity check, a false NEGATIVE is the expensive error -- missing a
# real bias means publishing a wrong number -- so the bar is lower. A flagged
# experiment costs someone a look; an unflagged broken one costs the claim.
VALIDITY_ALPHA = 0.10

# Arm balance gets its OWN, far stricter alpha, and the reason is that the two
# checks are looking for effects of completely different size.
#
# Attrition is a gradient: a 10-point reporting gap between arms is a real
# problem and is what VALIDITY_ALPHA is tuned to catch. Arm balance is not a
# gradient. Assignment is a deterministic hash compared against a threshold,
# so it is either doing that or it is not -- and a broken assigner (one arm
# always, a rate off by 5x, a client on a different rate) misses by many
# standard deviations, not by a couple.
#
# Measured, because the first version of this check used VALIDITY_ALPHA and
# the consequence is not intuitive: a CORRECT randomizer trips a two-sided
# test at alpha=0.10 about 10% of the time, at EVERY n -- that is what an
# alpha is. This check runs on every experiment, so one sound run in ten
# would have been reported COMPROMISED for nothing. A validity report whose
# findings are mostly noise is worse than no validity report, because it
# teaches people to skip the section where the real ones appear. Found by a
# test that failed roughly one run in fifteen under random ordering.
#
# At 0.001 a correct randomizer is essentially never flagged and every
# realistic breakage still is (tests/test_integrity.py measures both).
ARM_BALANCE_ALPHA = 0.001

# Overall missing-outcome share above which the run is called degraded even
# when the two arms lose data at the SAME rate. Symmetric attrition does not
# bias the estimate, it just shrinks it, but at this level the run is mostly
# unobserved and the CI stops describing what a reader thinks it describes.
ATTRITION_WEAKENS_AT = 0.30

# The share of eventually-reported occasions used to define "has had a fair
# chance to report". An occasion younger than the time by which this much of
# the run's OWN reported outcomes had arrived is not attrition, it is pending:
# it has not yet reached the age at which its absence would mean anything.
#
# Derived from the run's own reporting curve rather than fixed in wall-clock
# time, because the honest horizon is a property of the work. A fleet closing
# support tickets in ninety seconds and one escalating incidents over four days
# would each be mis-served by any constant, and a constant is exactly the kind
# of unmeasured policy number this package exists to avoid.
MATURITY_QUANTILE = 0.90

# How far apart the two arms' reporting SCHEDULES have to be before the run is
# called too early to read. Deliberately looser than VALIDITY_ALPHA: a speed
# difference is not itself a defect -- a lesson that helps should close work
# sooner -- so this only fires to say "wait", never to say "biased".
CENSORING_ALPHA = 0.05

# What the experiment randomizes, as a word for the reports.
#
# The two tiers randomize different objects: the local tier withholds
# LESSONS, the Hub withholds TRACES. The checks are identical and the finding
# text is not -- a Hub customer reading "1 lesson(s) were edited" about their
# own traces has been handed the other tier's vocabulary and will go looking
# for a lesson they do not have. Threaded through rather than hardcoded,
# because getting this wrong is invisible to every test that only reads
# severities.
UNIT_LESSON = "lesson"
UNIT_TRACE = "trace"

SEVERITY_OK = "OK"
SEVERITY_WEAKENS = "WEAKENS"
SEVERITY_INVALIDATES = "INVALIDATES"

VERDICT_SOUND = "SOUND"
VERDICT_WEAKENED = "WEAKENED"
VERDICT_COMPROMISED = "COMPROMISED"

_SEVERITY_RANK = {SEVERITY_OK: 0, SEVERITY_WEAKENS: 1, SEVERITY_INVALIDATES: 2}
_VERDICT_FOR = {0: VERDICT_SOUND, 1: VERDICT_WEAKENED, 2: VERDICT_COMPROMISED}


@dataclass(frozen=True)
class Assignment:
    """One (lesson, occasion) arm decision, whether or not it was ever resolved.

    The unit both tiers record at decision time. `succeeded is None` means no
    outcome was ever reported for that occasion -- the case `experiment.analyze`
    never sees, and the one most of this module is about.
    """

    lesson: str
    occasion_id: str
    injected: bool
    rate: float = experiment.DEFAULT_HOLDOUT_RATE
    salt: str = ""
    succeeded: bool | None = None
    at: datetime.datetime | None = None
    # WHEN the outcome came back, against `at` above being when the arm was
    # decided. The gap between them is follow-up time, and it is the only
    # thing that can tell an occasion nobody has reported YET apart from one
    # nobody will EVER report (commontrace/survival.py). None on a row that is
    # still waiting -- and also on any row written before this was recorded,
    # where the checks fall back to their untimed behaviour rather than
    # guessing a duration.
    resolved_at: datetime.datetime | None = None
    # Content identity of the lesson AS IT WAS on this occasion
    # (commontrace/revision.py). None means unknown -- an assignment logged
    # before revisions were recorded, or a lesson that could not be read --
    # and unknown is treated as unknown, never as a change.
    revision: str | None = None
    # WHY this lesson was eligible: how strongly it matched, where it placed,
    # and the retrieval settings that judged it. None on assignments logged
    # before these were recorded; the checks that read them skip such rows
    # rather than inferring a value they do not have.
    relevance: float | None = None
    rank: int | None = None
    scorer: str | None = None
    floor: float | None = None


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    headline: str
    detail: str
    numbers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Projection:
    """When, at the rate outcomes are currently arriving, a lesson can be answered."""

    lesson: str
    n_injected: int
    n_withheld: int
    needed_per_arm: int
    binding_arm: str
    still_needed: int
    per_day: float | None
    days_remaining: float | None
    eta: datetime.date | None
    advice: str


@dataclass(frozen=True)
class IntegrityReport:
    verdict: str
    findings: list[Finding]
    projections: list[Projection]
    n_assignments: int
    n_resolved: int
    n_duplicates: int = 0
    # What the experiment randomizes -- "lesson" locally, "trace" on the Hub.
    # Purely a word for the rendered findings; every check is identical.
    unit: str = UNIT_LESSON

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEVERITY_INVALIDATES]

    @property
    def readable(self) -> bool:
        """Can the effect estimate be quoted at all?"""
        return self.verdict != VERDICT_COMPROMISED


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _binomial_tail_p(k: int, n: int, p: float) -> float:
    """Two-sided p for k successes in n Bernoulli(p) trials, via normal
    approximation with a continuity correction.

    Approximate rather than exact on purpose: the exact binomial needs a sum
    of n choose k terms that overflows on the sample sizes a real fleet
    produces, and this check exists to raise an eyebrow at a badly wrong
    assignment rate, not to price an option. Guarded below for the small-n
    case where the approximation is worthless -- there it declines to answer
    rather than returning a confident wrong number.
    """
    if n <= 0 or not (0.0 < p < 1.0):
        return 1.0
    mean = n * p
    sd = (n * p * (1.0 - p)) ** 0.5
    if sd <= 0:
        return 1.0
    z = (abs(k - mean) - 0.5) / sd
    if z <= 0:
        return 1.0
    return 2.0 * (1.0 - experiment._norm_cdf(z))


def _resolved(rows: list[Assignment]) -> list[Assignment]:
    return [r for r in rows if r.succeeded is not None]


def normalize(rows: list[Assignment]) -> tuple[list[Assignment], int]:
    """One row per (lesson, occasion); returns it with the retries collapsed.

    The log is append-only and a retried task logs the same pair again.
    Counting a retry twice inflates that arm and deflates the p-value, so a
    retry storm manufactures significance out of nothing.

    Lives here rather than in the caller because "what counts as one
    assignment" has to be the same question for the estimate and for the
    checks on it. If the analysis collapsed retries and the attrition check
    did not, the check would be computing a missing-outcome rate against a
    denominator the estimate never used -- and disagreeing with the thing it
    is auditing is the one thing an auditor may not do.

    Later duplicates are dropped rather than merged: assignment is
    deterministic, so within one randomization they are identical by
    construction. When they are NOT, that is a real defect and
    `check_inconsistent_arms` reads the raw rows to catch it -- which is why
    that check runs before this collapse and not after.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[Assignment] = []
    duplicates = 0
    for r in rows:
        key = (r.lesson, r.occasion_id)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique.append(r)
    return unique, duplicates


# --- the checks ----------------------------------------------------------


def _follow_up(
    rows: list[Assignment], now: datetime.datetime | None
) -> list[tuple[Assignment, survival.Observation]] | None:
    """Pair each row with how long it was watched, or None if unanswerable.

    Returns None -- meaning "fall back to the untimed behaviour" -- whenever
    the log cannot support a timed reading:

      * no row carries `at`, so nothing can be placed on a clock at all; or
      * some row HAS an outcome but no `resolved_at`, so the event is known to
        have happened at an unknown time.

    The second rule is deliberately strict. A resolved row with no timestamp is
    interval-censored: placing it at `now` would stretch its follow-up to the
    full age of the run and drag the reporting curve right, and placing it at
    `at` would compress it to zero and drag the curve left. Either guess moves
    the horizon that decides which occasions count as attrition. Refusing the
    timed path on a partly-timed log keeps the old answer, which is merely
    coarse, instead of inventing a new one that is precise and wrong.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if not any(r.at is not None for r in rows):
        return None
    paired: list[tuple[Assignment, survival.Observation]] = []
    for row in rows:
        if row.at is None:
            return None
        reported = row.succeeded is not None
        if reported and row.resolved_at is None:
            return None
        end = row.resolved_at if reported else now
        # Clamped at zero: `now` is the reader's clock and the timestamps are
        # the database's, so a little skew between them is ordinary and must
        # not surface as a negative time-to-event.
        duration = max(0.0, (end - row.at).total_seconds())
        paired.append((row, survival.Observation(duration=duration, event=reported)))
    return paired


def _mature_horizon(paired: list[tuple[Assignment, survival.Observation]]) -> float | None:
    """How old an occasion must be before its silence means anything.

    The time by which MATURITY_QUANTILE of outcomes had arrived -- computed per
    arm, and the SLOWER arm's answer wins.

    Taking the max rather than pooling is the whole difficulty of this check in
    one line. A pooled horizon is dominated by whichever arm reports faster, so
    a run where the treated arm concludes in minutes and the control takes
    hours would judge the control's occasions against the treated arm's clock
    and call every one of them missing -- reintroducing, one level down, the
    exact false positive the maturity rule exists to remove. Each arm is
    therefore given the follow-up its own reporting behaviour says it needs.

    An arm whose curve never reaches the quantile contributes nothing instead
    of raising the horizon to infinity. That case is the signature worth
    keeping: an arm that is merely SLOW still reports eventually and so still
    has a quantile, while an arm that is genuinely LOSING outcomes never gets
    there. Letting it set the horizon would let a broken arm excuse its own
    missingness forever.

    `None` when no arm reaches the quantile, and the caller falls back to the
    untimed comparison -- which, on a run where almost nothing is ever
    reported, is exactly the blunt answer that is called for.
    """
    horizons: list[float] = []
    for injected in (True, False):
        arm = [obs for row, obs in paired if row.injected is injected]
        if not arm:
            continue
        reached = survival.time_to_reported_fraction(
            survival.kaplan_meier(arm), MATURITY_QUANTILE
        )
        if reached is not None:
            horizons.append(reached)
    return max(horizons) if horizons else None


def check_censoring_hazard(
    rows: list[Assignment], now: datetime.datetime | None = None
) -> Finding:
    """Are the two arms reporting on the same schedule -- and is it too early?

    Distinct from attrition, and the distinction is the point. Attrition asks
    whether one arm ends up permanently less observed than the other, which
    biases the estimate. This asks whether one arm is merely reporting SOONER,
    which does not -- everything still arrives, just not yet.

    That is not a defect; it is arguably the treatment working. A lesson that
    helps closes work faster, so its arm's outcomes land first. The hazard only
    matters for WHEN the effect can be read: while a materially large pending
    population remains and the arms are draining it at different rates, the
    sample the estimate is computed over is not yet equally observed, and the
    number moves as the laggards land. So this reports WEAKENS -- "wait" -- and
    never INVALIDATES. Calling a working lesson biased because it was fast is
    the exact failure this check exists to stop.
    """
    paired = _follow_up(rows, now)
    if paired is None:
        return Finding(
            "censoring_hazard", SEVERITY_OK,
            "Reporting schedule not checkable.",
            "This log does not carry an assignment time and a resolution time for "
            "every row, so how long each occasion was watched is unknown. The "
            "attrition check still reads the totals; only the timing does not.",
            {},
        )
    inj = [obs for row, obs in paired if row.injected]
    wit = [obs for row, obs in paired if not row.injected]
    pending = sum(1 for _, obs in paired if not obs.event)
    z, p = survival.log_rank_test(inj, wit)
    numbers = {
        "injected": len(inj), "withheld": len(wit),
        "pending": pending, "z": z, "p": p,
    }
    if not inj or not wit:
        return Finding(
            "censoring_hazard", SEVERITY_OK,
            "Reporting schedule not comparable yet.",
            "One arm has no assignments, so there are no two schedules to compare.",
            numbers,
        )

    inj_curve = survival.kaplan_meier(inj)
    wit_curve = survival.kaplan_meier(wit)
    # Median reporting time per arm, where each arm reaches one half reported.
    # Absent when an arm never gets there, which the text handles rather than
    # printing "None seconds".
    med_inj = survival.time_to_reported_fraction(inj_curve, 0.5)
    med_wit = survival.time_to_reported_fraction(wit_curve, 0.5)
    numbers |= {"median_seconds_injected": med_inj, "median_seconds_withheld": med_wit}

    if p >= CENSORING_ALPHA:
        return Finding(
            "censoring_hazard", SEVERITY_OK,
            f"Both arms report on the same schedule (log-rank p={p:.3g}).",
            "Outcomes arrive at the same rate in each arm, so reading the effect "
            "now is not reading it mid-drain.",
            numbers,
        )

    faster, slower = ("injected", "withheld") if z > 0 else ("withheld", "injected")
    pending_share = pending / len(paired) if paired else 0.0
    if pending_share < 1.0 - MATURITY_QUANTILE:
        return Finding(
            "censoring_hazard", SEVERITY_OK,
            f"The {faster} arm reported faster than the {slower} arm "
            f"(log-rank p={p:.3g}), but the run has since caught up.",
            f"{_pct(pending_share)} of occasions are still waiting, so the speed "
            "difference no longer decides which occasions the estimate can see. "
            "Worth knowing on its own: an arm that concludes work sooner is what "
            "a lesson that helps looks like before the outcomes are counted.",
            numbers,
        )
    return Finding(
        "censoring_hazard", SEVERITY_WEAKENS,
        f"The {faster} arm is reporting faster than the {slower} arm "
        f"(log-rank p={p:.3g}) and {_pct(pending_share)} of occasions are still "
        "waiting.",
        f"This is a statement about timing, NOT about bias: the {slower} arm's "
        "occasions are pending, not lost, and the attrition check below only "
        "counts an occasion against an arm once it is old enough to have "
        "reported. But the effect read right now is computed over a sample the "
        "two arms have drained to different depths, and it will move as the "
        "laggards land. Re-read it once the pending share falls, or widen the "
        "window the outcomes are collected over.",
        numbers,
    )


def check_differential_attrition(
    rows: list[Assignment], now: datetime.datetime | None = None
) -> Finding:
    """Are the two arms equally likely to have an outcome recorded?

    THE load-bearing check. Dropping unresolved occasions is unbiased only
    when both arms drop at the same rate; when they do not, the surviving
    sample is selected on something downstream of the treatment, and the
    difference between arms stops being the treatment's effect.

    The direction matters and is reported: the withheld arm is the one
    working without its memory, so it is the arm more likely to run long or
    be abandoned before anyone records how it went. When the withheld arm
    is the one losing data, the occasions that survive in it are the easier
    ones -- which flatters the control and UNDERSTATES the lesson. When the
    injected arm loses more, the effect is overstated. Either way the number
    is not the causal effect.

    An occasion counts here only once it is old enough for its silence to mean
    something: either it has already reported, or it has been waiting at least
    as long as `_mature_horizon` -- the time by which most of this run's own
    outcomes had arrived. Anything younger is pending, not missing.

    Without that rule this check had a failure mode that punished the product
    for working. Outcomes are reported some time after the arm is assigned, and
    a lesson that helps concludes its occasions SOONER; so at any moment before
    the run has run its course, the injected arm has more outcomes on the books
    purely because it got there first. The terminal comparison read that head
    start as differential attrition and returned INVALIDATES, suppressing the
    effect estimate exactly when the lesson was working, and the better the
    lesson the faster it was disqualified. The speed difference is still
    reported -- by `check_censoring_hazard`, which calls it what it is.

    On a log without timestamps the maturity rule cannot run and every row is
    judged, which is the behaviour this check has always had.
    """
    judged = rows
    immature = 0
    horizon: float | None = None
    paired = _follow_up(rows, now)
    if paired is not None:
        horizon = _mature_horizon(paired)
        if horizon is not None:
            # Reported occasions always count -- they are evidence however
            # young. Silent ones count only once they are past the horizon.
            mature = [row for row, obs in paired if obs.event or obs.duration >= horizon]
            immature = len(rows) - len(mature)
            judged = mature

    inj = [r for r in judged if r.injected]
    wit = [r for r in judged if not r.injected]
    s_inj, n_inj = sum(r.succeeded is not None for r in inj), len(inj)
    s_wit, n_wit = sum(r.succeeded is not None for r in wit), len(wit)
    numbers = {
        "injected_resolved": s_inj, "injected_total": n_inj,
        "withheld_resolved": s_wit, "withheld_total": n_wit,
        "pending_too_young": immature,
        "maturity_horizon_seconds": horizon,
    }
    if not n_inj or not n_wit:
        return Finding(
            "differential_attrition", SEVERITY_OK,
            "Attrition not checkable yet.",
            "One arm has no assignments, so there is nothing to compare "
            "outcome-recording rates between.",
            numbers,
        )

    r_inj, r_wit = s_inj / n_inj, s_wit / n_wit
    _, p = experiment.two_proportion_test(s_inj, n_inj, s_wit, n_wit)
    numbers |= {"injected_rate": r_inj, "withheld_rate": r_wit, "p": p,
                "gap": r_inj - r_wit}

    if p < VALIDITY_ALPHA:
        worse, better = ("withheld", "injected") if r_wit < r_inj else ("injected", "withheld")
        # Which arm loses data determines which way the estimate is pushed,
        # and a reader deciding what to do next needs the direction, not just
        # the fact.
        skew = (
            "The surviving withheld occasions are therefore the ones that "
            "concluded cleanly enough to be recorded, which flatters the control "
            "arm and UNDERSTATES the lesson's effect."
            if worse == "withheld" else
            "The surviving injected occasions are the ones that concluded cleanly "
            "enough to be recorded, which flatters the treated arm and OVERSTATES "
            "the lesson's effect."
        )
        return Finding(
            "differential_attrition", SEVERITY_INVALIDATES,
            f"The arms are not equally observed: {_pct(r_inj)} of injected occasions "
            f"have an outcome against {_pct(r_wit)} of withheld ones (p={p:.3g}).",
            f"Occasions are dropped when nothing reported how they went, and the "
            f"{worse} arm is losing them faster than the {better} arm. {skew} "
            "Fix the reporting gap -- capture an outcome for every occasion you "
            "retrieved against, including the ones that were abandoned or escalated "
            "-- and re-read this. Nothing here can correct it after the fact.",
            numbers,
        )

    missing = 1.0 - ((s_inj + s_wit) / (n_inj + n_wit))
    if missing >= ATTRITION_WEAKENS_AT:
        return Finding(
            "differential_attrition", SEVERITY_WEAKENS,
            f"{_pct(missing)} of assignments never got an outcome, though both arms "
            f"lose them at a similar rate (p={p:.3g}).",
            "Losing them evenly does not bias the estimate, it shrinks it: the "
            "confidence interval is computed on what survived, so the run is "
            "weaker than its own interval suggests. Capture an outcome for every "
            "occasion and the same number of retrievals will answer far more.",
            numbers,
        )
    return Finding(
        "differential_attrition", SEVERITY_OK,
        f"Both arms are observed at a similar rate ({_pct(r_inj)} injected, "
        f"{_pct(r_wit)} withheld; p={p:.3g}).",
        "Dropping unresolved occasions is unbiased when the two arms lose them "
        "equally, which is what this measures.",
        numbers,
    )


def check_arm_balance(rows: list[Assignment]) -> Finding:
    """Did roughly `rate` of assignments actually land in the control arm?

    Assignment is a deterministic hash, so a realized fraction far from the
    configured one is not bad luck -- it means the thing doing the assigning
    is not the thing the analysis thinks it is. Cheap, and it catches a
    broken client before anyone builds a claim on its output.
    """
    if not rows:
        return Finding("arm_balance", SEVERITY_OK, "No assignments yet.", "", {})
    # Row-weighted mean: the MLE of the assignment probability. Averaging
    # distinct rates instead (e.g. 90 rows @0.1 + 10 @0.5 -> 0.3 instead of
    # 0.14) tests against a rate that matches neither run and mis-reports.
    configured = sum(r.rate for r in rows) / len(rows)
    n = len(rows)
    k = sum(1 for r in rows if not r.injected)
    observed = k / n
    numbers = {"withheld": k, "total": n, "observed_rate": observed,
               "configured_rate": configured}

    # Below this the normal approximation is not worth trusting. (It is no
    # longer the main defence against false positives -- ARM_BALANCE_ALPHA
    # is -- but a tail probability computed from a bad approximation is not
    # worth acting on at either alpha.)
    if n < 30:
        return Finding(
            "arm_balance", SEVERITY_OK,
            f"{k} of {n} assignments withheld ({_pct(observed)} against a configured "
            f"{_pct(configured)}); too few to test yet.",
            "Checked once there are 30 assignments.", numbers,
        )

    p = _binomial_tail_p(k, n, configured)
    numbers["p"] = p
    if p < ARM_BALANCE_ALPHA:
        return Finding(
            "arm_balance", SEVERITY_INVALIDATES,
            f"Withheld share is {_pct(observed)} where {_pct(configured)} was configured "
            f"(p={p:.3g}).",
            "Assignment is a deterministic hash of (lesson, occasion, salt), so this "
            "is not sampling noise -- something other than that hash decided these "
            "arms. Check that every retriever is using the same holdout rate and "
            "salt, and that nothing is filtering assignments before they are logged.",
            numbers,
        )
    return Finding(
        "arm_balance", SEVERITY_OK,
        f"Withheld share is {_pct(observed)}, consistent with the configured "
        f"{_pct(configured)} (p={p:.3g}).",
        "", numbers,
    )


def check_assignment_drift(rows: list[Assignment]) -> Finding:
    """Was the randomization reshuffled part-way through?

    Assignment is a hash of (lesson, occasion, SALT) compared against RATE.
    Change either and every occasion is re-randomized, so the log stops being
    one experiment and becomes two overlapping ones pooled into a single
    comparison. Pooling them is not a smaller experiment, it is a broken one:
    an occasion can appear in both arms.
    """
    salts = sorted({r.salt for r in rows})
    rates = sorted({round(r.rate, 6) for r in rows})
    numbers = {"salts": salts, "rates": rates}
    if len(salts) <= 1 and len(rates) <= 1:
        return Finding(
            "assignment_drift", SEVERITY_OK,
            "One randomization throughout.",
            "", numbers,
        )
    changed = []
    if len(salts) > 1:
        changed.append(f"salt ({', '.join(repr(s) for s in salts)})")
    if len(rates) > 1:
        changed.append(f"rate ({', '.join(_pct(r) for r in rates)})")
    return Finding(
        "assignment_drift", SEVERITY_INVALIDATES,
        f"The randomization changed mid-run: {' and '.join(changed)}.",
        "Every occasion is re-randomized when the salt or rate changes, so these "
        "assignments are two different experiments pooled into one comparison -- "
        "and the same occasion can sit in opposite arms in each. Analyse one "
        "randomization at a time, or start a fresh run and let this one end.",
        numbers,
    )


# A lesson is "marginally eligible" on an occasion when it barely cleared the
# retrieval floor. The band is absolute rather than a percentile because the
# relevance scale itself is absolute (commontrace/retrieval.py) -- a
# percentile would move with the store's own distribution and stop meaning
# the same thing between two fleets.
MARGINAL_BAND = 0.10
_MARGINAL_WEAKENS = 0.40
_MARGINAL_INVALIDATES = 0.70

# How far a lesson's assignment count may exceed the median before it looks
# like it is absorbing occasions that are not about it.
_CONCENTRATION_MULTIPLE = 3.0


def check_marginal_eligibility(rows: list[Assignment]) -> Finding:
    """How much of the evidence comes from lessons that barely matched.

    THE FAILURE THIS EXISTS FOR. Retrieval returns top-k, and every retrieved
    lesson is logged as eligible on that occasion -- so a lesson that scraped
    in on one incidental word gets an assignment row identical to one that was
    squarely on topic, and the occasion's outcome is attributed to both. The
    outcome had nothing to do with the marginal one, so those rows are noise
    with a sign: they pull the estimate toward the store's base rate, and with
    enough of them a lesson that does nothing acquires a significant verdict.

    Observed in practice: in a six-lesson store, one lesson accumulated 246
    assignments against roughly 80 occasions actually about it, and was
    reported as significantly HURTING outcomes (-14.5pp, p=0.018) after
    Benjamini-Hochberg. Nothing in the log could show why, because the log
    recorded that the lesson was eligible and not how weakly.

    Rows with no recorded relevance are skipped, not assumed: an assignment
    logged before the evidence was recorded cannot be assessed, and reporting
    it as clean would be as wrong as reporting it as marginal.
    """
    scored = [r for r in rows if r.relevance is not None and r.floor is not None]
    numbers: dict = {"n_scored": len(scored), "n_unscored": len(rows) - len(scored)}
    if not scored:
        return Finding(
            "marginal_eligibility", SEVERITY_OK,
            "No retrieval scores recorded, so eligibility strength cannot be assessed.",
            "Assignments logged before retrieval evidence was recorded carry no "
            "relevance score. Newer assignments will carry one; this check reports "
            "on those.",
            numbers,
        )

    by_lesson: dict[str, list[Assignment]] = {}
    for r in scored:
        by_lesson.setdefault(r.lesson, []).append(r)

    worst_slug = ""
    worst_share = 0.0
    shares: dict[str, float] = {}
    for slug, group in sorted(by_lesson.items()):
        marginal = sum(
            1 for r in group
            if r.relevance is not None and r.floor is not None
            and r.relevance < r.floor + MARGINAL_BAND
        )
        share = marginal / len(group)
        shares[slug] = round(share, 4)
        if share > worst_share:
            worst_slug, worst_share = slug, share
    numbers["marginal_share_by_lesson"] = shares
    numbers["worst"] = {"lesson": worst_slug, "share": round(worst_share, 4)}

    if worst_share >= _MARGINAL_INVALIDATES:
        severity = SEVERITY_INVALIDATES
        detail = (
            f"Most of {worst_slug!r}'s evidence comes from occasions it barely matched, "
            "so its effect is mostly measured on tasks it was never about. That is a "
            "bias in the estimate, not a shortage of data -- more occasions of the same "
            "kind make it worse, not better. Raise the retrieval floor "
            "(`commontrace retrieval --floor`) and start a fresh randomization before "
            "quoting an effect for it."
        )
    elif worst_share >= _MARGINAL_WEAKENS:
        severity = SEVERITY_WEAKENS
        detail = (
            f"A large minority of {worst_slug!r}'s assignments only just cleared the "
            "retrieval floor. Its effect is diluted toward the store's base rate by "
            "occasions it was not really about. Compare against the top-relevance "
            "band before drawing a conclusion."
        )
    else:
        return Finding(
            "marginal_eligibility", SEVERITY_OK,
            "Assignments come from lessons that matched their occasions solidly.",
            "", numbers,
        )

    return Finding(
        "marginal_eligibility", severity,
        f"{worst_share:.0%} of {worst_slug!r}'s assignments only just cleared the "
        "retrieval floor.",
        detail, numbers,
    )


def check_assignment_concentration(rows: list[Assignment]) -> Finding:
    """Is one lesson being logged far more often than the rest, and worse?

    A lesson eligible on several times more occasions than its peers is either
    genuinely broad or matching things it should not. The two are told apart
    by relevance: a broad lesson matches its many occasions as strongly as
    other lessons match theirs, while an over-matching one is both more
    frequent AND weaker. Only the second is a problem, and only the second is
    reported here.
    """
    if not rows:
        return Finding("assignment_concentration", SEVERITY_OK, "No assignments.", "", {})

    by_lesson: dict[str, list[Assignment]] = {}
    for r in rows:
        by_lesson.setdefault(r.lesson, []).append(r)
    if len(by_lesson) < 3:
        return Finding(
            "assignment_concentration", SEVERITY_OK,
            "Too few lessons under test to compare assignment counts.",
            "", {"n_lessons": len(by_lesson)},
        )

    counts = {slug: len(group) for slug, group in by_lesson.items()}
    median_count = statistics.median(counts.values())
    medians = {
        slug: statistics.median([r.relevance for r in group if r.relevance is not None])
        for slug, group in by_lesson.items()
        if any(r.relevance is not None for r in group)
    }
    overall_median_rel = statistics.median(medians.values()) if medians else None

    flagged = []
    for slug, count in sorted(counts.items()):
        if median_count <= 0 or count < _CONCENTRATION_MULTIPLE * median_count:
            continue
        rel = medians.get(slug)
        if rel is None or overall_median_rel is None or rel >= overall_median_rel:
            # Broad, but matching as strongly as its peers. Not a defect.
            continue
        flagged.append((slug, count, rel))

    numbers = {
        "counts": counts,
        "median_count": median_count,
        "median_relevance_by_lesson": {k: round(v, 4) for k, v in medians.items()},
        "flagged": [f[0] for f in flagged],
    }
    if not flagged:
        return Finding(
            "assignment_concentration", SEVERITY_OK,
            "No lesson is absorbing a disproportionate share of occasions.",
            "", numbers,
        )

    slug, count, rel = flagged[0]
    return Finding(
        "assignment_concentration", SEVERITY_WEAKENS,
        f"{slug!r} was eligible on {count} occasions against a median of "
        f"{median_count:.0f}, and matched them less strongly than other lessons match theirs.",
        "A lesson that is both far more frequent and weaker than its peers is being "
        "retrieved into occasions it is not about, and those occasions' outcomes are "
        "attributed to it. Its effect estimate describes a mixture of the tasks it "
        "addresses and the tasks it merely matched.",
        numbers,
    )


def check_scorer_drift(rows: list[Assignment]) -> Finding:
    """Did retrieval change what counts as eligible, mid-experiment?

    The same reasoning as check_assignment_drift, one level up. That check
    catches a changed randomization; this catches a changed DENOMINATOR. The
    scorer and the floor together decide which lessons get an assignment at
    all, so changing either mid-run means the rows before and after describe
    two different treatments -- "injected when retrieved" is not one treatment
    if what counts as retrieved moved.
    """
    scorers = sorted({r.scorer for r in rows if r.scorer})
    floors = sorted({round(r.floor, 6) for r in rows if r.floor is not None})
    numbers = {"scorers": scorers, "floors": floors}
    if len(scorers) <= 1 and len(floors) <= 1:
        return Finding(
            "scorer_drift", SEVERITY_OK,
            "One retrieval configuration throughout.",
            "", numbers,
        )
    changed = []
    if len(scorers) > 1:
        changed.append(f"scorer ({', '.join(repr(s) for s in scorers)})")
    if len(floors) > 1:
        changed.append(f"floor ({', '.join(str(f) for f in floors)})")
    return Finding(
        "scorer_drift", SEVERITY_INVALIDATES,
        f"Retrieval changed mid-run: {' and '.join(changed)}.",
        "The scorer and floor decide which lessons are eligible on an occasion, so "
        "these assignments describe two different treatments pooled into one "
        "comparison. Analyse one configuration at a time, or start a fresh "
        "randomization (`commontrace experiment --configure --rate <rate>`) so the "
        "new settings get their own run.",
        numbers,
    )


def check_inconsistent_arms(rows: list[Assignment], unit: str = UNIT_LESSON) -> Finding:
    """Was any (lesson, occasion) recorded in BOTH arms?

    The unit of assignment is the pair, and assignment is deterministic, so
    this cannot happen within one randomization. When it does, that occasion
    contributes to the treated and control rate simultaneously -- it is
    evidence for and against the same lesson.
    """
    arms: dict[tuple[str, str], set[bool]] = {}
    for r in rows:
        arms.setdefault((r.lesson, r.occasion_id), set()).add(r.injected)
    conflicted = sorted(k for k, v in arms.items() if len(v) > 1)
    numbers = {"conflicted": len(conflicted),
               "examples": [f"{item} @ {occ}" for item, occ in conflicted[:5]]}
    if not conflicted:
        return Finding(
            "consistent_arms", SEVERITY_OK,
            f"Every ({unit}, occasion) sits in exactly one arm.", "", numbers,
        )
    return Finding(
        "consistent_arms", SEVERITY_INVALIDATES,
        f"{len(conflicted)} ({unit}, occasion) pair(s) appear in BOTH arms.",
        "Assignment is deterministic, so within one randomization this is "
        "impossible -- it means the salt or rate changed (see the drift check) or "
        "something is writing the log that is not the assigner. Those occasions "
        f"count as evidence for and against the same {unit} at once.",
        numbers,
    )


def check_treatment_stability(rows: list[Assignment], unit: str = UNIT_LESSON) -> Finding:
    """Did the lesson being measured stay the same lesson?

    `check_assignment_drift` catches the randomization changing mid-run. This
    catches the thing being randomized changing mid-run, which is the same
    defect one level down and is the easier of the two to cause: a lesson is
    a file, and `lesson edit`, an MCP `draft_lesson` call, and a text editor
    all rewrite it in place.

    Edit a lesson on day 10 of a 30-day run and the occasions before and
    after were treated with different instructions. `analyze()` pools them
    into one arm and reports a single effect -- for a treatment that is an
    average of two, one of which no longer exists anywhere. The estimate is
    not wrong about a lesson; there is no longer one lesson for it to be
    about.

    A run with no recorded revisions is reported as unchecked rather than
    clean. Silence would let an old log -- or a client that never recorded
    them -- read as a stable treatment, which is precisely the state this
    exists to distinguish from one.
    """
    # First-seen order, not sorted. The log is chronological, so this is the
    # order the lesson actually moved through -- and the finding renders it
    # with an arrow. Sorting alphabetically produced an arrow pointing the
    # wrong way, which reads as a sequence and cross-references against
    # `commontrace lesson history` incorrectly.
    by_lesson: dict[str, list[str]] = {}
    unknown = 0
    for r in rows:
        if r.revision is None:
            unknown += 1
            continue
        seen = by_lesson.setdefault(r.lesson, [])
        if r.revision not in seen:
            seen.append(r.revision)

    if not rows:
        return Finding("treatment_stability", SEVERITY_OK, "No assignments yet.", "",
                       {"lessons_tracked": 0})

    changed = {slug: revs for slug, revs in by_lesson.items() if len(revs) > 1}
    numbers = {
        "lessons_tracked": len(by_lesson),
        "assignments_without_a_revision": unknown,
        "changed": {slug: list(revs) for slug, revs in sorted(changed.items())},
    }

    if changed:
        named = "; ".join(
            f"`{slug}` ({' -> '.join(revs)})" for slug, revs in sorted(changed.items())
        )
        how_to_inspect = (
            " `commontrace lesson history <slug>` shows what changed and when."
            if unit == UNIT_LESSON else ""
        )
        return Finding(
            "treatment_stability", SEVERITY_INVALIDATES,
            f"{len(changed)} {unit}(s) were edited while the experiment was running: {named}.",
            "Occasions before and after the edit were treated with different "
            "instructions, and both arms pool them into one comparison -- so the "
            f"effect reported for such a {unit} is an average over a treatment that "
            f"no longer exists.{how_to_inspect} To measure the current text, start a "
            "fresh randomization (change the salt) and let this one end.",
            numbers,
        )
    if not by_lesson:
        return Finding(
            "treatment_stability", SEVERITY_WEAKENS,
            f"No assignment recorded which revision of a {unit} it used, so whether "
            "the treatment held still cannot be checked.",
            "Assignments written before revisions were recorded do not carry one. "
            "The estimate may be fine; nothing here can say so. Assignments made "
            "from now on carry the revision, and this check answers on the next run.",
            numbers,
        )
    if unknown:
        return Finding(
            "treatment_stability", SEVERITY_OK,
            f"No {unit} changed while the experiment ran ({unknown} older "
            "assignment(s) carry no revision and were not checked).",
            "", numbers,
        )
    return Finding(
        "treatment_stability", SEVERITY_OK,
        f"All {len(by_lesson)} {unit}(s) held the same text throughout.",
        "", numbers,
    )


def check_outcome_variation(rows: list[Assignment]) -> Finding:
    """Is there any variation in the outcome at all?

    An all-succeeded or all-failed corpus produces a difference of exactly
    zero with a tidy interval around it, and it reads as a confident null.
    It is not a null; it is an outcome field that is not being filled in
    honestly, or a success criterion that nothing can fail.
    """
    resolved = _resolved(rows)
    n = len(resolved)
    wins = sum(1 for r in resolved if r.succeeded)
    numbers = {"resolved": n, "succeeded": wins}
    if n < 20:
        return Finding("outcome_variation", SEVERITY_OK,
                       f"{n} recorded outcome(s); too few to judge.", "", numbers)
    if wins in (0, n):
        state = "succeeded" if wins else "failed"
        return Finding(
            "outcome_variation", SEVERITY_INVALIDATES,
            f"All {n} recorded occasions {state}.",
            "With no variation in the outcome there is nothing for a lesson to move, "
            "and the resulting difference of zero will read as a confident null. "
            "Either the outcome is not being recorded honestly, or the success "
            "criterion is one nothing can fail -- pick a measure that discriminates "
            "before running this again.",
            numbers,
        )
    return Finding("outcome_variation", SEVERITY_OK,
                   f"{wins} of {n} recorded occasions succeeded.", "", numbers)


# --- power projection ----------------------------------------------------


def project(rows: list[Assignment], min_arm: int = experiment.DEFAULT_MIN_ARM) -> list[Projection]:
    """Per lesson: how far from an answer, and when at the current rate.

    `experiment` already says a lesson is underpowered and how many
    observations each arm needs. What it cannot say is WHEN -- and the
    difference decides whether a pilot is on track or already lost. A team
    told on day 30 that their run was underpowered has spent the pilot; the
    same team told on day 3 that the control arm lands in 94 days can raise
    the holdout rate that afternoon and still finish.

    The control arm is almost always the binding one, and the reason is
    arithmetic rather than bad luck: at a 10% holdout it takes ~10x
    `min_arm` occasions to put `min_arm` in the control, so a run reaches
    power roughly ten times slower than its raw occasion count suggests.
    That is the single most useful thing this can tell someone, so when the
    control arm binds, the advice names the rate that would fix it.
    """
    by_lesson: dict[str, list[Assignment]] = {}
    for r in _resolved(rows):
        by_lesson.setdefault(r.lesson, []).append(r)

    out: list[Projection] = []
    for lesson, rs in sorted(by_lesson.items()):
        n_inj = sum(1 for r in rs if r.injected)
        n_wit = len(rs) - n_inj
        binding, have = ("withheld", n_wit) if n_wit <= n_inj else ("injected", n_inj)
        still = max(0, min_arm - have)

        stamps = sorted(r.at for r in rs if r.at is not None)
        per_day = days = None
        eta = None
        if still and len(stamps) >= 2:
            span_days = (stamps[-1] - stamps[0]).total_seconds() / 86400.0
            arm_rows = [r for r in rs if (r.injected != (binding == "withheld"))]
            if span_days > 0 and arm_rows:
                per_day = len(arm_rows) / span_days
                if per_day > 0:
                    days = still / per_day
                    # Capped so an accrual rate of one row a fortnight does
                    # not produce a date in the next century and read as a
                    # plan. Past this, the answer is not a date.
                    if days <= 3650:
                        eta = (stamps[-1] + datetime.timedelta(days=days)).date()

        if not still:
            advice = "Powered. This lesson has enough in both arms to be answered."
        elif binding == "withheld":
            rate = sum(r.rate for r in rs) / len(rs)
            # n needed at the current rate vs at a rate that balances the arms.
            at_current = int(min_arm / rate) if rate > 0 else 0
            advice = (
                f"The control arm binds: at a {_pct(rate)} holdout it takes about "
                f"{at_current} occasions to put {min_arm} in it. Raising the holdout "
                f"rate toward 50% reaches an answer in roughly {min_arm * 2} occasions "
                "instead -- the cost is that more work runs without its memory while "
                "the experiment is live, which is the trade a shorter pilot is buying."
            )
        else:
            advice = (
                f"The treated arm binds, which is unusual -- it normally means this "
                f"lesson matched few occasions. It needs {still} more."
            )
        out.append(Projection(
            lesson=lesson, n_injected=n_inj, n_withheld=n_wit, needed_per_arm=min_arm,
            binding_arm=binding, still_needed=still, per_day=per_day,
            days_remaining=days, eta=eta, advice=advice,
        ))
    return out


# --- the report ----------------------------------------------------------


def audit(
    rows: list[Assignment],
    min_arm: int = experiment.DEFAULT_MIN_ARM,
    unit: str = UNIT_LESSON,
    now: datetime.datetime | None = None,
) -> IntegrityReport:
    """Every check, plus the projection, over one experiment's assignments.

    Takes the RAW log -- duplicates, unresolved occasions and all. Most of
    what this looks for is precisely what the estimate drops, so a caller
    that pre-filtered would hand over a record with the evidence already
    removed.
    """
    # Conflict detection and drift read the raw rows (a conflict IS a pair
    # logged twice, differently); everything else reads one row per pair, the
    # same unit the estimate is computed on.
    conflicts = check_inconsistent_arms(rows, unit)
    drift = check_assignment_drift(rows)
    # Read the raw rows for the same reason drift does: a configuration
    # change is visible across every line written under each setting, and
    # normalizing first would hide a change that happened within one pair's
    # retries.
    scorer_drift = check_scorer_drift(rows)
    unique, duplicates = normalize(rows)
    findings = [
        check_differential_attrition(unique, now=now),
        # Immediately after attrition, and deliberately: the two are read
        # together. Attrition says whether an arm is permanently less observed;
        # this says whether it is merely behind. `now` is threaded from the
        # caller so a test can read the log at a fixed instant instead of
        # against a wall clock that moves while it runs.
        check_censoring_hazard(unique, now=now),
        check_arm_balance(unique),
        drift,
        scorer_drift,
        conflicts,
        check_treatment_stability(unique, unit),
        check_outcome_variation(unique),
        # Both read the de-duplicated rows: these are about which occasions
        # the estimate is computed over, so they must count assignments the
        # same way the estimate does.
        check_marginal_eligibility(unique),
        check_assignment_concentration(unique),
    ]
    worst = max((_SEVERITY_RANK[f.severity] for f in findings), default=0)
    return IntegrityReport(
        verdict=_VERDICT_FOR[worst],
        findings=findings,
        projections=project(unique, min_arm=min_arm),
        unit=unit,
        n_assignments=len(unique),
        n_resolved=len(_resolved(unique)),
        n_duplicates=duplicates,
    )


_VERDICT_LINE = {
    VERDICT_SOUND: "**Sound.** Nothing in the assignment record undermines the "
                   "comparison below.",
    VERDICT_WEAKENED: "**Weakened.** The comparison below is still an estimate, but "
                      "less sits behind it than its confidence interval implies.",
    VERDICT_COMPROMISED: "**Compromised. Do not quote the effect sizes below.** A "
                         "specific mechanism is biasing them, named beneath. This is "
                         "not a power problem and more data will not fix it.",
}


def render(report: IntegrityReport) -> str:
    """The validity section, written to be read BEFORE the effects.

    Order is deliberate. A report that leads with a significant effect and
    mentions the caveat underneath is how a broken number gets quoted: the
    headline travels and the caveat does not. If the sample cannot support
    the estimate, that is the first thing on the page.
    """
    lines = ["## Can this be trusted?", "", _VERDICT_LINE[report.verdict], ""]
    lines.append(
        f"{report.n_resolved} of {report.n_assignments} assignment(s) have a recorded "
        "outcome. Only those enter the comparison."
        + (f" {report.n_duplicates} retry/retries were collapsed."
           if report.n_duplicates else "")
    )
    lines.append("")
    for f in report.findings:
        mark = {SEVERITY_OK: "OK", SEVERITY_WEAKENS: "WEAKENS",
                SEVERITY_INVALIDATES: "INVALIDATES"}[f.severity]
        lines.append(f"- **[{mark}] {f.headline}**")
        if f.detail:
            lines.append(f"  {f.detail}")
    lines.append("")
    lines.append(
        "_Checked here: attrition, arm balance, mid-run re-randomization, "
        f"conflicting arms, whether the {report.unit} text held still, and whether the "
        "outcome varies at all. NOT checkable "
        f"here: whether an agent used a {report.unit} it was told to withhold. That leaves "
        "no trace in the record and biases the effect toward zero -- it is honoured "
        "by the client or not at all._"
    )

    pending = [p for p in report.projections if p.still_needed]
    if pending:
        lines += ["", "## When will this be answerable?", ""]
        for p in pending:
            when = ""
            if p.eta is not None:
                when = (f" At the current rate ({p.per_day:.2f}/day into that arm) "
                        f"that is about {p.days_remaining:.0f} more days -- around {p.eta}.")
            elif p.per_day is None:
                when = (" No dated assignments yet, so there is no accrual rate to "
                        "project from.")
            else:
                when = " Nothing is currently accruing into that arm."
            lines.append(
                f"- `{p.lesson}` -- {p.n_injected} injected / {p.n_withheld} withheld; "
                f"needs {p.still_needed} more in the {p.binding_arm} arm.{when}"
            )
            lines.append(f"  {p.advice}")
    return "\n".join(lines)
