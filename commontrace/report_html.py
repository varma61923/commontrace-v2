"""Shared HTML page shell for the taxonomy/impact/pilot reports.

Deliberately not a general markdown-to-HTML converter (that already exists
in commontrace/reference/measure_performance.py, for the subprocess-run
benchmark scripts) -- these three reports render structured data (stat
cards, grouped tables) that reads better as hand-built HTML than as
markdown pushed through a generic renderer, and they are in-process modules
that should not take a dependency on the reference/ scripts (those ship
standalone, runnable with nothing but PyYAML, and are meant to work without
the rest of the package installed).

Escaping is done INSIDE this module (wrap_page title/timestamp, stat_card
label/value/note): trace titles, tags and lesson slugs are file content,
not code, and are exactly as attacker-controllable as the frontmatter
values measure_performance.py already escapes. Callers must still escape
values they interpolate into their own fragments directly.
"""
from __future__ import annotations

import html

PAGE_CSS = """
:root {
  color-scheme: light dark;
  --ink: #1a2233; --head: #0d1b2a; --muted: #5b6b82; --paper: #fff; --rule: #d8dee6; --soft: #f4f6f9;
  --ok: #1f7a3d; --ok-bg: #e4f6e8; --ok-rule: #bfe6c8; --bad: #a3312a; --bad-bg: #fdecec; --bad-rule: #f3c9c6;
  --warn: #8a6d1d; --warn-bg: #fdf6e3; --warn-rule: #f0e2ad; --card-bg: #f3faf4; --card-rule: #d7ecd9;
}
@media (prefers-color-scheme: dark) { :root {
  --ink: #e2e8f0; --head: #f1f5f9; --muted: #9aa8b8; --paper: #0f141a; --rule: #2a3440; --soft: #18202a;
  --ok: #6fcf8f; --ok-bg: #12261a; --ok-rule: #245436; --bad: #f08a80; --bad-bg: #2a1614; --bad-rule: #5a2a25;
  --warn: #e3c16b; --warn-bg: #2a2414; --warn-rule: #5a4a22; --card-bg: #121c16; --card-rule: #22382a;
} }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  max-width: 1080px; margin: 2em auto; padding: 0 1em; line-height: 1.6;
  color: var(--ink); background: var(--paper);
}
h1, h2, h3 { color: var(--head); }
h1 { border-bottom: 2px solid var(--ink); padding-bottom: 0.3em; }
h2 { border-bottom: 1px solid var(--rule); padding-bottom: 0.2em; margin-top: 2em; }
.subtitle { color: var(--muted); margin-top: -0.5em; }
.meta { color: var(--muted); font-size: 0.9em; }
.cards { display: flex; flex-wrap: wrap; gap: 1em; margin: 1.5em 0; }
.card {
  flex: 1 1 220px; border: 1px solid var(--card-rule); background: var(--card-bg);
  border-radius: 10px; padding: 1em 1.2em;
}
.card .label { text-transform: uppercase; font-size: 0.75em; letter-spacing: 0.04em;
  color: var(--ok); font-weight: 600; }
.card .value { font-size: 2.2em; font-weight: 700; color: var(--head); margin: 0.1em 0; overflow-wrap: anywhere; }
.card .note { color: var(--muted); font-size: 0.85em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid var(--rule); padding: 0.5em 0.7em; text-align: left; font-size: 0.92em; }
th { background: var(--soft); }
td { overflow-wrap: anywhere; }
@media (max-width: 640px) { table { display: block; overflow-x: auto; } }
.badge { display: inline-block; border-radius: 6px; padding: 0.15em 0.6em; font-size: 0.8em; font-weight: 600; }
.badge-covered { background: var(--ok-bg); color: var(--ok); }
.badge-gap { background: var(--bad-bg); color: var(--bad); }
.result-banner { border-radius: 10px; padding: 1.2em 1.4em; margin: 1.5em 0; font-size: 1.05em; }
.result-yes { background: var(--ok-bg); border: 1px solid var(--ok-rule); color: var(--ok); }
.result-no { background: var(--bad-bg); border: 1px solid var(--bad-rule); color: var(--bad); }
.result-unknown { background: var(--warn-bg); border: 1px solid var(--warn-rule); color: var(--warn); }
.caveat { color: var(--muted); font-size: 0.88em; border-left: 3px solid var(--rule);
  padding-left: 0.8em; margin: 1em 0; }
code { background: var(--soft); padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }
"""


def wrap_page(title: str, body_html: str, timestamp: str) -> str:
    # Escape by default: title/timestamp are often lesson/trace-derived file
    # content, and a caller that forgets html.escape() would otherwise ship
    # stored XSS in a saved report. body_html stays raw (it is this module's
    # own fragments), so escape values before composing them into it -- or
    # use stat_card/table helpers below, which escape their inputs.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — {html.escape(timestamp)}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
{body_html}
</body>
</html>
"""


def stat_card(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    return f"""<div class="card">
  <div class="label">{html.escape(label)}</div>
  <div class="value">{html.escape(value)}</div>
  {note_html}
</div>"""
