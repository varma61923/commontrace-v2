from __future__ import annotations

import dataclasses
import html
from dataclasses import dataclass, field

from commontrace import distill, report_html, templates


@dataclass
class PatternGroup:
    description: str
    shared_terms: list[str]
    tags: list[str]
    trace_ids: list[str]
    n_traces: int
    covered: bool
    lesson_slug: str | None


@dataclass
class DomainGroup:
    domain: str
    patterns: list[PatternGroup] = field(default_factory=list)

    @property
    def n_traces(self) -> int:
        return sum(p.n_traces for p in self.patterns)


@dataclass
class Taxonomy:
    domains: list[DomainGroup]
    n_traces_total: int
    n_patterns: int
    n_covered: int
    n_gaps: int
    n_unclustered: int


def _eligible_lessons(lessons: list[dict]) -> list[dict]:
    """Active, fully drafted lessons in deterministic (name-sorted) order.

    Factoring the per-lesson eligibility checks out of the per-pattern loop:
    `templates.unfilled_placeholders` is evaluated once per lesson per
    `build_taxonomy` call instead of once per (pattern, lesson) pair.
    """
    return [
        fm for fm in sorted(lessons, key=lambda lesson: str(lesson.get("name", "")))
        if fm.get("status") == "active"
        and not templates.unfilled_placeholders(fm, str(fm.get(templates.BODY_KEY) or ""))
    ]


def _coverage_index(eligible: list[dict]) -> dict[str, list[int]]:
    """Inverted index: source trace id -> ordinals into `eligible`.

    Postings reference lesson dicts (not names) so duplicate lesson names
    score exactly as the old per-lesson scan did: one overlap value per dict,
    first maximum in name order winning. `eligible` must be name-sorted, so
    ascending ordinals reproduce that order.
    """
    index: dict[str, list[int]] = {}
    for pos, fm in enumerate(eligible):
        for trace_id in fm.get("source_traces") or []:
            index.setdefault(str(trace_id), []).append(pos)
    return index


def _best_covering_lesson(
    trace_ids: set[str],
    lessons: list[dict],
    _index: tuple[list[dict], dict[str, list[int]]] | None = None,
) -> str | None:
    """The active lesson covering most of `trace_ids`, or None.

    Before: O(lessons) set intersections per pattern. With the `_index` built
    once per `build_taxonomy` call (see `_coverage_index`), each pattern costs
    O(|trace_ids| x avg postings). Called without `_index` (e.g. directly in
    tests), the index is built for this single call -- same result, no saving.
    """
    if _index is None:
        eligible = _eligible_lessons(lessons)
        index = _coverage_index(eligible)
    else:
        eligible, index = _index
    counts: dict[int, int] = {}
    for trace_id in trace_ids:
        for pos in index.get(trace_id, ()):
            counts[pos] = counts.get(pos, 0) + 1
    best_pos = None
    best_overlap = 0
    for pos in sorted(counts):
        if counts[pos] > best_overlap:
            best_overlap = counts[pos]
            best_pos = pos
    if best_pos is None:
        return None
    return str(eligible[best_pos].get("name", "")) or None


def build_taxonomy(
    traces: list[distill.TraceCandidate],
    lessons: list[dict],
    similarity_threshold: float = 0.3,
    min_cluster_size: int = 2,
) -> Taxonomy:
    clusters = distill.find_clusters(
        traces,
        existing_lessons_source_traces=[],
        similarity_threshold=similarity_threshold,
        min_cluster_size=min_cluster_size,
    )

    by_domain: dict[str, DomainGroup] = {}
    n_covered = 0
    clustered_ids: set[str] = set()

    eligible = _eligible_lessons(lessons)
    coverage_index = (eligible, _coverage_index(eligible))

    for cluster in clusters:
        trace_ids = {t.id for t in cluster.traces}
        clustered_ids |= trace_ids
        agent_type = cluster.traces[0].agent_type or "custom"
        lesson_slug = _best_covering_lesson(trace_ids, lessons, _index=coverage_index)
        covered = lesson_slug is not None
        fallback_domain = distill.propose_domain(cluster, agent_type)
        if covered:
            n_covered += 1
            matched = next((fm for fm in lessons if fm.get("name") == lesson_slug), None)
            domain = str(matched.get("domain")) if matched and matched.get("domain") else fallback_domain
        else:
            domain = fallback_domain

        group = by_domain.setdefault(domain, DomainGroup(domain=domain))
        group.patterns.append(
            PatternGroup(
                description=distill.propose_description(cluster),
                shared_terms=cluster.shared_terms,
                tags=distill.propose_tags(cluster),
                trace_ids=sorted(trace_ids),
                n_traces=len(cluster.traces),
                covered=covered,
                lesson_slug=lesson_slug,
            )
        )

    domains = sorted(by_domain.values(), key=lambda g: (-g.n_traces, g.domain))
    for g in domains:
        g.patterns.sort(key=lambda p: (-p.n_traces, p.description))

    return Taxonomy(
        domains=domains,
        n_traces_total=len(traces),
        n_patterns=len(clusters),
        n_covered=n_covered,
        n_gaps=len(clusters) - n_covered,
        n_unclustered=len(traces) - len(clustered_ids),
    )


def to_dict(tax: Taxonomy) -> dict:
    out = dataclasses.asdict(tax)
    for domain_dict, group in zip(out["domains"], tax.domains):
        domain_dict["n_traces"] = group.n_traces
    return out


def render_markdown(tax: Taxonomy) -> str:
    out = [
        "# CommonTrace Taxonomy",
        "",
        "A structured map of the failure patterns CommonTrace can address, "
        "built from recurring traces (the pilot design step 1: \"Map the issues\").",
        "",
        f"- Traces considered: **{tax.n_traces_total}**",
        f"- Recurring patterns found: **{tax.n_patterns}**",
        f"- Already covered by an active lesson: **{tax.n_covered}**",
        f"- Gaps (recurring, no lesson yet): **{tax.n_gaps}**",
        f"- Traces that did not cluster with any other (below the "
        f"minimum pattern size): **{tax.n_unclustered}**",
        "",
    ]
    if not tax.domains:
        out.append(
            "*No repeated pattern found yet -- either there aren't enough traces, "
            "or none share enough vocabulary to cluster. This is not the same as "
            "\"no failures\"; see `commontrace distill --similarity-threshold` to "
            "loosen the match.*"
        )
        return "\n".join(out)

    for group in tax.domains:
        out.append(f"## {group.domain} ({group.n_traces} trace(s), {len(group.patterns)} pattern(s))")
        out.append("")
        out.append("| Pattern | Traces | Shared terms | Tags | Status |")
        out.append("|---|---|---|---|---|")
        for p in group.patterns:
            status = f"covered by `{p.lesson_slug}`" if p.covered else "**gap — no lesson yet**"
            terms = ", ".join(p.shared_terms[:5]) or "—"
            tags = ", ".join(p.tags) or "—"
            out.append(f"| {p.description} | {p.n_traces} | {terms} | {tags} | {status} |")
        out.append("")

    out.append(
        "---\n\n"
        "Gaps are candidates for `commontrace distill` (which writes them as "
        "`status: review` lessons for a human to approve) or manual "
        "`commontrace lesson new`. Coverage here means a pattern's traces are "
        "*referenced by* an active lesson's `source_traces` -- it says nothing "
        "about whether that lesson actually works; see `commontrace reliability` "
        "for that."
    )
    return "\n".join(out)


def render_html_fragment(tax: Taxonomy) -> str:
    cards = "".join([
        report_html.stat_card("Patterns found", str(tax.n_patterns)),
        report_html.stat_card("Covered", str(tax.n_covered), "have an active lesson"),
        report_html.stat_card("Gaps", str(tax.n_gaps), "recurring, no lesson yet"),
    ])
    rows = []
    for group in tax.domains:
        rows.append(f'<h3>{html.escape(group.domain)} '
                     f'<span class="meta">({group.n_traces} trace(s))</span></h3>')
        rows.append(
            "<table><tr><th>Pattern</th><th>Traces</th><th>Shared terms</th>"
            "<th>Tags</th><th>Status</th></tr>"
        )
        for p in group.patterns:
            badge = (
                f'<span class="badge badge-covered">covered · {html.escape(p.lesson_slug or "")}</span>'
                if p.covered else '<span class="badge badge-gap">gap</span>'
            )
            terms = html.escape(", ".join(p.shared_terms[:5]) or "—")
            tags = html.escape(", ".join(p.tags) or "—")
            rows.append(
                f"<tr><td>{html.escape(p.description)}</td><td>{p.n_traces}</td>"
                f"<td>{terms}</td><td>{tags}</td><td>{badge}</td></tr>"
            )
        rows.append("</table>")

    body = "\n".join(rows) if tax.domains else "<p>No repeated pattern found yet.</p>"
    return f"""<h1>Taxonomy</h1>
<p class="subtitle">A structured map of the failure patterns CommonTrace can address.</p>
<div class="cards">{cards}</div>
{body}
<p class="caveat">Coverage means a pattern's traces are referenced by an active lesson's
<code>source_traces</code> — it says nothing about whether that lesson actually works;
see <code>commontrace reliability</code> for that.</p>
"""


def render_html(tax: Taxonomy, timestamp: str) -> str:
    return report_html.wrap_page("CommonTrace Taxonomy", render_html_fragment(tax), timestamp)
