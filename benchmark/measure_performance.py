#!/usr/bin/env python3
"""measure_performance.py — memory benchmark for /commontrace.

Measures 3 axes identified as gaps in the state of the art (Park 2023 Generative
Agents, Evo-Memory, AgentErrorBench, ERL, LongMemEval, LoCoMo):

- lesson_quality     : % of Omega proposals validated by Lambda (generation quality)
- implicit_retrieval : % of lessons retrieved by Alpha that actually helped (retrieval quality)
- transfer_gap       : % of cross-project hits (transfer beyond originating project)

Usage:
    python measure_performance.py                  # markdown stdout, all episodes
    python measure_performance.py --n=5            # last 5 episodes
    python measure_performance.py --html           # HTML to memory/benchmark_reports/
    python measure_performance.py --json           # raw JSON to stdout
    python measure_performance.py --save           # persist JSON report to benchmark_reports/

Alert thresholds (warn when metrics fall below):
    --threshold-quality=0.8        lesson_quality warning below 80% (default)
    --threshold-retrieval=0.7      implicit_retrieval strict warning below 70% (default)
    --threshold-never-hit=0.25     warn if >25% of lessons are never-hit (default)

Falls back to regex parser if PyYAML is not installed.
"""
import argparse
import datetime
import glob
import html
import json
import os
import re
import sys

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

SCHEMA_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Path configuration — provider-agnostic
#
# Priority:
#   1. COMMONTRACE_ROOT env var (explicit override)
#   2. JUSTDOIT_ROOT env var (legacy backward compatibility)
#   3. Auto-detect from this script's location (works out of the box)
#
# Example: export COMMONTRACE_ROOT=/opt/commontrace
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTO_ROOT = os.path.dirname(_SCRIPT_DIR)  # benchmark → ROOT
_ROOT = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT") or _AUTO_ROOT
BASE_DIR = os.path.join(_ROOT, "memory")


def parse_frontmatter(content):
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    fm_text = parts[1]
    if HAS_YAML:
        try:
            return yaml.safe_load(fm_text)
        except yaml.YAMLError:
            return parse_yaml_minimal(fm_text)
    return parse_yaml_minimal(fm_text)


_KEY_RE = re.compile(r"^([A-Za-z_]\w*):\s*(.*)$")


def _strip_inline_comment(line):
    """Strip a trailing ' #comment', but not a '#' that's inside a quoted string."""
    in_squote = in_dquote = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_dquote:
            in_squote = not in_squote
        elif ch == '"' and not in_squote:
            in_dquote = not in_dquote
        elif ch == "#" and not in_squote and not in_dquote:
            if i == 0 or line[i - 1] in " \t":
                return line[:i].rstrip()
    return line


def _coerce_scalar(val):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        return val[1:-1]
    if val in ("null", "Null", "NULL", "~", ""):
        return None
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    if val == "{}":
        return {}
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_coerce_scalar(x.strip()) for x in inner.split(",")] if inner else []
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d+\.\d+", val):
        return float(val)
    return val


def _split_lines(text):
    """(indent, content) pairs for non-blank, non-full-line-comment lines, comments stripped."""
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        content = _strip_inline_comment(raw.rstrip()).strip()
        if not content:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        out.append((indent, content))
    return out


def _parse_value(lines, i, key_indent):
    """Parse the value following an empty-valued 'key:' line (lines[i] is the first
    candidate child line). PyYAML block-dumps a list under a mapping key at the SAME
    indent as the key itself (not indented further), while a nested mapping IS indented
    further -- branch on which shape actually follows.
    """
    if i < len(lines) and lines[i][0] >= key_indent and lines[i][1].startswith("- "):
        return _parse_block(lines, i, lines[i][0])
    return _parse_block(lines, i, key_indent + 1)


def _parse_block(lines, start, min_indent):
    """Parse a contiguous indentation block starting at lines[start] (indent >= min_indent)
    as either a YAML block list ('- item' / '- key: val') or block mapping ('key: val').
    Returns (value, next_index). Not a general YAML parser -- covers exactly the shapes
    templates.py's yaml.safe_dump emits for this project's frontmatter (which defaults to
    PyYAML's block style for every non-empty list/mapping, not inline '[a, b]').
    """
    if start >= len(lines) or lines[start][0] < min_indent:
        return None, start

    indent0 = lines[start][0]

    if lines[start][1].startswith("- "):
        result = []
        i = start
        while i < len(lines) and lines[i][0] == indent0 and lines[i][1].startswith("- "):
            rest = lines[i][1][2:]
            m = _KEY_RE.match(rest)
            if not m:
                result.append(_coerce_scalar(rest))
                i += 1
                continue
            item = {}
            field_indent = indent0 + 2  # column where "key:" starts, right after "- "
            key0, val0 = m.group(1), m.group(2).strip()
            if val0 == "":
                sub_val, i = _parse_value(lines, i + 1, field_indent)
                item[key0] = sub_val
            else:
                item[key0] = _coerce_scalar(val0)
                i += 1
            while i < len(lines) and lines[i][0] == field_indent:
                m2 = _KEY_RE.match(lines[i][1])
                if not m2:
                    break
                key, val = m2.group(1), m2.group(2).strip()
                if val == "":
                    sub_val, i = _parse_value(lines, i + 1, field_indent)
                    item[key] = sub_val
                else:
                    item[key] = _coerce_scalar(val)
                    i += 1
            result.append(item)
        return result, i

    result = {}
    i = start
    while i < len(lines) and lines[i][0] == indent0:
        m = _KEY_RE.match(lines[i][1])
        if not m:
            break
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            sub_val, i = _parse_value(lines, i + 1, indent0)
            result[key] = sub_val
        else:
            result[key] = _coerce_scalar(val)
            i += 1
    return result, i


def parse_yaml_minimal(text):
    """Fallback parser for our frontmatter format, used only when PyYAML isn't installed.

    Handles flat 'key: value' pairs, one or more levels of nested mapping ('key:' followed
    by more-indented 'subkey: value' lines -- e.g. Trace.outcome), block lists of scalars
    or of dicts (e.g. tags, importance_history -- PyYAML's default block style, not inline
    '[a, b]'), inline '[a, b]' lists, quoted strings, booleans/null, and trailing '# comment'
    stripping.
    """
    lines = _split_lines(text)
    value, _ = _parse_block(lines, 0, 0)
    return value if isinstance(value, dict) else {}


def load_episodes(n=None):
    paths = sorted(glob.glob(os.path.join(BASE_DIR, "episodes", "2*.md")))
    if n is not None and n > 0:
        paths = paths[-n:]
    episodes = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            fm = parse_frontmatter(fh.read())
        if fm:
            fm["_path"] = p
            episodes.append(fm)
    return episodes


def load_lessons():
    paths = sorted(glob.glob(os.path.join(BASE_DIR, "lessons", "lesson_*.md")))
    lessons = {}
    for p in paths:
        name = os.path.basename(p).replace(".md", "")
        if name.endswith("_template"):
            continue
        with open(p, encoding="utf-8") as fh:
            fm = parse_frontmatter(fh.read())
        if fm:
            fm["_path"] = p
            lessons[name] = fm
    return lessons


def get_validated(ep):
    """Get validated lessons, falling back to legacy field name for back-compat."""
    return ep.get("lessons_validated_by_lambda") or ep.get("lessons_validated_by_user") or []


def compute_lesson_quality(episodes):
    """Mean over episodes of (n_validated / n_proposed). Exclude episodes with no proposal."""
    ratios = []
    for ep in episodes:
        proposed = ep.get("lessons_proposed_by_omega") or []
        if not proposed:
            continue
        validated = get_validated(ep)
        ratios.append(len(validated) / len(proposed))
    if not ratios:
        return None, 0
    return sum(ratios) / len(ratios), len(ratios)


def compute_implicit_retrieval(episodes):
    """Two angles on retrieval quality:

    - strict     = mean(|hit ∩ retrieved| / |retrieved|)  → Alpha retrieval precision
                   (proportion of Alpha's selections that actually helped)
    - permissive = mean(|hit| / |retrieved|)               → Omega application richness
                   (can exceed 100% if Omega counts influential lessons beyond those
                    retrieved by Alpha — counter-examples, background methodological rules, etc.)

    Exclude episodes with empty retrieval. Returns (strict, permissive, n_valid).
    """
    strict_ratios = []
    permissive_ratios = []
    for ep in episodes:
        retrieved = set(ep.get("lessons_retrieved_by_alpha") or [])
        if not retrieved:
            continue
        hit = set(ep.get("lessons_hit") or [])
        strict_ratios.append(len(hit & retrieved) / len(retrieved))
        permissive_ratios.append(len(hit) / len(retrieved))
    if not strict_ratios:
        return None, None, 0
    return (
        sum(strict_ratios) / len(strict_ratios),
        sum(permissive_ratios) / len(permissive_ratios),
        len(strict_ratios),
    )


def compute_transfer_gap(episodes, lessons):
    """% of hits whose source_episodes are from a different project than current."""
    episode_project = {ep["name"]: ep.get("project") for ep in episodes}

    def resolve_project(slug):
        if slug in episode_project:
            return episode_project[slug]
        path = os.path.join(BASE_DIR, "episodes", f"{slug}.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                fm = parse_frontmatter(fh.read())
            return (fm or {}).get("project")
        return None

    total_hits = 0
    cross_hits = 0
    untraceable = 0
    for ep in episodes:
        current = ep.get("project")
        for hit_slug in ep.get("lessons_hit") or []:
            lesson = lessons.get(hit_slug)
            if not lesson:
                continue
            src_episodes = lesson.get("source_episodes") or []
            if not src_episodes:
                untraceable += 1
                continue
            src_projects = {resolve_project(s) for s in src_episodes}
            src_projects.discard(None)
            if not src_projects:
                untraceable += 1
                continue
            if current is None:
                # Current episode's own project is unknown -- can't tell same- vs.
                # cross-project, so this hit is untraceable rather than automatically
                # "cross-project" (None was never a real project value).
                untraceable += 1
                continue
            total_hits += 1
            if current not in src_projects:
                cross_hits += 1
    if total_hits == 0:
        return None, 0, untraceable
    return cross_hits / total_hits, total_hits, untraceable


def compute_extras(episodes, lessons):
    by_uses = sorted(lessons.items(), key=lambda kv: kv[1].get("uses", 0), reverse=True)
    top5 = [(n, l.get("uses", 0)) for n, l in by_uses[:5] if l.get("uses", 0) > 0]
    never_hit = sorted(n for n, l in lessons.items() if l.get("uses", 0) == 0)

    all_proposed = set()
    all_validated = set()
    for ep in episodes:
        all_proposed |= set(ep.get("lessons_proposed_by_omega") or [])
        all_validated |= set(get_validated(ep))
    proposed_not_validated = sorted(all_proposed - all_validated)

    # Distribution importance
    imp_lessons = {}
    for l in lessons.values():
        i = l.get("importance")
        imp_lessons[i] = imp_lessons.get(i, 0) + 1
    imp_episodes = {}
    for ep in episodes:
        i = ep.get("importance")
        imp_episodes[i] = imp_episodes.get(i, 0) + 1

    # Coverage by domain
    domain_coverage = {}
    for l in lessons.values():
        d = l.get("domain", "?")
        domain_coverage[d] = domain_coverage.get(d, 0) + 1

    return {
        "top5": top5,
        "never_hit": never_hit,
        "proposed_not_validated": proposed_not_validated,
        "importance_lessons": imp_lessons,
        "importance_episodes": imp_episodes,
        "domain_coverage": domain_coverage,
    }


def compute_alerts(report, thresholds):
    """Return list of alert strings when metrics breach thresholds."""
    alerts = []
    lq = report["lesson_quality"]
    if lq["value"] is not None and lq["value"] < thresholds["quality"]:
        alerts.append(
            f"lesson_quality {lq['value']:.1%} < threshold {thresholds['quality']:.1%} "
            f"— Omega proposal quality degraded"
        )
    ir = report["implicit_retrieval"]
    if ir["strict"] is not None and ir["strict"] < thresholds["retrieval"]:
        alerts.append(
            f"implicit_retrieval (strict) {ir['strict']:.1%} < threshold {thresholds['retrieval']:.1%} "
            f"— Alpha retrieval precision degraded"
        )
    extras = report["extras"]
    n_lessons = report["n_lessons"]
    if n_lessons > 0:
        never_ratio = len(extras["never_hit"]) / n_lessons
        if never_ratio > thresholds["never_hit"]:
            alerts.append(
                f"{len(extras['never_hit'])}/{n_lessons} lessons never hit "
                f"({never_ratio:.1%} > threshold {thresholds['never_hit']:.1%}) "
                f"— consider archiving stale lessons"
            )
    # Warn if lesson_quality > 100% (retro-validation artefact)
    if lq["value"] is not None and lq["value"] > 1.0:
        alerts.append(
            f"lesson_quality {lq['value']:.1%} > 100% — retro-validation artefact "
            f"(Lambda validated proposals from earlier runs; see STATUS.md §2.2)"
        )
    return alerts


def _importance_sort_key(x):
    """Sort importance keys (None / int / stray non-numeric garbage) without ever
    comparing across incompatible types -- e.g. `sorted([3, "high"])` raises TypeError.
    Groups: None first, then numbers (sorted numerically), then anything else (sorted
    as a string).
    """
    if x is None:
        return (0, 0, "")
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return (1, x, "")
    return (2, 0, str(x))


def fmt_pct(v):
    return "N/A" if v is None else f"{v:.1%}"


def render_markdown(r, alerts=None):
    out = []
    out.append("# /commontrace Memory Benchmark Report")
    out.append("")
    out.append(f"**Date** : {r['timestamp']}")
    out.append(f"**Schema version** : {r.get('schema_version', 'N/A')}")
    out.append(f"**Episodes analyzed** : {r['n_episodes']}")
    out.append(f"**Lessons in store** : {r['n_lessons']}")
    out.append(f"**YAML parser** : {'PyYAML' if HAS_YAML else 'regex fallback (PyYAML not found)'}")
    out.append("")

    if alerts:
        out.append("## Alerts")
        out.append("")
        for a in alerts:
            out.append(f"- **WARNING**: {a}")
        out.append("")

    out.append("## Main Metrics (3 axes)")
    out.append("")
    out.append("### lesson_quality")
    out.append("% of lessons proposed by Omega that were validated by Lambda.")
    lq = r["lesson_quality"]
    if lq["value"] is None:
        out.append("-> **N/A** (no episodes with Omega proposals)")
    else:
        out.append(f"-> **{fmt_pct(lq['value'])}** across {lq['n']} valid episodes")
    out.append("")

    out.append("### implicit_retrieval (2 angles)")
    out.append("Precision and richness of Alpha retrieval. `hit` is not bounded by `retrieved` —")
    out.append("see semantic doc (counter-examples / background rules can count as hits).")
    ir = r["implicit_retrieval"]
    if ir["strict"] is None:
        out.append("-> **N/A** (no episodes with non-empty Alpha retrieval)")
    else:
        out.append(f"- **strict** = mean(|hit ∩ retrieved| / |retrieved|) -> **{fmt_pct(ir['strict'])}**")
        out.append(f"  (precision: proportion of Alpha selections that actually helped)")
        out.append(f"- **permissive** = mean(|hit| / |retrieved|) -> **{fmt_pct(ir['permissive'])}**")
        out.append(f"  (richness: can exceed 100% if Omega counts hits beyond Alpha's retrieved set)")
        out.append(f"- across **{ir['n']}** valid episodes")
    out.append("")

    out.append("### transfer_gap")
    out.append("% of cross-project hits (transfer beyond originating project).")
    tg = r["transfer_gap"]
    if tg["value"] is None:
        msg = "-> **N/A**"
        if tg["untraceable"] > 0:
            msg += f" ({tg['untraceable']} hits untraceable — seeded lessons without source_episode)"
        msg += ". Single-project base or untraceable hits — seed with multi-project episodes to measure."
        out.append(msg)
    else:
        out.append(f"-> **{fmt_pct(tg['value'])}** across {tg['n']} traceable hits (+ {tg['untraceable']} untraceable)")
    out.append("")

    out.append("## Detail per episode")
    out.append("")
    out.append("| Date / slug | Verdict | Imp | Retrieved | Hit | Proposed | Validated | Project |")
    out.append("|---|---|---|---|---|---|---|---|")
    for ep in r["episodes"]:
        out.append(
            f"| `{ep['name']}` | {ep.get('verdict', '?')} | "
            f"{ep.get('importance', '?')} | "
            f"{len(ep.get('lessons_retrieved_by_alpha') or [])} | "
            f"{len(ep.get('lessons_hit') or [])} | "
            f"{len(ep.get('lessons_proposed_by_omega') or [])} | "
            f"{len(get_validated(ep))} | "
            f"{ep.get('project', '?')} |"
        )
    out.append("")

    extras = r["extras"]

    out.append("## Top 5 lessons by uses")
    out.append("")
    if extras["top5"]:
        for n, u in extras["top5"]:
            out.append(f"- `{n}` : {u} uses")
    else:
        out.append("*(no lessons with uses > 0)*")
    out.append("")

    out.append("## Lessons never hit (archival candidates)")
    out.append("")
    if extras["never_hit"]:
        for n in extras["never_hit"]:
            out.append(f"- `{n}`")
    else:
        out.append("*(all lessons have been hit at least once)*")
    out.append("")

    out.append("## Lessons proposed but never validated (Omega quality signal degraded)")
    out.append("")
    if extras["proposed_not_validated"]:
        for n in extras["proposed_not_validated"]:
            out.append(f"- `{n}`")
    else:
        out.append("*(all Omega proposals have been validated)*")
    out.append("")

    out.append("## Importance distribution")
    out.append("")
    out.append("**Lessons**:")
    for i in sorted(extras["importance_lessons"].keys(), key=_importance_sort_key):
        out.append(f"- importance {i} : {extras['importance_lessons'][i]} lessons")
    out.append("")
    out.append("**Episodes**:")
    for i in sorted(extras["importance_episodes"].keys(), key=_importance_sort_key):
        out.append(f"- importance {i} : {extras['importance_episodes'][i]} episodes")
    out.append("")

    out.append("## Coverage by domain")
    out.append("")
    for d in sorted(extras["domain_coverage"].keys()):
        out.append(f"- {d} : {extras['domain_coverage'][d]} lessons")
    out.append("")

    return "\n".join(out)


def _md_to_html_fragment(md_text):
    """Convert a subset of markdown to HTML (stdlib only, no external deps).

    Handles: ATX headers (# ## ###), bold (**text**), inline code (`code`),
    table rows (| col | col |), bullet lists (- item), horizontal rules (---),
    and plain paragraphs. Sufficient for the benchmark report format.
    """
    lines = md_text.split("\n")
    html_lines = []
    in_table = False
    in_list = False
    in_para = False

    def close_para():
        nonlocal in_para
        if in_para:
            html_lines.append("</p>")
            in_para = False

    def close_list():
        nonlocal in_list
        if in_list:
            html_lines.append("</ul>")
            in_list = False

    def close_table():
        nonlocal in_table
        if in_table:
            html_lines.append("</tbody></table>")
            in_table = False

    def inline(text):
        # Escape raw content FIRST so arbitrary frontmatter text (project names, lesson
        # titles, etc.) containing <, >, or & can't inject markup into the report --
        # markdown syntax chars (*, `, _) aren't HTML-special so escaping first is safe.
        text = html.escape(text, quote=False)
        # bold **...** or __...__
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
        # inline code `...`
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        # italic *...* (single asterisk, after bold handled)
        text = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", text)
        return text

    i = 0
    while i < len(lines):
        line = lines[i]

        # ATX headers
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            close_list()
            close_table()
            close_para()
            level = len(m.group(1))
            html_lines.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^---+\s*$", line) or re.match(r"^\*\*\*+\s*$", line):
            close_list()
            close_table()
            close_para()
            html_lines.append("<hr>")
            i += 1
            continue

        # Bullet list
        m = re.match(r"^[-*]\s+(.*)", line)
        if m:
            close_table()
            close_para()
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{inline(m.group(1))}</li>")
            i += 1
            continue

        # Table row
        if line.startswith("|"):
            close_list()
            close_para()
            cells = [c.strip() for c in line.strip("|").split("|")]
            # Skip separator rows like |---|---|
            if all(re.match(r"^[-:]+$", c) for c in cells if c):
                i += 1
                continue
            if not in_table:
                html_lines.append('<table><thead><tr>')
                for c in cells:
                    html_lines.append(f"<th>{inline(c)}</th>")
                html_lines.append("</tr></thead><tbody>")
                in_table = True
            else:
                html_lines.append("<tr>")
                for c in cells:
                    html_lines.append(f"<td>{inline(c)}</td>")
                html_lines.append("</tr>")
            i += 1
            continue

        # Empty line
        if not line.strip():
            close_list()
            close_table()
            close_para()
            html_lines.append("")
            i += 1
            continue

        # Plain paragraph text
        close_list()
        close_table()
        if not in_para:
            html_lines.append("<p>")
            in_para = True
        else:
            # Preserve line breaks within a paragraph (e.g. consecutive metadata lines)
            html_lines.append("<br>")
        html_lines.append(inline(line))
        i += 1

    close_list()
    close_table()
    close_para()
    return "\n".join(html_lines)


def render_html(md_content, timestamp, alerts=None):
    alert_html = ""
    if alerts:
        items = "\n".join(f"<li>{a}</li>" for a in alerts)
        alert_html = f'<div class="alerts"><h2>Alerts</h2><ul>{items}</ul></div>'
    # Strip the markdown "## Alerts" section so we don't duplicate the banner.
    body_md = re.sub(
        r"^## Alerts\s*\n(\n|\s)*" r"((- \*\*WARNING\*\*:.*\n)+)",
        "",
        md_content,
        flags=re.MULTILINE,
    )
    body_html = _md_to_html_fragment(body_md)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>/commontrace Memory Benchmark — {timestamp}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1.5em; line-height: 1.6; color: #2d2d2d; }}
h1, h2, h3 {{ color: #1a1a1a; }}
h1 {{ border-bottom: 2px solid #444; padding-bottom: 0.3em; }}
h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 0.2em; margin-top: 2em; }}
h3 {{ margin-top: 1.5em; color: #444; }}
code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; font-family: "SF Mono", Menlo, Consolas, monospace; }}
table {{ border-collapse: collapse; margin: 1em 0; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 8px 12px; text-align: left; }}
th {{ background: #f0f0f0; }}
strong {{ color: #0066cc; }}
em {{ color: #888; font-style: italic; }}
ul {{ padding-left: 1.5em; }}
li {{ margin-bottom: 0.3em; }}
hr {{ border: none; border-top: 1px solid #ddd; margin: 1.5em 0; }}
.alerts {{ background: #fff8e1; border: 1px solid #f0c000; border-radius: 6px; padding: 1em 1.5em; margin: 1em 0; }}
.alerts h2 {{ color: #b07000; border-bottom: none; }}
.alerts li {{ color: #7a5000; }}
</style>
</head>
<body>
{alert_html}
{body_html}
</body>
</html>"""


def main():
    # Report text uses non-ASCII characters (—, ∩, →); Windows consoles default
    # stdout/stderr to the system codepage (e.g. cp1252), which raises
    # UnicodeEncodeError on print(). Force UTF-8 output where supported
    # (Python 3.7+); no-op on platforms already using a UTF-8 locale.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Memory benchmark for /commontrace (lesson_quality, implicit_retrieval, transfer_gap)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("--n", type=int, default=0, help="Number of recent episodes (default: all)")
    parser.add_argument("--html", action="store_true", help="Output HTML to memory/benchmark_reports/")
    parser.add_argument("--json", action="store_true", help="Raw JSON output to stdout")
    parser.add_argument("--save", action="store_true", help="Persist JSON report to memory/benchmark_reports/")
    parser.add_argument(
        "--threshold-quality", type=float, default=0.8,
        metavar="FLOAT",
        help="lesson_quality alert threshold (default: 0.8)",
    )
    parser.add_argument(
        "--threshold-retrieval", type=float, default=0.7,
        metavar="FLOAT",
        help="implicit_retrieval strict alert threshold (default: 0.7)",
    )
    parser.add_argument(
        "--threshold-never-hit", type=float, default=0.25,
        metavar="FLOAT",
        help="Never-hit lesson ratio alert threshold (default: 0.25)",
    )
    args = parser.parse_args()
    if args.json and args.html:
        parser.error("--json and --html are mutually exclusive (choose one output format).")

    episodes = load_episodes(args.n if args.n > 0 else None)
    lessons = load_lessons()

    if not episodes:
        print("Not enough episodes to compute. Run /commontrace a few times first.")
        sys.exit(0)

    lq_value, lq_n = compute_lesson_quality(episodes)
    ir_strict, ir_permissive, ir_n = compute_implicit_retrieval(episodes)
    tg_value, tg_n, tg_untraceable = compute_transfer_gap(episodes, lessons)
    extras = compute_extras(episodes, lessons)

    report = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_episodes": len(episodes),
        "n_lessons": len(lessons),
        "lesson_quality": {"value": lq_value, "n": lq_n},
        "implicit_retrieval": {"strict": ir_strict, "permissive": ir_permissive, "n": ir_n},
        "transfer_gap": {"value": tg_value, "n": tg_n, "untraceable": tg_untraceable},
        "episodes": episodes,
        "extras": extras,
    }

    thresholds = {
        "quality": args.threshold_quality,
        "retrieval": args.threshold_retrieval,
        "never_hit": args.threshold_never_hit,
    }
    alerts = compute_alerts(report, thresholds)

    clean_report = dict(report)
    clean_report["episodes"] = [{k: v for k, v in ep.items() if not k.startswith("_")} for ep in episodes]
    clean_report["alerts"] = alerts

    if args.json:
        print(json.dumps(clean_report, indent=2, default=str))
    elif args.html:
        md = render_markdown(report, alerts)
        html_report = render_html(md, report["timestamp"], alerts)
        out_dir = os.path.join(BASE_DIR, "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = os.path.join(out_dir, f"{ts}.html")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(html_report)
        print(f"HTML report written: {out_path}")
        if alerts:
            print("\nAlerts:")
            for a in alerts:
                print(f"  WARNING: {a}")
    else:
        md = render_markdown(report, alerts)
        print(md)

    if args.save:
        out_dir = os.path.join(BASE_DIR, "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        json_path = os.path.join(out_dir, f"{ts}.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(clean_report, fh, indent=2, default=str)
        print(f"JSON report saved: {json_path}", file=sys.stderr)

    # Non-zero exit when alerts fire (after all output is flushed)
    if alerts and not args.json:
        sys.exit(2)


if __name__ == "__main__":
    main()
