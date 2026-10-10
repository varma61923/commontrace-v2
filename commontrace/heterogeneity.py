"""Where does a lesson help? Randomized effects per subgroup of occasions.

The holdout randomizes each (lesson, occasion) independently, so *within* any
subgroup of occasions the injected and withheld arms are still a randomized
comparison -- provided the subgroup is decided by something fixed before the
lesson could act. Who ran the occasion (``agent_type``, ``agent_id``) qualifies.
Anything recorded after the task -- a trace's tags, its text, its outcome --
does not: an injected lesson can change it, and splitting on it would bias
every number below. That is why only those two identity fields are read from
traces, and why an operator-supplied covariate file must attest the same.

Two questions per lesson:

1. **Effect per subgroup**: the same pooled two-proportion test `experiment`
   uses, Benjamini-Hochberg corrected across *every* lesson x subgroup test
   at once (many subgroups mean many chances to find one by luck).
2. **Does the effect differ between subgroups**: Cochran's Q over the
   per-subgroup effects (inverse-variance weights, chi-square with k-1
   degrees of freedom). A lesson significantly helping in one subgroup and
   significantly hurting in another is flagged ``CROSSING`` -- the case where
   a fleet-wide average hides real harm, and where narrowing ``applies_when``
   (``commontrace lesson suggest-revision``) is the fix.

These are fixed-horizon, exploratory readings: they are not the anytime-valid
billing verdict and must not replace it.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from commontrace import experiment

PRE_TREATMENT_FIELDS = ("agent_type", "agent_id")
MAX_GROUPS_PER_LESSON = 50
MAX_GROUP_CHARS = 128

FLAG_CROSSING = "CROSSING"
FLAG_HETEROGENEOUS = "HETEROGENEOUS"
FLAG_CONSISTENT = "CONSISTENT"
FLAG_UNTESTABLE = "UNTESTABLE"


@dataclass
class SubgroupEffect:
    group: str
    n_injected: int
    n_withheld: int
    rate_injected: float
    rate_withheld: float
    effect: float
    ci_low: float
    ci_high: float
    p_value: float
    significant: bool = False


@dataclass
class LessonHeterogeneity:
    lesson_slug: str
    flag: str
    q_statistic: float | None
    degrees_of_freedom: int
    q_p_value: float | None
    groups: list[SubgroupEffect] = field(default_factory=list)
    untested_groups: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "lesson": self.lesson_slug, "flag": self.flag, "q": self.q_statistic,
            "df": self.degrees_of_freedom, "q_p_value": self.q_p_value,
            "groups": [g.__dict__ for g in self.groups], "untested_groups": self.untested_groups,
        }


def _gamma_q(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x), stdlib only (series / continued fraction)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1.0:
        term = total = 1.0 / a
        ap = a
        for _ in range(1000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - gln))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return min(1.0, math.exp(-x + a * math.log(x) - gln) * h)


def chi2_sf(x: float, df: int) -> float:
    """P(X >= x) for a chi-square variable with `df` degrees of freedom."""
    if df <= 0:
        raise ValueError("degrees of freedom must be positive")
    return _gamma_q(df / 2.0, x / 2.0)


def _effect(s1: int, n1: int, s0: int, n0: int, group: str) -> SubgroupEffect:
    p1, p0 = s1 / n1, s0 / n0
    lo, hi = experiment.diff_confidence_interval(s1, n1, s0, n0)
    _z, p = experiment.two_proportion_test(s1, n1, s0, n0)
    return SubgroupEffect(group, n1, n0, round(p1, 4), round(p0, 4), round(p1 - p0, 4),
                          round(lo, 4), round(hi, 4), round(p, 6))


def _cochran_q(groups: list[SubgroupEffect]) -> tuple[float, float] | None:
    weights, effects = [], []
    for g in groups:
        # Unpooled per-arm variance with a half-count continuity floor, so a
        # subgroup at 0% or 100% still contributes a finite, conservative weight.
        p1 = min(max(g.rate_injected, 0.5 / g.n_injected), 1 - 0.5 / g.n_injected)
        p0 = min(max(g.rate_withheld, 0.5 / g.n_withheld), 1 - 0.5 / g.n_withheld)
        var = p1 * (1 - p1) / g.n_injected + p0 * (1 - p0) / g.n_withheld
        weights.append(1.0 / var)
        effects.append(g.effect)
    total = sum(weights)
    pooled = sum(w * e for w, e in zip(weights, effects)) / total
    q = sum(w * (e - pooled) ** 2 for w, e in zip(weights, effects))
    return q, chi2_sf(q, len(groups) - 1)


def analyze(observations: Iterable[experiment.HoldoutObservation], group_of: Mapping[str, str], *,
            min_arm: int = experiment.DEFAULT_MIN_ARM, alpha: float = 0.05) -> list[LessonHeterogeneity]:
    """Per-subgroup effects and a heterogeneity test for every lesson.

    `group_of` maps occasion id -> pre-treatment subgroup label. Occasions with
    no label are left out (never pooled into an "unknown" group, which would
    mix every unlabelled context into one comparison).
    """
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    if min_arm < 1:
        raise ValueError("min_arm must be at least 1")
    counts: dict[str, dict[str, list[int]]] = {}
    for obs in observations:
        group = group_of.get(obs.occasion_id)
        if not group:
            continue
        cell = counts.setdefault(obs.lesson_slug, {}).setdefault(group, [0, 0, 0, 0])
        if obs.injected:
            cell[0] += int(obs.succeeded)
            cell[1] += 1
        else:
            cell[2] += int(obs.succeeded)
            cell[3] += 1
    staged: list[tuple[str, list[SubgroupEffect], list[str]]] = []
    for slug in sorted(counts):
        if len(counts[slug]) > MAX_GROUPS_PER_LESSON:
            raise ValueError(f"lesson {slug!r} spans more than {MAX_GROUPS_PER_LESSON} subgroups; "
                             "choose a coarser grouping")
        tested, untested = [], []
        for group, (s1, n1, s0, n0) in sorted(counts[slug].items()):
            if n1 >= min_arm and n0 >= min_arm:
                tested.append(_effect(s1, n1, s0, n0, group))
            else:
                untested.append(group)
        staged.append((slug, tested, untested))
    every = [g for _slug, tested, _u in staged for g in tested]
    for g, significant in zip(every, experiment.benjamini_hochberg([g.p_value for g in every], alpha=alpha)):
        g.significant = significant
    out = []
    for slug, tested, untested in staged:
        if len(tested) < 2:
            out.append(LessonHeterogeneity(slug, FLAG_UNTESTABLE, None, 0, None, tested, untested))
            continue
        q, q_p = _cochran_q(tested)
        helps = any(g.significant and g.effect > 0 for g in tested)
        hurts = any(g.significant and g.effect < 0 for g in tested)
        if helps and hurts:
            flag = FLAG_CROSSING
        elif q_p < alpha:
            flag = FLAG_HETEROGENEOUS
        else:
            flag = FLAG_CONSISTENT
        out.append(LessonHeterogeneity(slug, flag, round(q, 4), len(tested) - 1, round(q_p, 6), tested, untested))
    rank = {FLAG_CROSSING: 0, FLAG_HETEROGENEOUS: 1, FLAG_CONSISTENT: 2, FLAG_UNTESTABLE: 3}
    out.sort(key=lambda h: (rank[h.flag], h.lesson_slug))
    return out


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_GROUP_CHARS or any(ord(c) < 32 for c in text):
        return None
    return text


def occasion_groups_from_traces(root: str, by: str) -> dict[str, str]:
    """Occasion id -> who ran it, read from captured traces' identity fields."""
    if by not in PRE_TREATMENT_FIELDS:
        raise ValueError(f"--by must be one of {', '.join(PRE_TREATMENT_FIELDS)} (pre-treatment fields only)")
    from commontrace import paths, trace_io

    out: dict[str, str] = {}
    tdir = paths.traces_dir(root)
    if not os.path.isdir(tdir):
        return out
    for name in sorted(os.listdir(tdir)):
        if not name.endswith(".md") or name == "README.md":
            continue
        try:
            fm, _body = trace_io.read(os.path.join(tdir, name))
        except Exception:  # noqa: BLE001 - an unreadable trace contributes no label
            continue
        occasion, group = _clean(fm.get("id")), _clean(fm.get(by))
        if occasion and group:
            out[occasion] = group
    return out


def occasion_groups_from_file(path: str) -> dict[str, str]:
    """JSONL rows ``{"occasion_id": ..., "group": ...}``; the operator attests they are pre-treatment."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"covariates line {number} is not JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"covariates line {number} must be an object")
            occasion, group = _clean(row.get("occasion_id")), _clean(row.get("group"))
            if not occasion or not group:
                raise ValueError(f"covariates line {number} needs occasion_id and group (<= {MAX_GROUP_CHARS} chars)")
            if out.get(occasion, group) != group:
                raise ValueError(f"occasion {occasion!r} has two different groups in the covariates file")
            out[occasion] = group
    return out


def narrowing(h: LessonHeterogeneity) -> tuple[list[SubgroupEffect], list[SubgroupEffect]]:
    """(subgroups where the lesson significantly helps, where it significantly hurts)."""
    return ([g for g in h.groups if g.significant and g.effect > 0],
            [g for g in h.groups if g.significant and g.effect < 0])


def _effect_text(g: SubgroupEffect) -> str:
    return (f"{g.group} ({g.effect:+.1%} [{g.ci_low:+.1%}, {g.ci_high:+.1%}], "
            f"n={g.n_injected}/{g.n_withheld})")


def draft_narrowing(root: str, h: LessonHeterogeneity, by: str, *, actor: str = "experiment") -> str | None:
    """Write a review-status revision that keeps a CROSSING lesson where it helps.

    Nothing about the original lesson changes. The draft's ``applies_when``
    gains an explicit "only for" clause naming the helped subgroups and its
    ``do_not_apply_when`` names the harmed ones, both with their randomized
    effects; ``narrowed_to`` records the same machine-readably. An operator
    approves or rejects it like any draft. Returns the draft path, or None when
    the lesson is not CROSSING or a draft is already waiting.
    """
    from commontrace import frontmatter, lesson_io, paths, templates

    if h.flag != FLAG_CROSSING:
        return None
    helps, hurts = narrowing(h)
    path = lesson_io.lesson_path(root, h.lesson_slug)
    if path is None:
        return None
    fm, body = frontmatter.read(path)
    stem = lesson_io.canonical_slug(h.lesson_slug)
    draft_slug = f"{stem}-narrowed"
    out_path = os.path.join(paths.lessons_dir(root), f"lesson_{draft_slug}.md")
    with frontmatter.locked(out_path):
        if os.path.exists(out_path):
            existing, _ = frontmatter.read(out_path)
            if existing.get("status") == "review":
                return None
        only = ", ".join(_effect_text(g) for g in helps)
        never = ", ".join(_effect_text(g) for g in hurts)
        draft = templates.lesson_frontmatter(
            slug=draft_slug,
            description=f"Narrowed revision of {stem}: only where randomized evidence shows it helps",
            agent_type=str(fm.get("agent_type") or paths.store_agent_type(root)),
            domain=str(fm.get("domain") or ""), tags=list(fm.get("tags") or []),
            applies_when=f"{fm.get('applies_when', '')} Only when {by} is one of: {only}.".strip(),
            do_not_apply_when=f"{fm.get('do_not_apply_when', '')} Not when {by} is one of: {never}.".strip(),
            importance=int(fm.get("importance") or 3),
            importance_rationale=(f"Randomized subgroup effects for {stem} cross zero "
                                  f"(Cochran's Q={h.q_statistic}, p={h.q_p_value})."),
            source_traces=[], status="review", scopes=list(fm.get("scopes") or []),
        )
        draft["revises"] = stem
        draft["narrowed_to"] = {"by": by, "include": [g.group for g in helps], "exclude": [g.group for g in hurts]}
        evidence = [f"- helps: {_effect_text(g)}" for g in helps] + [f"- hurts: {_effect_text(g)}" for g in hurts]
        evidence += [f"- other: {_effect_text(g)}" for g in h.groups if g not in helps and g not in hurts]
        text = (body.rstrip("\n") + "\n\n## Evidence for narrowing\n"
                f"Randomized holdout effects by {by} (injected minus withheld success rate, BH-corrected; "
                "exploratory and fixed-horizon, not the billing verdict):\n" + "\n".join(evidence) + "\n")
        lesson_io.write_lesson(out_path, draft, text, root=root, actor=actor,
                               reason=f"narrowing draft of {stem} from CROSSING subgroup effects by {by}")
    return out_path


def render(results: list[LessonHeterogeneity], by: str) -> str:
    lines = [f"Subgroup effects by {by} (exploratory, fixed-horizon; BH across all subgroup tests; "
             "not the billing verdict)", ""]
    if not results:
        lines.append("No lesson has outcomes in two labelled subgroups yet.")
        return "\n".join(lines)
    for h in results:
        head = f"{h.lesson_slug}: {h.flag}"
        if h.q_statistic is not None:
            head += f"  (Q={h.q_statistic:.2f}, df={h.degrees_of_freedom}, p={h.q_p_value:.3g})"
        lines.append(head)
        for g in h.groups:
            mark = "*" if g.significant else " "
            lines.append(f"  {mark} {g.group:<24} {g.effect:+.1%}  [{g.ci_low:+.1%}, {g.ci_high:+.1%}]  "
                         f"n={g.n_injected}/{g.n_withheld}")
        if h.untested_groups:
            lines.append(f"    below the per-arm floor: {', '.join(h.untested_groups)}")
        if h.flag == FLAG_CROSSING:
            lines.append("    -> helps in one context and hurts in another: `commontrace experiment "
                         f"--by {by} --draft-revisions` drafts a narrowed revision for review.")
    lines.append("")
    lines.append("* = significant after correction. Effects are injected minus withheld success rate.")
    return "\n".join(lines)
