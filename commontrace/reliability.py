"""Does this lesson actually work? — credit assignment and coherence."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from commontrace.overlap import estimate_jaccard, minhash

DEFAULT_MIN_EVIDENCE = 5

DEFAULT_PRECISION_FLOOR = 0.50

DEFAULT_ACTIVATION_OVERLAP = 0.25

_Z_95 = 1.959963984540054

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
    """Lower bound of the Wilson score interval for a binomial proportion."""
    if trials <= 0:
        return 0.0
    p_hat = successes / trials
    denom = 1 + z**2 / trials
    center = (p_hat + z**2 / (2 * trials)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / trials + z**2 / (4 * trials**2))
    return max(0.0, center - margin)


@dataclass
class Evidence:
    occasion_id: str
    retrieved: list[str]
    hit: list[str]
    succeeded: bool | None


@dataclass
class LessonReliability:
    slug: str
    n_retrieved: int
    n_hit: int
    precision: float
    precision_lower: float
    success_rate: float | None
    lift: float | None
    verdict: str
    rationale: str


VERDICT_RELIABLE = "RELIABLE"
VERDICT_UNPROVEN = "UNPROVEN"
VERDICT_MISCALIBRATED = "MISCALIBRATED"
VERDICT_HARMFUL = "HARMFUL"

_VERDICT_ADJUSTMENT: dict[str, float] = {
    VERDICT_HARMFUL: -1.0,
    VERDICT_MISCALIBRATED: -0.5,
    VERDICT_UNPROVEN: 0.0,
    VERDICT_RELIABLE: 1.0,
}


def ranking_adjustments(scores: list["LessonReliability"]) -> dict[str, float]:
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

    order = {VERDICT_HARMFUL: 0, VERDICT_MISCALIBRATED: 1, VERDICT_UNPROVEN: 2, VERDICT_RELIABLE: 3}
    out.sort(key=lambda r: (order[r.verdict], -r.n_retrieved))
    return out


def polarity(text: str) -> float:
    """+1 = purely prescriptive, -1 = purely prohibitive, 0 = neither/mixed."""
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
                    severity="high" if len(signals) > 1 or "effect" in " ".join(signals) else "review",
                )
            )

    found.sort(key=lambda c: (c.severity != "high", -c.activation_overlap))
    return found


def render(
    scores: list[LessonReliability],
    contradictions: list[Contradiction],
    min_evidence: int,
    uncaptured: dict[str, int] | None = None,
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

    if uncaptured:
        lines += [
            "## Under-reported",
            "",
            "Retrieved under `--experiment`, but no `capture` was ever recorded for the "
            "occasion -- not reflected in any verdict above, since an unknown outcome is "
            "not the same fact as a confirmed miss:",
            "",
        ]
        for slug in sorted(uncaptured):
            n = uncaptured[slug]
            lines.append(f"- `{slug}` — {n} occasion(s) with no captured outcome")
        lines.append("")

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
