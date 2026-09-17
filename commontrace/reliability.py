"""Does this lesson actually work? — credit assignment and coherence.

THE PROBLEM THIS EXISTS FOR
---------------------------
CommonTrace's core claim is that raw memory remembers, while CommonTrace
generalizes. The first half is delivered: a `Lesson` is a generalized rule with an
explicit activation condition, not a stored episode. The second half has a
hole in it — **nothing ever checks whether the generalization was correct.**

Concretely, before this module the system had no way to represent, let alone
detect, any of:

  * a lesson that is simply **wrong**
  * a lesson whose `applies_when` is too **broad**, so it fires in
    situations it does not actually help
  * two `active` lessons that **contradict each other** and will be injected
    into the same decision

A `Lesson.status` is set by a human and never revisited. `Trace.trust` on
the Hub is an up/down vote count. That makes the corpus a *retrieval*
system: it returns what was written, and its quality is fixed at authoring
time and can only decay.

The distinction that matters: a retrieval system answers "what did we
write down about this?"; a learning system also answers "and was it
right?". Everything needed for the second question — which lessons were
injected (`lessons_retrieved_by_alpha`), which proved useful
(`lessons_hit`), and how the task turned out (`verdict`,
`Trace.outcome.resolved`, `Trace.outcome.repeated_error`) — is already
being recorded and was, until now, joined by nothing.

WHY IT MATTERS COMMERCIALLY (see STRATEGY.md)
---------------------------------------------
Human review does not scale past a few hundred lessons, and a single
fleet's own lesson corpus can accumulate contradictions just as easily as
a pooled one would: two decision points recorded months apart can encode
opposite rules that were each correct in their own unstated context.
Injecting contradictory guidance into a live decision is worse than
injecting none -- and the same risk applies to the CommonTrace Knowledge
Base, which the operator authors and curates but does not exempt from
needing to be internally consistent. Self-auditing is therefore a
precondition for a lesson corpus staying trustworthy at scale, not a
nicety.

WHAT THIS DOES NOT DO
---------------------
It never modifies a lesson. It produces evidence and recommendations; the
Validator gate (`commontrace lesson approve|reject`) stays human, exactly
as it is for `commontrace distill`. An automated system that silently
demoted lessons based on noisy small-sample statistics would be worse than
the problem it solves.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from commontrace.overlap import estimate_jaccard, minhash

# Below this many retrievals, no claim is made about a lesson at all. Small
# samples produce confident-looking nonsense, and these verdicts are meant
# to drive real decisions about what agents get told.
DEFAULT_MIN_EVIDENCE = 5

# A lesson is called reliable only if the *lower bound* of its precision
# interval clears this -- not the point estimate. See wilson_lower_bound.
DEFAULT_PRECISION_FLOOR = 0.50

# Activation-condition similarity above which two lessons are considered to
# fire in overlapping situations, and therefore capable of contradicting
# each other in a way an agent would actually experience.
DEFAULT_ACTIVATION_OVERLAP = 0.25

_Z_95 = 1.959963984540054

# Rule-text polarity markers. This is a heuristic and is labelled as one
# everywhere it surfaces: it catches "always X" vs "never X", and it will
# miss contradictions phrased without these words. It is a cheap first
# filter for human review, not a semantic entailment checker.
_PROHIBITIVE = re.compile(
    r"\b(never|don't|do not|avoid|must not|mustn't|should not|shouldn't|"
    r"cannot|can't|refuse|forbid|prohibit|no longer|stop)\b",
    re.IGNORECASE,
)
_IMPERATIVE = re.compile(
    r"\b(always|must|should|ensure|prefer|require|use|enable|apply)\b",
    re.IGNORECASE,
)


def wilson_lower_bound(successes: int, trials: int, z: float = _Z_95) -> float:
    """Lower bound of the Wilson score interval for a binomial proportion.

    Used instead of the raw ratio because the raw ratio is actively
    misleading at the sample sizes this system will actually see: a lesson
    that hit 1/1 has a point estimate of 100%, which would rank it above one
    that hit 45/50. Wilson gives the first ~0.05 and the second ~0.79,
    which is the correct ordering of *confidence that the lesson works*.

    Chosen over the normal approximation because that one is degenerate at
    the extremes (it returns an interval of zero width for 0/n and n/n,
    which are exactly the cases a young corpus is full of).
    """
    if trials <= 0:
        return 0.0
    p_hat = successes / trials
    denom = 1 + z**2 / trials
    center = (p_hat + z**2 / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / trials + z**2 / (4 * trials**2))
    return max(0.0, center - margin)


# --- Evidence ---------------------------------------------------------------


@dataclass
class Evidence:
    """One occasion on which a set of lessons was injected into a decision,
    and how that decision turned out.

    Deliberately agent-agnostic (protocol/PROTOCOL.md §6): an "occasion" is
    a code-review episode, a support ticket, a sales call. `hit` is the
    subset the producer judged actually useful; `succeeded` is the outcome
    of the underlying task.
    """

    occasion_id: str
    retrieved: list[str]
    hit: list[str]
    succeeded: bool | None  # None = outcome unknown; excluded from lift


@dataclass
class LessonReliability:
    slug: str
    n_retrieved: int
    n_hit: int
    precision: float           # raw hit rate -- shown, never ranked on
    precision_lower: float     # Wilson lower bound -- what verdicts use
    success_rate: float | None  # P(task succeeded | this lesson injected)
    lift: float | None         # that, minus the corpus-wide baseline
    verdict: str
    rationale: str


# Verdicts. Deliberately four, not "good/bad": the distinction between a
# rule that is wrong and a rule that merely fires too often has completely
# different remedies, and collapsing them would send people to rewrite
# correct rules.
VERDICT_RELIABLE = "RELIABLE"          # earns its place
VERDICT_UNPROVEN = "UNPROVEN"          # not enough evidence to say anything
VERDICT_MISCALIBRATED = "MISCALIBRATED"  # fires often, rarely useful -> tighten applies_when
VERDICT_HARMFUL = "HARMFUL"            # tasks go *worse* when it is injected -> rule may be wrong

# Verdict -> ranking adjustment, for commontrace/retrieval.py's optional
# `reliability_lookup` (see RetrievalConfig.reliability_weight). This is the
# one place a verdict becomes a number retrieval can use -- everywhere else
# in this module a verdict stays a verdict, on purpose (see `score_lessons`'s
# docstring on why four, not a score).
#
# MISCALIBRATED gets a real but SMALLER penalty than HARMFUL: a lesson that
# fires too often is an activation-condition problem (see its own rationale
# string above) and still helps on the occasions it is actually right, while
# a HARMFUL verdict means the rule itself may be wrong. Collapsing the two to
# the same penalty would rank a lesson that helps 20% of the time behind one
# that measurably makes tasks worse, by the same amount -- which is not what
# either verdict is trying to say.
_VERDICT_ADJUSTMENT: dict[str, float] = {
    VERDICT_HARMFUL: -1.0,
    VERDICT_MISCALIBRATED: -0.5,
    VERDICT_UNPROVEN: 0.0,
    VERDICT_RELIABLE: 1.0,
}


def ranking_adjustments(scores: list["LessonReliability"]) -> dict[str, float]:
    """slug -> adjustment in [-1, 1], for `retrieval.rank_lessons`'s optional
    `reliability_lookup`.

    A pure function of `verdict` alone, not the underlying counts: the
    MAGNITUDE of the adjustment lives in exactly one place
    (`_VERDICT_ADJUSTMENT` above), and how much that magnitude is allowed to
    move a ranking lives in exactly one other place (`retrieval.py`'s
    `reliability_weight`, which the caller supplies and this function knows
    nothing about). A lesson with no verdict at all (not present in `scores`
    -- typically because it has never been retrieved with a recorded
    outcome) is absent from the returned dict; `rank_lessons` treats a
    missing slug as 0.0, the same as UNPROVEN, which is the correct reading:
    no evidence is not evidence of harm.
    """
    return {s.slug: _VERDICT_ADJUSTMENT.get(s.verdict, 0.0) for s in scores}


def score_lessons(
    evidence: list[Evidence],
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    precision_floor: float = DEFAULT_PRECISION_FLOOR,
) -> list[LessonReliability]:
    """Credit-assign outcomes back to the lessons that were injected."""
    retrieved_counts: dict[str, int] = {}
    hit_counts: dict[str, int] = {}
    succ_when_retrieved: dict[str, list[bool]] = {}

    for ev in evidence:
        for slug in set(ev.retrieved):
            retrieved_counts[slug] = retrieved_counts.get(slug, 0) + 1
            if ev.succeeded is not None:
                succ_when_retrieved.setdefault(slug, []).append(ev.succeeded)
        # A hit only counts as evidence if the lesson was actually retrieved
        # on that occasion; otherwise a lesson credited by a retro pass would
        # get precision > 1.
        for slug in set(ev.hit) & set(ev.retrieved):
            hit_counts[slug] = hit_counts.get(slug, 0) + 1

    known = [ev.succeeded for ev in evidence if ev.succeeded is not None]
    baseline = (sum(known) / len(known)) if known else None

    out: list[LessonReliability] = []
    for slug, n_ret in sorted(retrieved_counts.items()):
        n_hit = hit_counts.get(slug, 0)
        precision = n_hit / n_ret
        lower = wilson_lower_bound(n_hit, n_ret)

        outcomes = succ_when_retrieved.get(slug, [])
        succ_rate = (sum(outcomes) / len(outcomes)) if outcomes else None
        lift = (succ_rate - baseline) if (succ_rate is not None and baseline is not None) else None

        if n_ret < min_evidence:
            verdict = VERDICT_UNPROVEN
            rationale = (
                f"only {n_ret} retrieval(s); {min_evidence} needed before any claim. "
                "Reported precision at this sample size is noise."
            )
        elif lift is not None and lift < -0.10 and len(outcomes) >= min_evidence:
            verdict = VERDICT_HARMFUL
            rationale = (
                f"tasks succeed {abs(lift) * 100:.0f} points LESS often when this is injected "
                f"({succ_rate:.0%} vs {baseline:.0%} baseline). The rule itself may be wrong, "
                "or right only in a narrower context than it claims."
            )
        elif lower >= precision_floor:
            verdict = VERDICT_RELIABLE
            rationale = (
                f"useful in {n_hit}/{n_ret} retrievals; even the pessimistic bound "
                f"({lower:.0%}) clears the {precision_floor:.0%} floor."
            )
        elif precision < 0.25:
            verdict = VERDICT_MISCALIBRATED
            rationale = (
                f"fires often but helps rarely ({n_hit}/{n_ret}). This is an activation-condition "
                "problem, not necessarily a wrong rule -- tighten `applies_when` before rewriting it."
            )
        else:
            verdict = VERDICT_UNPROVEN
            rationale = (
                f"{n_hit}/{n_ret} useful, but the confidence bound ({lower:.0%}) is still below "
                f"the {precision_floor:.0%} floor. Needs more evidence, not a decision."
            )

        out.append(
            LessonReliability(
                slug=slug, n_retrieved=n_ret, n_hit=n_hit,
                precision=round(precision, 4), precision_lower=round(lower, 4),
                success_rate=round(succ_rate, 4) if succ_rate is not None else None,
                lift=round(lift, 4) if lift is not None else None,
                verdict=verdict, rationale=rationale,
            )
        )

    # Worst first: this list exists to be acted on.
    order = {VERDICT_HARMFUL: 0, VERDICT_MISCALIBRATED: 1, VERDICT_UNPROVEN: 2, VERDICT_RELIABLE: 3}
    out.sort(key=lambda r: (order[r.verdict], -r.n_retrieved))
    return out


# --- Coherence --------------------------------------------------------------


def polarity(text: str) -> float:
    """+1 = purely prescriptive, -1 = purely prohibitive, 0 = neither/mixed.

    A deliberately shallow lexical signal. It catches "always retry" vs
    "never retry" and will miss a contradiction expressed without these
    markers. It exists to narrow a quadratic pair-space down to something a
    human can review, and it is never the sole basis for flagging a pair.
    """
    pro = len(_PROHIBITIVE.findall(text or ""))
    imp = len(_IMPERATIVE.findall(text or ""))
    if pro + imp == 0:
        return 0.0
    return (imp - pro) / (imp + pro)


@dataclass
class Contradiction:
    slug_a: str
    slug_b: str
    activation_overlap: float
    polarity_a: float
    polarity_b: float
    lift_a: float | None
    lift_b: float | None
    signals: list[str] = field(default_factory=list)
    severity: str = "review"


def find_contradictions(
    lessons: list[dict],
    reliability: list[LessonReliability] | None = None,
    activation_overlap: float = DEFAULT_ACTIVATION_OVERLAP,
) -> list[Contradiction]:
    """Find `active` lesson pairs that fire in the same situations but pull
    in opposite directions.

    Two independent signals, because each alone is too weak:

    * **lexical polarity** — one says "always", the other "never". Cheap,
      catches the obvious case, blind to paraphrase.
    * **empirical divergence** — one has positive lift, the other negative,
      in overlapping activation conditions. Says nothing about the words and
      cannot be fooled by them, but needs outcome data to exist.

    Both are gated on activation overlap: two lessons that never fire in the
    same situation cannot contradict each other in practice, however
    opposed their text.
    """
    by_slug = {str(fm.get("name", "")): fm for fm in lessons if fm.get("status") == "active"}
    lift_by_slug = {r.slug: r.lift for r in (reliability or [])}

    sigs = {}
    for slug, fm in by_slug.items():
        tags = fm.get("tags")
        tags_list = [str(t) for t in tags if t is not None] if isinstance(tags, (list, tuple)) else []
        sigs[slug] = minhash(f"{fm.get('applies_when', '')} {' '.join(tags_list)} {fm.get('domain', '')}")
    pol = {slug: polarity(str(fm.get("description", ""))) for slug, fm in by_slug.items()}

    found: list[Contradiction] = []
    slugs = sorted(by_slug)
    for i, a in enumerate(slugs):
        for b in slugs[i + 1:]:
            overlap_score = estimate_jaccard(sigs[a], sigs[b])
            if overlap_score < activation_overlap:
                continue

            signals: list[str] = []
            pa, pb = pol[a], pol[b]
            if pa * pb < 0 and abs(pa) > 0.2 and abs(pb) > 0.2:
                signals.append("opposite prescriptive/prohibitive polarity")

            la, lb = lift_by_slug.get(a), lift_by_slug.get(b)
            if la is not None and lb is not None and la * lb < 0 and abs(la - lb) > 0.2:
                signals.append("opposite measured effect on task success")

            if not signals:
                continue

            found.append(
                Contradiction(
                    slug_a=a, slug_b=b,
                    activation_overlap=round(overlap_score, 4),
                    polarity_a=round(pa, 3), polarity_b=round(pb, 3),
                    lift_a=la, lift_b=lb,
                    signals=signals,
                    # Empirical evidence outranks a word-matching heuristic.
                    severity="high" if len(signals) > 1 or "effect" in " ".join(signals) else "review",
                )
            )

    found.sort(key=lambda c: (c.severity != "high", -c.activation_overlap))
    return found


def render(
    scores: list[LessonReliability],
    contradictions: list[Contradiction],
    min_evidence: int,
) -> str:
    counts: dict[str, int] = {}
    for s in scores:
        counts[s.verdict] = counts.get(s.verdict, 0) + 1

    lines = [
        "# Lesson Reliability Report",
        "",
        "Which lessons actually earn their place, judged against outcomes rather "
        "than against whether a human liked them at authoring time.",
        "",
        f"- Lessons with any retrieval evidence: **{len(scores)}**",
        f"- Reliable: **{counts.get(VERDICT_RELIABLE, 0)}** · "
        f"Miscalibrated: **{counts.get(VERDICT_MISCALIBRATED, 0)}** · "
        f"Harmful: **{counts.get(VERDICT_HARMFUL, 0)}** · "
        f"Unproven: **{counts.get(VERDICT_UNPROVEN, 0)}**",
        f"- Evidence floor: {min_evidence} retrievals before any verdict is issued",
        "",
    ]

    actionable = [s for s in scores if s.verdict in (VERDICT_HARMFUL, VERDICT_MISCALIBRATED)]
    if actionable:
        lines += [
            "## Needs attention",
            "",
            "| Lesson | Verdict | Useful | Precision (lower bound) | Lift |",
            "|---|---|---|---|---|",
        ]
        for s in actionable:
            lift = f"{s.lift:+.0%}" if s.lift is not None else "—"
            lines.append(
                f"| `{s.slug}` | **{s.verdict}** | {s.n_hit}/{s.n_retrieved} | "
                f"{s.precision:.0%} ({s.precision_lower:.0%}) | {lift} |"
            )
        lines.append("")
        for s in actionable:
            lines.append(f"- `{s.slug}` — {s.rationale}")
        lines.append("")

    reliable = [s for s in scores if s.verdict == VERDICT_RELIABLE]
    if reliable:
        lines += [
            "## Earning their place", "",
            "| Lesson | Useful | Precision (lower bound) | Lift |", "|---|---|---|---|",
        ]
        for s in reliable:
            lift = f"{s.lift:+.0%}" if s.lift is not None else "—"
            lines.append(
                f"| `{s.slug}` | {s.n_hit}/{s.n_retrieved} | "
                f"{s.precision:.0%} ({s.precision_lower:.0%}) | {lift} |"
            )
        lines.append("")

    lines += ["## Coherence", ""]
    if not contradictions:
        lines += ["No contradictions detected among active lessons.", ""]
    else:
        lines += [
            f"**{len(contradictions)} contradiction candidate(s).** These fire in overlapping "
            "situations but pull in opposite directions — an agent can receive both at once.",
            "",
            "| A | B | Activation overlap | Signals | Severity |",
            "|---|---|---|---|---|",
        ]
        for c in contradictions:
            lines.append(
                f"| `{c.slug_a}` | `{c.slug_b}` | {c.activation_overlap:.0%} | "
                f"{'; '.join(c.signals)} | {c.severity} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "Nothing here was changed automatically. Act with "
        "`commontrace lesson reject <slug> --reason ...` to retire one, or by "
        "tightening its `applies_when` and leaving it active.",
        "",
        "Caveats worth keeping in view: precision uses the Wilson lower bound, so a "
        "young corpus will correctly read as mostly UNPROVEN rather than mostly good. "
        "Polarity detection is lexical and will miss contradictions phrased without "
        "always/never-style markers.",
        "",
        "**`lift` is correlational, not causal.** A lesson is retrieved *because* the "
        "situation matched its activation condition, so the occasions where it fired "
        "differ systematically from the ones where it did not. A lesson that fires on "
        "routine work can show strong positive lift while contributing nothing; one "
        "that fires only on the hardest cases can look harmful while being the reason "
        "those cases were resolved. This bias does not shrink as more data arrives. "
        "To measure cause instead, retrieve with `commontrace query --experiment "
        "--occasion-id <id>` and read `commontrace experiment`.",
    ]
    return "\n".join(lines)
