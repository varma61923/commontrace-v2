"""`commontrace taxonomy` — "Map the issues": group recurring failures into a
clear, structured taxonomy.

This is the first of the pilot's three leave-behinds (see PILOT.md and
STRATEGY.md's outcome-metrics framing): "a structured map of the failure
patterns CommonTrace can address."

It reuses commontrace/distill.py's clustering (word-overlap Jaccard over
title+context, no LLM call) rather than reimplementing pattern-finding —
that module already IS "group recurring failures by similarity", and a
second clustering algorithm here would just be a second thing to keep in
sync with it. The difference from `commontrace distill` is intent and side
effects: `distill` is the Curator step and writes new candidate lesson
files at status=review; `taxonomy` is read-only reporting -- it never
writes anything, and (unlike `distill`) it deliberately does NOT exclude
traces already covered by an existing lesson, because a pilot needs to see
the whole map -- what CommonTrace already addresses and what it does not --
not just the unaddressed remainder.
"""
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


def _best_covering_lesson(trace_ids: set[str], lessons: list[dict]) -> str | None:
    """The active lesson whose source_traces overlaps this pattern's traces
    the most, or None if no active lesson references any of them.

    Ties broken by lesson name for determinism -- an arbitrary dict-order
    pick would make the reported coverage flicker between otherwise-identical
    runs.
    """
    best_slug = None
    best_overlap = 0
    for fm in sorted(lessons, key=lambda lesson: str(lesson.get("name", ""))):
        if fm.get("status") != "active":
            continue
        # A lesson that is still unedited scaffolding covers nothing, and
        # counting it is worse than counting nothing: this number is the
        # "Already covered by an active lesson" line in `commontrace
        # taxonomy` and `commontrace pilot`, i.e. the number a customer
        # reads to decide which failure patterns still need work.
        # Reproduced before this guard: an all-"TODO:" candidate reported a
        # real recurring pattern as covered and drove the pilot report's
        # "Gaps: 0", telling the customer there was nothing left to do.
        if templates.unfilled_placeholders(fm, str(fm.get(templates.BODY_KEY) or "")):
            continue
        source = set(str(t) for t in (fm.get("source_traces") or []))
        overlap = len(source & trace_ids)
        if overlap > best_overlap:
            best_overlap = overlap
            best_slug = str(fm.get("name", "")) or None
    return best_slug


def build_taxonomy(
    traces: list[distill.TraceCandidate],
    lessons: list[dict],
    similarity_threshold: float = 0.3,
    min_cluster_size: int = 2,
) -> Taxonomy:
    clusters = distill.find_clusters(
        traces,
        existing_lessons_source_traces=[],  # taxonomy maps everything, covered or not
        similarity_threshold=similarity_threshold,
        min_cluster_size=min_cluster_size,
    )

    by_domain: dict[str, DomainGroup] = {}
    n_covered = 0
    clustered_ids: set[str] = set()

    for cluster in clusters:
        trace_ids = {t.id for t in cluster.traces}
        clustered_ids |= trace_ids
        agent_type = cluster.traces[0].agent_type or "custom"
        lesson_slug = _best_covering_lesson(trace_ids, lessons)
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
    """dataclasses.asdict() skips DomainGroup.n_traces (a computed property,
    not a field) -- this includes it, since it's the number the markdown/HTML
    renderers lead with and JSON consumers should not have to re-derive it."""
    out = dataclasses.asdict(tax)
    for domain_dict, group in zip(out["domains"], tax.domains):
        domain_dict["n_traces"] = group.n_traces
    return out


def render_markdown(tax: Taxonomy) -> str:
    out = [
        "# CommonTrace Taxonomy",
        "",
        "A structured map of the failure patterns CommonTrace can address, "
        "built from recurring traces (PILOT.md step 1: \"Map the issues\").",
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
