"""One self-contained HTML page for a proof package: the thing you send to a buyer."""
from __future__ import annotations

import html

from commontrace import report_html

_CSS = """
.pos { color: var(--ok); } .neg { color: var(--bad); } .zero { color: var(--muted); }
.banner { border-radius: 10px; padding: .9em 1.2em; margin: 1.2em 0; font-weight: 600; }
.forest { list-style: none; padding: 0; margin: 1em 0; }
.forest li { padding: .6em 0; border-top: 1px solid var(--rule); }
.forest li:first-child { border-top: 0; }
.f-head { display: flex; flex-wrap: wrap; gap: .25em .9em; align-items: baseline; }
.f-name { font-family: ui-monospace, Menlo, monospace; font-weight: 600; overflow-wrap: anywhere; }
.f-num { color: var(--muted); font-variant-numeric: tabular-nums; }
.f-svg { display: block; width: 100%; height: 34px; }
.f-svg .grid { stroke: var(--rule); } .f-svg .zero-line { stroke: var(--muted); stroke-dasharray: 4 3; }
.f-svg .ci { stroke-width: 3; stroke-linecap: round; }
.f-svg .dot { stroke: var(--paper); stroke-width: 2; }
.f-svg .dot.hollow { fill: var(--paper); stroke: var(--muted); stroke-width: 3; }
.cards .card { flex: 1 1 150px; }
.c-pos { stroke: var(--ok); fill: var(--ok); } .c-neg { stroke: var(--bad); fill: var(--bad); }
.c-zero { stroke: var(--muted); fill: var(--muted); }
.f-axis { display: flex; justify-content: space-between; color: var(--muted); font-size: .8em; padding-top: .2em; }
.mono { font-family: ui-monospace, Menlo, monospace; overflow-wrap: anywhere; }
@media print { body { max-width: none; } }
"""

_VERDICT = {
    "HELPS": ("pos", "▲", "Helps", "c-pos"), "HURTS": ("neg", "▼", "Hurts", "c-neg"),
    "NO_MEASURABLE_EFFECT": ("zero", "●", "No measurable effect", "c-zero"),
    "UNDERPOWERED": ("zero", "○", "Not yet decided", "c-zero"),
}


def _esc(x: object) -> str:
    return html.escape(str(x))


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:+.1%}"


def _extent(memories: list[dict]) -> float:
    widest = max([abs(m["ci_low"]) for m in memories] + [abs(m["ci_high"]) for m in memories] + [0.1])
    for step in (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 1.0):
        if widest <= step:
            return step
    return 1.0


def _measured(m: dict) -> bool:
    return not (m["ci_low"] == 0 and m["ci_high"] == 0 and m["verdict"] == "UNDERPOWERED")


def _forest_row(m: dict, extent: float) -> str:
    width, pad, mid = 600, 12, 17
    def x(v: float) -> float:
        return pad + ((max(-extent, min(extent, v)) + extent) / (2 * extent)) * (width - 2 * pad)
    cls = _VERDICT.get(m["verdict"], _VERDICT["UNDERPOWERED"])[3]
    hollow = " hollow" if m["verdict"] == "UNDERPOWERED" else ""
    label = (f"{m['lesson_slug']}: effect {_pct(m['effect'])}, 95% interval {_pct(m['ci_low'])} "
             f"to {_pct(m['ci_high'])}" if _measured(m) else f"{m['lesson_slug']}: not enough data yet")
    return (f'<svg class="f-svg" viewBox="0 0 {width} 34" role="img" aria-label="{_esc(label)}">'
            f'<line class="grid" x1="{x(-extent / 2):.1f}" x2="{x(-extent / 2):.1f}" y1="4" y2="30"/>'
            f'<line class="grid" x1="{x(extent / 2):.1f}" x2="{x(extent / 2):.1f}" y1="4" y2="30"/>'
            f'<line class="zero-line" x1="{x(0):.1f}" x2="{x(0):.1f}" y1="2" y2="32"/>'
            f'<line class="ci {cls}" x1="{x(m["ci_low"]):.1f}" x2="{x(m["ci_high"]):.1f}" y1="{mid}" y2="{mid}"/>'
            f'<circle class="dot {cls}{hollow}" cx="{x(m["effect"]):.1f}" cy="{mid}" r="6.5"/></svg>')


def _forest(memories: list[dict]) -> str:
    extent = _extent(memories)
    rows = []
    for m in sorted(memories, key=lambda r: r["effect"]):
        cls, icon, label, _ = _VERDICT.get(m["verdict"], _VERDICT["UNDERPOWERED"])
        numbers = (f"{_pct(m['effect'])} ({_pct(m['ci_low'])} to {_pct(m['ci_high'])})" if _measured(m)
                   else f"not enough data yet: {m['n_injected']} with, {m['n_withheld']} without")
        rows.append(f'<li><div class="f-head"><span class="f-name">{_esc(m["lesson_slug"])}</span>'
                    f'<strong class="{cls}"><span aria-hidden="true">{icon}</span> {label}</strong>'
                    f'<span class="f-num">{numbers}</span></div>{_forest_row(m, extent)}</li>')
    ticks = "".join(f"<span>{'+' if v > 0 else ''}{round(v * 100)}%</span>"
                    for v in (-extent, -extent / 2, 0, extent / 2, extent))
    return (f'<ul class="forest">{"".join(rows)}</ul><div class="f-axis" aria-hidden="true">{ticks}</div>'
            '<p class="caveat">Right of zero: outcomes were better with the memory. Left: worse. '
            "Bars are 95% intervals.</p>")


def _table(memories: list[dict]) -> str:
    head = "".join(f"<th>{h}</th>" for h in ("Memory", "Verdict", "Effect", "95% interval", "With", "Without"))
    body = ""
    for m in memories:
        _cls, icon, label, _ = _VERDICT.get(m["verdict"], _VERDICT["UNDERPOWERED"])
        effect = _pct(m["effect"]) if _measured(m) else "–"
        interval = f"{_pct(m['ci_low'])} to {_pct(m['ci_high'])}" if _measured(m) else "–"
        body += (f'<tr><td class="mono">{_esc(m["lesson_slug"])}</td><td>{icon} {label}</td>'
                 f"<td>{effect}</td><td>{interval}</td>"
                 f'<td>{_esc(m["n_injected"])}</td><td>{_esc(m["n_withheld"])}</td></tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _banners(record: dict) -> str:
    out = []
    if record["synthetic"]:
        out.append('<div class="banner result-unknown">Synthetic demo data. Outcomes were drawn by the tool '
                   "from planted effects so this page can be shown before there is real data. It is not a "
                   "customer measurement.</div>")
    if record["integrity"]["verdict"] == "COMPROMISED":
        out.append('<div class="banner result-no">The experiment cannot be trusted as it stands, so no '
                   "effect is stated. The findings below name what went wrong.</div>")
    elif not record["final"]:
        out.append('<div class="banner result-unknown">Interim. The planned sample has not been reached, '
                   "so this can still change.</div>")
    return "".join(out)


def render(record: dict) -> str:
    """The page for one `proof.json` record."""
    kit = record["design"]["kit"]
    memories = record["memories"]
    readable = record["integrity"]["readable"]
    helps = sum(m["verdict"] == "HELPS" for m in memories)
    hurts = sum(m["verdict"] == "HURTS" for m in memories)
    cards = "".join([
        report_html.stat_card("Integrity", record["integrity"]["verdict"].title(),
                              "Whether the comparison can be trusted"),
        report_html.stat_card("Memories helping", str(helps) if readable else "–"),
        report_html.stat_card("Memories hurting", str(hurts) if readable else "–"),
        report_html.stat_card("Occasions measured", f"{record['evidence']['occasions']:,}"),
    ])
    parts = [f"<h1>Agent Learning Proof: {_esc(record['label'])}</h1>",
             f'<p class="subtitle">{_esc(kit["title"])}. One occasion is one {_esc(kit["occasion"]["label"])}. '
             f'Success: {_esc(kit["outcome"]["success"])}.</p>',
             _banners(record), f'<div class="cards">{cards}</div>']
    if readable and memories:
        parts += ["<h2>What each memory did</h2>", _forest(memories), _table(memories)]
    elif not readable:
        bad = [f for f in record["integrity"]["findings"] if f.get("severity") != "ok"]
        items = "".join(f"<li><strong>{_esc(f['headline'])}</strong> {_esc(f.get('detail', ''))}</li>" for f in bad)
        parts += [f"<h2>Why there is no figure</h2><ul>{items}</ul>"]
    harmful = record["harmful"]
    if readable and harmful["memories"]:
        names = ", ".join(_esc(s) for s in harmful["memories"])
        action = ("The store's policy is to withdraw them, so they are no longer delivered."
                  if harmful["withdrawn_automatically"] else
                  "The store's policy is to inform only, so they are still being delivered.")
        parts.append(f"<h2>Harmful memories</h2><p>These made outcomes worse: {names}. {action}</p>")
        rec = harmful.get("recoverable")
        if rec:
            lo, hi = rec["ci_95"]
            parts.append(f"<p>Over the measured window they cost about <strong>{rec['occasions']:,.0f} "
                         f"occasions</strong> (95% interval {lo:,.0f} to {hi:,.0f}). That is what stopping "
                         "them gives back: measured, not forecast, and not billed.</p>")
    value = record["value"]
    if value["readable"] and value["occasions_improved"] is not None:
        lo, hi = value["ci_95"]
        money = f", worth {value['money']:,.2f}" if value["money"] is not None else ""
        parts.append(f"<h2>Value</h2><p>{value['occasions_improved']:+,.1f} occasions improved{money} "
                     f"(95% interval {lo:+,.1f} to {hi:+,.1f}).</p>")
    if record["ledger"]:
        rows = "".join(f"<tr><td>{_esc(e['index'])}</td><td class='mono'>{_esc(e['slug'])}</td>"
                       f"<td>{_esc(e['verdict'])}</td><td>{e['occasions_improved']:+,.1f}</td>"
                       f"<td>{e['money']:,.2f}</td></tr>" for e in record["ledger"])
        parts.append("<h2>Ledger</h2><table><thead><tr><th>#</th><th>Memory</th><th>Verdict</th>"
                     f"<th>Occasions improved</th><th>Amount</th></tr></thead><tbody>{rows}</tbody></table>")
    pre = ("Registered before the run and run as registered." if record["preregistration_clean"]
           else "Registered, with deviations: " + "; ".join(_esc(d) for d in record["deviations"]))
    signature = ("Signed by the issuer over the ledger root, the data digest and the pre-registration "
                 f"fingerprint ({_esc(record['org_id'])}, {_esc(record['audited_at'])})."
                 if record["signature"] else
                 "Not signed. The hash chain and data digest still check; without an issuer signature "
                 "someone with write access could replace the whole package.")
    parts.append(
        "<h2>Check it yourself</h2>"
        f"<p>{_esc(pre)}</p><p>{signature}</p>"
        f'<p>Ledger root <span class="mono">{_esc(record["ledger_root"])}</span><br>'
        f'Data digest <span class="mono">{_esc(record["evidence"]["digest"])}</span><br>'
        f'Pre-registration <span class="mono">{_esc(record["preregistration_fingerprint"])}</span></p>'
        "<p>The raw rows are in the CSV beside this page, one per arm decision including those that never "
        "got an outcome. <code>commontrace proof verify &lt;directory&gt;</code> recomputes the audit, every "
        "estimate and the value arithmetic from them alone.</p>"
        '<p class="caveat">This does not show that the agent honoured a memory it was told to withhold, '
        "that outcomes were reported honestly, or that the registration predates the data beyond what the "
        "assignment log's own timestamps say. A signature says who issued the package, not that they were "
        "honest about what they ran.</p>")
    page = report_html.wrap_page(f"Agent Learning Proof: {record['label']}", "\n".join(parts),
                                 record["audited_at"][:10])
    return page.replace("</style>", _CSS + "</style>", 1)
