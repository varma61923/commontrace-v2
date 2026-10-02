"""Is the causal number trustworthy? -- validity checks over a holdout run."""

from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass, field

from commontrace import experiment, survival

VALIDITY_ALPHA = 0.10

ARM_BALANCE_ALPHA = 0.001

ATTRITION_WEAKENS_AT = 0.30

MATURITY_QUANTILE = 0.90

CENSORING_ALPHA = 0.05

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
    """One (lesson, occasion) arm decision, whether or not it was ever resolved."""

    lesson: str
    occasion_id: str
    injected: bool
    rate: float = experiment.DEFAULT_HOLDOUT_RATE
    salt: str = ""
    succeeded: bool | None = None
    at: datetime.datetime | None = None
    resolved_at: datetime.datetime | None = None
    revision: str | None = None
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
    """One row per (lesson, occasion); returns it with the retries collapsed."""
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


def _follow_up(
    rows: list[Assignment], now: datetime.datetime | None
) -> list[tuple[Assignment, survival.Observation]] | None:
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
        duration = max(0.0, (end - row.at).total_seconds())
        paired.append((row, survival.Observation(duration=duration, event=reported)))
    return paired


def _mature_horizon(paired: list[tuple[Assignment, survival.Observation]]) -> float | None:
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
    """Are the two arms reporting on the same schedule -- and is it too early?"""
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
    """Are the two arms equally likely to have an outcome recorded?"""
    judged = rows
    immature = 0
    horizon: float | None = None
    paired = _follow_up(rows, now)
    if paired is not None:
        horizon = _mature_horizon(paired)
        if horizon is not None:
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
    """Did roughly `rate` of assignments actually land in the control arm?"""
    if not rows:
        return Finding("arm_balance", SEVERITY_OK, "No assignments yet.", "", {})
    configured = sum(r.rate for r in rows) / len(rows)
    n = len(rows)
    k = sum(1 for r in rows if not r.injected)
    observed = k / n
    numbers = {"withheld": k, "total": n, "observed_rate": observed,
               "configured_rate": configured}

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
    """Was the randomization reshuffled part-way through?"""
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


MARGINAL_BAND = 0.10
_MARGINAL_WEAKENS = 0.40
_MARGINAL_INVALIDATES = 0.70

_FLOOR_GATED_SCORERS = frozenset({"idf-v3", "idf-v2", "count-v1"})


def _floor_decided(row: Assignment) -> bool:
    return row.scorer is None or row.scorer in _FLOOR_GATED_SCORERS

_CONCENTRATION_MULTIPLE = 3.0


def check_marginal_eligibility(rows: list[Assignment]) -> Finding:
    """How much of the evidence comes from lessons that barely matched."""
    recorded = [r for r in rows if r.relevance is not None and r.floor is not None]
    scored = [r for r in recorded if _floor_decided(r)]
    numbers: dict = {
        "n_scored": len(scored),
        "n_unscored": len(rows) - len(recorded),
        "n_not_floor_gated": len(recorded) - len(scored),
    }
    if not scored and recorded:
        return Finding(
            "marginal_eligibility", SEVERITY_OK,
            "Eligibility was decided by rank, not a relevance floor, so there is "
            "no floor to have barely cleared.",
            "These assignments come from fused or semantic retrieval, which admits "
            "the top-ranked lessons; the recorded floor gates only the lexical "
            "arm. Concentration and drift are still checked.",
            numbers,
        )
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
    """Is one lesson being logged far more often than the rest, and worse?"""
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
    """Did retrieval change what counts as eligible, mid-experiment?"""
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
    """Was any (lesson, occasion) recorded in BOTH arms?"""
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
    """Did the lesson being measured stay the same lesson?"""
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
    """Is there any variation in the outcome at all?"""
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


def project(rows: list[Assignment], min_arm: int = experiment.DEFAULT_MIN_ARM) -> list[Projection]:
    """Per lesson: how far from an answer, and when at the current rate."""
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
                    if days <= 3650:
                        eta = (stamps[-1] + datetime.timedelta(days=days)).date()

        if not still:
            advice = "Powered. This lesson has enough in both arms to be answered."
        elif binding == "withheld":
            rate = sum(r.rate for r in rs) / len(rs)
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


def audit(
    rows: list[Assignment],
    min_arm: int = experiment.DEFAULT_MIN_ARM,
    unit: str = UNIT_LESSON,
    now: datetime.datetime | None = None,
) -> IntegrityReport:
    """Every check, plus the projection, over one experiment's assignments."""
    conflicts = check_inconsistent_arms(rows, unit)
    drift = check_assignment_drift(rows)
    scorer_drift = check_scorer_drift(rows)
    unique, duplicates = normalize(rows)
    findings = [
        check_differential_attrition(unique, now=now),
        check_censoring_hazard(unique, now=now),
        check_arm_balance(unique),
        drift,
        scorer_drift,
        conflicts,
        check_treatment_stability(unique, unit),
        check_outcome_variation(unique),
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
    """The validity section, written to be read BEFORE the effects."""
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
