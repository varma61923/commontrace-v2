"""`commontrace pilot` — the 30-day pilot, bundled into one report.

Three steps (PILOT.md, and the sales narrative it backs):

1. **Map the issues** — `commontrace.taxonomy`: group recurring failures
   into a clear taxonomy.
2. **Reproduce + reinforce** — re-run representative cases with the lesson
   enforced. This step is inherently a human/agent loop
   (`commontrace query --experiment` + `commontrace capture
   --occasion-id`, per PILOT.md) that this command cannot perform on
   someone's behalf; what it reports is how far that loop has progressed
   (active lessons, holdout assignments logged).
3. **Measure what changed** — `commontrace.impact` plus the baseline-vs-
   current resolution rate already computed by `commontrace bench --pilot`.

"The result" is a yes/no gate, and it is deliberately conservative: a
randomized holdout result (`commontrace experiment`) always outranks a
correlational one, exactly as it does everywhere else in this codebase
(README.md's "Prove the lessons cause the improvement", STRATEGY.md §8).
A pilot that only ran the correlational half gets "not yet conclusive," not
a borrowed "yes."
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from commontrace import impact as impact_mod
from commontrace import report_html
from commontrace import taxonomy as taxonomy_mod

RESULT_YES = "yes"
RESULT_NO = "no"
RESULT_UNKNOWN = "unknown"


@dataclass
class Verdict:
    label: str
    level: str  # RESULT_YES / RESULT_NO / RESULT_UNKNOWN
    explanation: str


def determine_result(
    *,
    harmful_lesson_slugs: list[str],
    causal_effects: list | None,
    resolution_delta: float | None,
    has_baseline: bool,
) -> Verdict:
    """The yes/no gate. `causal_effects` is None when no holdout data exists
    yet (as opposed to an empty list, which would mean data exists but no
    lesson qualified for analysis -- callers pass None only in the true
    absence of `memory/holdout_log.jsonl`)."""
    if causal_effects is not None:
        hurts = [e for e in causal_effects if e.verdict == "HURTS"]
        helps = [e for e in causal_effects if e.verdict == "HELPS"]
        if hurts:
            return Verdict(
                "NO",
                RESULT_NO,
                f"{len(hurts)} lesson(s) — {', '.join(e.lesson_slug for e in hurts)} — "
                "measurably HURT outcomes in a randomized holdout test. Fix or reject "
                "them before drawing any conclusion from the rest.",
            )
        if helps:
            return Verdict(
                "YES",
                RESULT_YES,
                f"{len(helps)} lesson(s) — {', '.join(e.lesson_slug for e in helps)} — "
                "measurably help outcomes, causally (randomized holdout, see "
                "`commontrace experiment`).",
            )
        return Verdict(
            "NOT YET CONCLUSIVE",
            RESULT_UNKNOWN,
            "Holdout assignments are logged, but nothing can be analyzed yet -- "
            "either no lesson has a matching recorded outcome, or none has reached "
            "significance (NO_MEASURABLE_EFFECT or UNDERPOWERED). Keep logging "
            "occasions with `commontrace query --experiment`, record each outcome "
            "under the same `--occasion-id`, and re-run.",
        )

    if harmful_lesson_slugs:
        return Verdict(
            "NO",
            RESULT_NO,
            f"{len(harmful_lesson_slugs)} lesson(s) — {', '.join(harmful_lesson_slugs)} — "
            "score HARMFUL against outcomes. This is correlational "
            "(`commontrace reliability`), but a harmful correlational signal is still "
            "a reason to stop and look, not a reason to claim success elsewhere.",
        )

    if has_baseline and resolution_delta is not None:
        if resolution_delta > 0.05:
            # resolution_delta is a RELATIVE change ((current - baseline) /
            # baseline -- see pilot_cmd.py), not a percentage-point
            # difference in the resolution rate itself. reference/
            # pilot_metrics.py's own table shows this same relative number
            # next to the actual baseline/current values, so "+53%" reads
            # correctly there; this headline shows the delta ALONE, where
            # the identical number is easy to misread as "resolution rate
            # is now 53%" or "up 53 percentage points" -- either of which
            # can be wildly different from the real, smaller (or larger)
            # absolute change. Spelling out "relative" here costs nothing
            # and removes that ambiguity without changing what is computed
            # or gated on.
            return Verdict(
                f"LIKELY (resolution rate +{resolution_delta:.0%} relative to baseline)",
                RESULT_UNKNOWN,
                "Resolution rate improved baseline-to-current and no lesson scores "
                "harmful, but this is correlational, not causal — the same "
                "before/after confound `commontrace experiment` exists to remove. "
                "Run it before calling this a yes.",
            )
        return Verdict(
            "NOT YET",
            RESULT_NO,
            "Resolution rate has not improved baseline-to-current, and no causal "
            "test has been run. See the taxonomy and cost sections below for what "
            "to reinforce next.",
        )

    return Verdict(
        "NOT ENOUGH DATA",
        RESULT_UNKNOWN,
        "No baseline traces (`commontrace capture --baseline`) and no holdout "
        "data yet. See PILOT.md to start the loop.",
    )


@dataclass
class PilotReport:
    taxonomy: "taxonomy_mod.Taxonomy"
    impact: "impact_mod.ImpactReport"
    n_active_lessons: int
    n_holdout_assignments: int
    causal_effects: list | None
    resolution_baseline: float | None
    resolution_current: float | None
    resolution_delta: float | None
    n_baseline_traces: int
    n_current_traces: int
    harmful_lesson_slugs: list[str] = field(default_factory=list)

    @property
    def result(self) -> Verdict:
        return determine_result(
            harmful_lesson_slugs=self.harmful_lesson_slugs,
            causal_effects=self.causal_effects,
            resolution_delta=self.resolution_delta,
            has_baseline=self.n_baseline_traces > 0,
        )


def _fmt_pct(v: float | None) -> str:
    return "N/A" if v is None else f"{v:.1%}"


def render_markdown(r: PilotReport) -> str:
    result = r.result
    out = [
        "# CommonTrace Pilot Report",
        "",
        "A 30-day pilot answers one question: does CommonTrace actually fix "
        "the issues? Start from the failures you already see, reproduce them, "
        "reinforce the right behavior, and measure what changed.",
        "",
        f"## THE RESULT — {result.label}",
        "",
        result.explanation,
        "",
        "---",
        "",
        "## 1. Map the issues",
        "",
        f"- Recurring patterns found: **{r.taxonomy.n_patterns}** "
        f"(across {r.taxonomy.n_traces_total} traces)",
        f"- Already covered by an active lesson: **{r.taxonomy.n_covered}**",
        f"- Gaps (recurring, no lesson yet): **{r.taxonomy.n_gaps}**",
        "",
        "Full breakdown: `commontrace taxonomy`.",
        "",
        "## 2. Reproduce + reinforce",
        "",
        f"- Active lessons: **{r.n_active_lessons}**",
        f"- Holdout assignments logged (`commontrace query --experiment`): "
        f"**{r.n_holdout_assignments}**",
        "",
        "Re-run representative cases with the lesson enforced: "
        "`commontrace query \"<task>\" --experiment --occasion-id <id>`, then "
        "`commontrace capture ... --occasion-id <id>`. See PILOT.md for sample "
        "sizes.",
        "",
        "## 3. Measure what changed",
        "",
    ]
    if r.n_baseline_traces:
        out.append(
            f"- Resolution rate — baseline: {_fmt_pct(r.resolution_baseline)} "
            f"({r.n_baseline_traces} traces) · current: {_fmt_pct(r.resolution_current)} "
            f"({r.n_current_traces} traces) · change: "
            f"{'N/A' if r.resolution_delta is None else f'{r.resolution_delta:+.1%}'}"
        )
    else:
        out.append(
            "- No baseline traces yet (`commontrace capture --baseline ...`) — "
            "nothing to compare resolution rate against."
        )
    out += [
        f"- Errors avoided: **{r.impact.errors_avoided}** · "
        f"Lessons reused: **{r.impact.lessons_reused}**",
        "",
        "Full breakdown: `commontrace impact`. Full outcome-metric table: "
        "`commontrace bench --pilot`.",
        "",
        "---",
        "",
        "*Every correlational number above (resolution-rate delta, errors "
        "avoided, reliability lift) is subject to the confound explained in "
        "STRATEGY.md §8: a lesson is retrieved because the situation matched "
        "it. `commontrace experiment` removes that confound by randomized "
        "holdout, and outranks every other number here whenever both exist.*",
    ]
    return "\n".join(out)


def render_html(r: PilotReport, timestamp: str) -> str:
    result = r.result
    css_class = {
        RESULT_YES: "result-yes",
        RESULT_NO: "result-no",
        RESULT_UNKNOWN: "result-unknown",
    }[result.level]

    body = f"""<h1>Pilot Report</h1>
<p class="subtitle">Does CommonTrace actually fix the issues? A 30-day pilot answers it.</p>
<div class="result-banner {css_class}"><strong>THE RESULT — {html.escape(result.label)}</strong>
<p>{html.escape(result.explanation)}</p></div>
<h2>1. Map the issues</h2>
{taxonomy_mod.render_html_fragment(r.taxonomy)}
<h2>2. Reproduce + reinforce</h2>
<p>Active lessons: <strong>{r.n_active_lessons}</strong> ·
Holdout assignments logged: <strong>{r.n_holdout_assignments}</strong></p>
<p class="caveat">Re-run representative cases with the lesson enforced:
<code>commontrace query "&lt;task&gt;" --experiment --occasion-id &lt;id&gt;</code>,
then <code>commontrace capture ... --occasion-id &lt;id&gt;</code>. See PILOT.md.</p>
<h2>3. Measure what changed</h2>
{impact_mod.render_html_fragment(r.impact)}
"""
    return report_html.wrap_page("CommonTrace Pilot Report", body, timestamp)
