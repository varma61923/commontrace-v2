"""Corpus hygiene: which active lessons should be fused, archived, or fixed."""

from __future__ import annotations

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
