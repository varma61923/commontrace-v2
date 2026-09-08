"""`commontrace impact` — the Impact Dashboard: "Monitor errors avoided,
lessons reused, and value generated or saved" (PILOT.md step 3: "Measure
what changed").

Three numbers, each defined precisely rather than asserted, because a
number a prospect is shown has to survive being asked "how was that
computed":

* **Lessons reused** — how many times an already-curated lesson proved
  useful again on a fresh occasion. Counted directly from retrieval
  evidence (the same `extensions.lessons_retrieved`/`lessons_hit` and
  episode `lessons_retrieved_by_alpha`/`lessons_hit` fields
  `commontrace reliability` reads), not modeled.

* **Errors avoided** — among occasions where a lesson was hit, how many
  reached a successful outcome (resolved, or explicitly not a repeated
  error). This is **correlational**, exactly like `reliability`'s `lift`
  and `bench --pilot`'s deltas: a lesson fires *because* the situation
  matched its activation condition, so occasions where one fired differ
  systematically from occasions where none did. It is not a causal claim.
  For a causal number, run `commontrace query --experiment` +
  `commontrace experiment` (see PILOT.md) and read that instead.

* **Value generated or saved** — an *estimate*, and only ever computed from
  rates the caller supplies explicitly (`--cost-per-1k-tokens`,
  `--value-per-error-avoided`). Matching `hub/plans.py`'s "no currency
  appears anywhere in this repository, and that is deliberate": nothing
  here hardcodes a dollar figure. Omit both flags and this dashboard
  reports the underlying counts with no dollar total at all, rather than
  inventing a rate to fill one in.
"""
from __future__ import annotations

import dataclasses
import html
from dataclasses import dataclass

from commontrace import reliability, report_html


@dataclass
class ImpactReport:
    n_occasions_with_lesson: int
    lessons_reused: int
    errors_avoided: int
    errors_avoided_basis: int
    n_baseline: int
    n_current: int
    avg_tokens_baseline: float | None
    avg_tokens_current: float | None
    tokens_saved_per_task: float | None
    tokens_saved_total: float | None
    cost_per_1k_tokens: float | None
    value_per_error_avoided: float | None
    dollar_value_tokens: float | None
    dollar_value_errors: float | None
    dollar_value_total: float | None

    @property
    def errors_avoided_rate(self) -> float | None:
        if self.errors_avoided_basis == 0:
            return None
        return self.errors_avoided / self.errors_avoided_basis


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_impact(
    evidence: list[reliability.Evidence],
    traces: list[dict],
    cost_per_1k_tokens: float | None = None,
    value_per_error_avoided: float | None = None,
) -> ImpactReport:
    n_occasions_with_lesson = sum(1 for e in evidence if e.retrieved)

    lessons_reused = 0
    errors_avoided = 0
    errors_avoided_basis = 0
    for e in evidence:
        # set(e.hit) & set(e.retrieved), not a bare `e.hit` truthiness
        # check, for both lessons_reused and errors_avoided/basis below --
        # the same rule reliability.py:score_lessons already documents and
        # enforces ("a hit only counts as evidence if the lesson was
        # actually retrieved on that occasion; otherwise a lesson credited
        # by a retro pass would get precision > 1"). Without the
        # intersection, an occasion whose `lessons_hit` was populated by
        # something other than this occasion's own retrieval call (a
        # retro/backfill pass, hand-edited evidence) inflated both figures
        # with a success this product's own retrieval cannot actually take
        # credit for -- exactly the number a prospect would ask "how was
        # that computed?" about. Computed once per occasion and shared by
        # both rather than recomputed a second time for errors_avoided.
        hit_and_retrieved = set(e.hit) & set(e.retrieved)
        lessons_reused += len(hit_and_retrieved)
        if not hit_and_retrieved or e.succeeded is None:
            continue
        errors_avoided_basis += 1
        if e.succeeded:
            errors_avoided += 1

    baseline_tokens: list[float] = []
    current_tokens: list[float] = []
    n_baseline = 0
    n_current = 0
    for t in traces:
        outcome = t.get("outcome")
        if not isinstance(outcome, dict):
            continue
        is_baseline = outcome.get("baseline") is True
        if is_baseline:
            n_baseline += 1
        else:
            n_current += 1
        tokens = outcome.get("tokens_used")
        if isinstance(tokens, (int, float)) and not isinstance(tokens, bool):
            (baseline_tokens if is_baseline else current_tokens).append(float(tokens))

    avg_tokens_baseline = _mean(baseline_tokens)
    avg_tokens_current = _mean(current_tokens)

    tokens_saved_per_task = None
    tokens_saved_total = None
    if avg_tokens_baseline is not None and avg_tokens_current is not None and n_current:
        tokens_saved_per_task = avg_tokens_baseline - avg_tokens_current
        tokens_saved_total = tokens_saved_per_task * n_current

    dollar_value_tokens = None
    if cost_per_1k_tokens is not None and tokens_saved_total is not None:
        dollar_value_tokens = tokens_saved_total * (cost_per_1k_tokens / 1000.0)

    dollar_value_errors = None
    if value_per_error_avoided is not None:
        dollar_value_errors = errors_avoided * value_per_error_avoided

    dollar_value_total = None
    if dollar_value_tokens is not None or dollar_value_errors is not None:
        dollar_value_total = (dollar_value_tokens or 0.0) + (dollar_value_errors or 0.0)

    return ImpactReport(
        n_occasions_with_lesson=n_occasions_with_lesson,
        lessons_reused=lessons_reused,
        errors_avoided=errors_avoided,
        errors_avoided_basis=errors_avoided_basis,
        n_baseline=n_baseline,
        n_current=n_current,
        avg_tokens_baseline=avg_tokens_baseline,
        avg_tokens_current=avg_tokens_current,
        tokens_saved_per_task=tokens_saved_per_task,
        tokens_saved_total=tokens_saved_total,
        cost_per_1k_tokens=cost_per_1k_tokens,
        value_per_error_avoided=value_per_error_avoided,
        dollar_value_tokens=dollar_value_tokens,
        dollar_value_errors=dollar_value_errors,
        dollar_value_total=dollar_value_total,
    )


def to_dict(r: ImpactReport) -> dict:
    out = dataclasses.asdict(r)
    out["errors_avoided_rate"] = r.errors_avoided_rate
    return out


def _fmt_num(v: float | None, digits: int = 0) -> str:
    return "N/A" if v is None else f"{v:,.{digits}f}"


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def render_markdown(r: ImpactReport) -> str:
    out = [
        "# CommonTrace Impact Dashboard",
        "",
        "Errors avoided, lessons reused, and value generated or saved "
        "(PILOT.md step 3: \"Measure what changed\").",
        "",
        f"- **Lessons reused**: {r.lessons_reused} "
        f"(across {r.n_occasions_with_lesson} occasion(s) where a lesson was retrieved)",
        f"- **Errors avoided**: {r.errors_avoided} / {r.errors_avoided_basis} "
        f"({_fmt_num(r.errors_avoided_rate * 100 if r.errors_avoided_rate is not None else None, 1)}%"
        " of occasions with a known outcome where a lesson hit)",
        "",
        "> Errors avoided is **correlational**, like `reliability`'s `lift`: a lesson "
        "fires because the situation matched it, so these occasions are not a random "
        "sample. For a causal number, run `commontrace experiment`.",
        "",
        "## Cost",
        "",
        f"- Avg. tokens/task — baseline: {_fmt_num(r.avg_tokens_baseline, 1)} "
        f"({r.n_baseline} traces) · current: {_fmt_num(r.avg_tokens_current, 1)} ({r.n_current} traces)",
        f"- Tokens saved per task: {_fmt_num(r.tokens_saved_per_task, 1)}"
        + (f" (× {r.n_current} current traces = {_fmt_num(r.tokens_saved_total, 0)} total)"
           if r.tokens_saved_total is not None else ""),
        "",
        "## Value generated or saved (estimate)",
        "",
    ]
    if r.dollar_value_total is None:
        out += [
            "*No dollar figure computed — pass `--cost-per-1k-tokens` and/or "
            "`--value-per-error-avoided` to estimate one from the counts above. "
            "Nothing here assumes a rate on your behalf.*",
        ]
    else:
        if r.dollar_value_tokens is not None:
            out.append(
                f"- From token savings, at ${r.cost_per_1k_tokens}/1k tokens: {_fmt_money(r.dollar_value_tokens)}"
            )
        if r.dollar_value_errors is not None:
            out.append(
                f"- From errors avoided, at ${r.value_per_error_avoided}/error: {_fmt_money(r.dollar_value_errors)}"
            )
        out.append(f"- **Total (estimate): {_fmt_money(r.dollar_value_total)}**")
        out.append("")
        out.append(
            "*This is an estimate built from a rate you supplied, not a measurement — "
            "the counts above are measured, the dollar conversion is your assumption.*"
        )
    out.append("")
    return "\n".join(out)


def render_html_fragment(r: ImpactReport) -> str:
    value_card = (
        report_html.stat_card("Value generated + saved", "N/A", "pass a rate flag to estimate")
        if r.dollar_value_total is None
        else report_html.stat_card(
            "Value generated + saved", _fmt_money(r.dollar_value_total), "estimate — see below"
        )
    )
    cards = "".join([
        report_html.stat_card("Errors avoided", str(r.errors_avoided), "in production, to date"),
        report_html.stat_card("Lessons reused", str(r.lessons_reused)),
        value_card,
    ])
    dollar_detail = ""
    if r.dollar_value_total is not None:
        parts = []
        if r.dollar_value_tokens is not None:
            money = html.escape(_fmt_money(r.dollar_value_tokens))
            parts.append(f"<li>From token savings, at ${html.escape(str(r.cost_per_1k_tokens))}/1k tokens: {money}</li>")
        if r.dollar_value_errors is not None:
            money = html.escape(_fmt_money(r.dollar_value_errors))
            parts.append(f"<li>From errors avoided, at ${html.escape(str(r.value_per_error_avoided))}/error: {money}</li>")
        dollar_detail = f"<ul>{''.join(parts)}</ul>"
    else:
        dollar_detail = (
            "<p>No dollar figure computed — pass <code>--cost-per-1k-tokens</code> and/or "
            "<code>--value-per-error-avoided</code> to estimate one. Nothing here assumes a rate.</p>"
        )

    return f"""<h1>Impact Dashboard</h1>
<p class="subtitle">Errors avoided, lessons reused, and value generated or saved.</p>
<div class="cards">{cards}</div>
<p class="caveat">Production count to date. Errors avoided is correlational, like
<code>reliability</code>'s <code>lift</code> — for a causal number, run
<code>commontrace experiment</code>. Dollar value is an estimate.</p>
<h2>Cost</h2>
<p>Avg. tokens/task — baseline: {html.escape(_fmt_num(r.avg_tokens_baseline, 1))}
({r.n_baseline} traces) · current: {html.escape(_fmt_num(r.avg_tokens_current, 1))} ({r.n_current} traces)</p>
<h2>Value detail</h2>
{dollar_detail}
"""


def render_html(r: ImpactReport, timestamp: str) -> str:
    return report_html.wrap_page("CommonTrace Impact Dashboard", render_html_fragment(r), timestamp)
