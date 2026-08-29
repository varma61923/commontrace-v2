"""Shared HTML page shell for the taxonomy/impact/pilot reports.

Deliberately not a general markdown-to-HTML converter (that already exists
in commontrace/reference/measure_performance.py, for the subprocess-run
benchmark scripts) -- these three reports render structured data (stat
cards, grouped tables) that reads better as hand-built HTML than as
markdown pushed through a generic renderer, and they are in-process modules
that should not take a dependency on the reference/ scripts (those ship
standalone, runnable with nothing but PyYAML, and are meant to work without
the rest of the package installed).

Every value interpolated into a fragment built on top of this module must
go through html.escape() at the call site -- trace titles, tags and lesson
slugs are file content, not code, and are exactly as attacker-controllable
as the frontmatter values measure_performance.py already escapes for the
same reason.
"""
from __future__ import annotations

PAGE_CSS = """
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  max-width: 1080px; margin: 2em auto; padding: 0 1.5em; line-height: 1.6;
  color: #1a2233; background: #fff;
}
h1, h2, h3 { color: #0d1b2a; }
h1 { border-bottom: 2px solid #1a2233; padding-bottom: 0.3em; }
h2 { border-bottom: 1px solid #d8dee6; padding-bottom: 0.2em; margin-top: 2em; }
.subtitle { color: #5b6b82; margin-top: -0.5em; }
.meta { color: #5b6b82; font-size: 0.9em; }
.cards { display: flex; flex-wrap: wrap; gap: 1em; margin: 1.5em 0; }
.card {
  flex: 1 1 220px; border: 1px solid #d7ecd9; background: #f3faf4;
  border-radius: 10px; padding: 1em 1.2em;
}
.card .label { text-transform: uppercase; font-size: 0.75em; letter-spacing: 0.04em;
  color: #2f8f4e; font-weight: 600; }
.card .value { font-size: 2.2em; font-weight: 700; color: #0d1b2a; margin: 0.1em 0; }
.card .note { color: #5b6b82; font-size: 0.85em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d8dee6; padding: 0.5em 0.7em; text-align: left; font-size: 0.92em; }
th { background: #f4f6f9; }
.badge { display: inline-block; border-radius: 6px; padding: 0.15em 0.6em; font-size: 0.8em; font-weight: 600; }
.badge-covered { background: #e4f6e8; color: #1f7a3d; }
.badge-gap { background: #fdecec; color: #a3312a; }
.result-banner { border-radius: 10px; padding: 1.2em 1.4em; margin: 1.5em 0; font-size: 1.05em; }
.result-yes { background: #e4f6e8; border: 1px solid #bfe6c8; color: #1f7a3d; }
.result-no { background: #fdecec; border: 1px solid #f3c9c6; color: #a3312a; }
.result-unknown { background: #fdf6e3; border: 1px solid #f0e2ad; color: #8a6d1d; }
.caveat { color: #5b6b82; font-size: 0.88em; border-left: 3px solid #d8dee6; padding-left: 0.8em; margin: 1em 0; }
code { background: #f4f6f9; padding: 1px 5px; border-radius: 4px; font-size: 0.9em; }
"""


def wrap_page(title: str, body_html: str, timestamp: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title} — {timestamp}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
{body_html}
</body>
</html>
"""


def stat_card(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="note">{note}</div>' if note else ""
    return f"""<div class="card">
  <div class="label">{label}</div>
  <div class="value">{value}</div>
  {note_html}
</div>"""
