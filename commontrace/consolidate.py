"""Corpus hygiene: which active lessons should be fused, archived, or fixed.

WHY THIS EXISTS
---------------
DOCUMENTATION.md §6.4 describes exactly this job -- "classic memory
consolidation (archive/fuse/reformulate/recalibrate lessons)" -- and assigns
it to a companion skill, Dreamer, that lives outside this repository and
detects fusion candidates via pairwise cosine similarity over
`memory/attention/index.npz`. That makes corpus hygiene unavailable to every
store that never installed the optional `attention` extra, which is the
default install (`pip install commontrace` pulls PyYAML alone --
pyproject.toml).

This is the same job -- fusion candidates, archive candidates, contradiction
candidates -- built on primitives this package already ships without the
optional extra:

  * **Fuse**: `commontrace/redundancy.py`'s lexical near-duplicate detection,
    over active lessons' description/applies_when/do_not_apply_when/body.
    Two lessons a fleet's agent re-derived from two different trace clusters
    read as the same rule here, whether or not the semantic layer is
    installed.
  * **Contradict**: `commontrace/reliability.py`'s existing
    `find_contradictions` -- not reinvented, exactly the DOCUMENTATION.md
    §6.4 principle for the fusion mechanism itself ("Proposals submitted to
    Lambda Phase 11 -- existing mechanism, not reinvented").
  * **Archive**: active lessons with `uses == 0` and `last_hit == "NEVER"` --
    a lesson injected on every occasion that ever matched its activation
    condition and never once counted as used. This is deliberately the
    NARROWEST honest signal available from the schema alone: there is no
    `created_at` field to compute an age-based staleness claim from (see
    commontrace/decay.py, which solves a related but different problem --
    the freshness of a MEASURED EFFECT, not of a lesson's activity -- and is
    not reused here for that reason), so this reports only the case that
    needs no threshold to be true: it has NEVER once been hit.

WHAT THIS DOES NOT DO
----------------------
Exactly what commontrace/reliability.py's own module docstring states for
itself, and for the same reason: "It never modifies a lesson. It produces
evidence and recommendations; the Validator gate ... stays human." An
automated system that silently merged, archived, or rewrote lessons based
on a lexical heuristic would be worse than the redundancy this module
exists to surface.
"""

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
        """Whether a `--strict` gate should pass.

        Every fuse and archive candidate counts -- neither carries a
        severity gradation the way a contradiction does. Only HIGH-severity
        contradictions count, matching `commontrace reliability --strict`'s
        own precedent: a `review`-severity contradiction is exactly the
        weaker, lexical-only signal that report already treats as worth
        surfacing but not worth failing a build over.
        """
        return not (self.fuse or self.archive or self.high_severity_contradictions)


def never_hit(fm: dict) -> bool:
    """Whether an active lesson has never once been retrieved.

    Tolerant of the shapes a hand-edited YAML file produces for `uses`
    (missing, a string, a float that should have been an int) -- the same
    posture `dosage.is_core` takes for `core`, for the same reason: a
    malformed field must not crash a report, only fail to flag the lesson
    it belongs to.
    """
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
    """`lessons` is `evidence_io.load_active_lessons`'s shape: active-status
    lesson frontmatter dicts with the body stashed under
    `templates.BODY_KEY`. Only active lessons are considered for all three
    signals -- a candidate still in review is not yet costing the corpus
    anything (see this module's docstring on why fusion in particular
    checks only what is actually competing for a retrieval slot; the same
    reasoning `commontrace/commands/lesson_cmd.py:_active_lesson_texts`
    documents for the write-time duplicate gate).
    """
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
