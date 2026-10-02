"""Corpus hygiene: which active lessons should be fused, archived, or fixed."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from commontrace import redundancy, reliability, templates


@dataclass(frozen=True)
class ConsolidationReport:
    n_active: int
    fuse: tuple[redundancy.Pair, ...] = field(default_factory=tuple)
    contradict: tuple["reliability.Contradiction", ...] = field(default_factory=tuple)
    archive: tuple[str, ...] = field(default_factory=tuple)

    @property
    def high_severity_contradictions(self) -> tuple["reliability.Contradiction", ...]:
        return tuple(c for c in self.contradict if c.severity == "high")

    @property
    def is_clean(self) -> bool:
        """Whether a `--strict` gate should pass."""
        return not (self.fuse or self.archive or self.high_severity_contradictions)


def never_hit(fm: dict) -> bool:
    """Whether an active lesson has never once been retrieved."""
    try:
        uses_zero = int(fm.get("uses") or 0) == 0
    except (TypeError, ValueError):
        return False
    last_hit = str(fm.get("last_hit") or "").strip().upper()
    return uses_zero and last_hit == "NEVER"


def build_report(
    lessons: list[dict],
    *,
    redundancy_threshold: float = redundancy.DEFAULT_THRESHOLD,
    activation_overlap: float = reliability.DEFAULT_ACTIVATION_OVERLAP,
) -> ConsolidationReport:
    active = [fm for fm in lessons if str(fm.get("status", "")) == "active"]

    fuse = redundancy.find_near_duplicates(
        [
            (str(fm.get("name", "")),
             redundancy.comparable_text(fm, str(fm.get(templates.BODY_KEY, "") or "")))
            for fm in active
        ],
        threshold=redundancy_threshold,
    )
    contradict = reliability.find_contradictions(active, activation_overlap=activation_overlap)
    archive = tuple(sorted(
        str(fm.get("name", "")) for fm in active if never_hit(fm)
    ))

    return ConsolidationReport(
        n_active=len(active), fuse=tuple(fuse), contradict=tuple(contradict), archive=archive,
    )


def render(report: ConsolidationReport) -> str:
    lines = [
        "# Consolidation Report",
        "",
        "Fusion, archive, and contradiction candidates in the active corpus -- "
        "reporting only, nothing here changes a lesson.",
        "",
        f"- Active lessons: **{report.n_active}**",
        f"- Fusion candidates: **{len(report.fuse)}** · "
        f"Contradiction candidates: **{len(report.contradict)}** · "
        f"Never retrieved: **{len(report.archive)}**",
        "",
    ]

    lines += ["## Fuse", ""]
    if not report.fuse:
        lines += ["No near-duplicate active lessons detected.", ""]
    else:
        lines += [
            "Pairs saying substantially the same thing "
            "(commontrace/redundancy.py). Each one is spending a retrieval "
            "slot the other could have used for something distinct.",
            "",
            "| A | B | Similarity |",
            "|---|---|---|",
        ]
        for pair in report.fuse:
            lines.append(f"| `{pair.a}` | `{pair.b}` | {pair.similarity:.0%} |")
        lines.append("")

    lines += ["## Archive", ""]
    if not report.archive:
        lines += ["Every active lesson has been retrieved at least once.", ""]
    else:
        lines += [
            "Active lessons with `uses: 0` and `last_hit: NEVER` -- injected "
            "whenever a task matched their activation condition, and never once "
            "counted as used. Either the activation condition never fires in "
            "practice, or the corpus has not been queried enough yet to tell.",
            "",
        ]
        for slug in report.archive:
            lines.append(f"- `{slug}`")
        lines.append("")

    lines += ["## Contradict", ""]
    if not report.contradict:
        lines += ["No contradictions detected among active lessons.", ""]
    else:
        lines += [
            f"**{len(report.contradict)} contradiction candidate(s).** These fire in "
            "overlapping situations but pull in opposite directions -- an agent can "
            "receive both at once.",
            "",
            "| A | B | Activation overlap | Signals | Severity |",
            "|---|---|---|---|---|",
        ]
        for c in report.contradict:
            lines.append(
                f"| `{c.slug_a}` | `{c.slug_b}` | {c.activation_overlap:.0%} | "
                f"{'; '.join(c.signals)} | {c.severity} |"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "Every finding here is a PROPOSAL, exactly like `commontrace distill`'s "
        "candidates and `commontrace reliability`'s contradictions: a human decides "
        "whether to act. Nothing is merged, archived, or edited automatically.",
        "",
        "  - Fuse: read both with `commontrace lesson history <slug>`, keep the "
        "better-written one, `commontrace lesson reject` the other.",
        "  - Archive: tighten `applies_when` if the condition is too narrow to ever "
        "fire, or reject it if the rule turned out not to matter.",
        "  - Contradict: `commontrace reliability` has the full detail (measured "
        "effect, not just the words) for the pair named here.",
        "",
        "Caveat worth keeping in view: fusion is lexical (token-set Jaccard, "
        "commontrace/redundancy.py) and will miss two lessons that say the same "
        "thing with no shared vocabulary. It is a floor on redundancy, not a ceiling.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skill-candidate emission: repeated contradiction-free patterns -> skill drafts
# ---------------------------------------------------------------------------

_SKILL_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _skill_slug(a: str, b: str) -> str:
    """A valid skill name for the fuse pair (a, b); matches skills.NAME_RE."""
    first, second = (a, b) if a <= b else (b, a)
    stem = _SKILL_SLUG_RE.sub("-", f"{first}-and-{second}".lower()).strip("-")
    stem = re.sub(r"-{2,}", "-", stem).strip("-")
    return (stem or "skill")[:60].rstrip("-") or "skill"


@dataclass(frozen=True)
class SkillCandidate:
    """A repeated contradiction-free pattern worth promoting to a skill."""

    name: str
    description: str
    when_to_use: str = ""
    sources: tuple[str, ...] = field(default_factory=tuple)
    rationale: str = ""


def find_skill_candidates(
    report: ConsolidationReport,
    lessons: list[dict],
) -> list[SkillCandidate]:
    """Fuse pairs with no contradiction involvement become skill candidates.

    A pair is "repeated" when :func:`build_report` flags it as a fusion
    candidate (the same guidance written twice), and "contradiction-free"
    when neither lesson appears in any contradiction in the same report.
    Contradicted pairs are excluded: promoting them to a skill would
    entrench guidance the corpus itself disagrees with.
    """
    by_name = {str(fm.get("name", "")): fm for fm in lessons if fm.get("name")}
    contradicted: set[str] = set()
    for c in report.contradict:
        contradicted.add(str(c.slug_a))
        contradicted.add(str(c.slug_b))

    candidates: list[SkillCandidate] = []
    seen_names: set[str] = set()
    for pair in sorted(report.fuse, key=lambda p: (p.a, p.b)):
        if pair.a in contradicted or pair.b in contradicted:
            continue
        base = _skill_slug(pair.a, pair.b)
        name, n = base, 2
        while name in seen_names:
            suffix = f"-{n}"
            name, n = f"{base[:60 - len(suffix)]}{suffix}", n + 1
        seen_names.add(name)
        first = by_name.get(pair.a, {})
        applies = str(first.get("applies_when", "") or "").strip()
        description = (
            f"Repeated pattern from lessons `{pair.a}` and `{pair.b}` "
            f"({pair.similarity:.0%} similar) with no contradicting guidance."
        )
        candidates.append(SkillCandidate(
            name=name,
            description=description,
            when_to_use=applies,
            sources=(pair.a, pair.b),
            rationale=(
                f"fuse similarity {pair.similarity:.0%}; "
                "neither lesson appears in a contradiction candidate"
            ),
        ))
    return candidates


def emit_skill_drafts(
    root: str,
    candidates: list[SkillCandidate],
    *,
    overwrite: bool = False,
) -> list[str]:
    """Write skill drafts under ``<root>/skills/<name>/SKILL.md``.

    Each draft carries valid skill frontmatter (name, description,
    when_to_use, user_invocable) at status draft -- a human promotes or
    rejects it. Existing drafts are left alone unless *overwrite* is true.
    Returns the list of written file paths.
    """
    from commontrace import frontmatter

    written: list[str] = []
    for cand in candidates:
        dir_path = os.path.join(os.path.abspath(root), "skills", cand.name)
        out_path = os.path.join(dir_path, "SKILL.md")
        if os.path.exists(out_path) and not overwrite:
            continue
        fm: dict = {
            "name": cand.name,
            "description": cand.description,
        }
        if cand.when_to_use:
            fm["when_to_use"] = cand.when_to_use
        fm["user_invocable"] = False
        sources = "\n".join(f"- `{slug}`" for slug in cand.sources)
        body = (
            f"Consolidated draft from repeated lessons:\n{sources}\n\n"
            f"{cand.rationale}\n\n"
            "## When to use\n"
            f"{cand.when_to_use or 'TODO: the shared trigger both lessons describe.'}\n\n"
            "## Procedure\n"
            "TODO: the shared reusable steps distilled from the sources above.\n"
        )
        os.makedirs(dir_path, exist_ok=True)
        frontmatter.write(out_path, fm, body)
        written.append(out_path)
    return sorted(written)
