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
    python measure_performance.py --no-save        # skip persisting JSON (persisted by default)
    python measure_performance.py --diff           # compare the 2 most recent stored runs
    python measure_performance.py --history        # table of stored runs over time
    python measure_performance.py --strict         # non-zero exit if any alert fires

Every invocation (other than --diff/--history, which only read existing history) persists
its JSON report to memory/benchmark_reports/YYYY-MM-DD_HHMMSS.json by default -- pass
--no-save to skip this (e.g. for a scratch/read-only invocation).

Alert thresholds (warn when metrics breach; see STATUS.md §5 P4):
    --threshold-quality=0.7        lesson_quality warning below 70% (default)
    --threshold-retrieval=0.5      implicit_retrieval strict warning below 50% (default)
    --threshold-never-hit=0.3      warn if >30% of lessons are never-hit (default)
    --threshold-unimodal=0.95      warn if >=95% of lessons sit at one importance level (default)

By default alerts are informational only (exit 0). Pass --strict to exit non-zero (2) when
any alert fires (including from --diff, when a metric moves more than 5 points).

Falls back to regex parser if PyYAML is not installed.
"""
import argparse
import datetime
import glob
import html
import json
import math
import os
import re
import sys

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# Bumped from 1.1.0: additive-only fields `operational_cost` and `semantic_duplicates`
# (Phase 3, P5 / P8). No existing key was removed or renamed.
SCHEMA_VERSION = "1.2.0"

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
_AUTO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))  # reference -> commontrace -> ROOT
_ROOT = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT") or _AUTO_ROOT
BASE_DIR = os.path.join(_ROOT, "memory")


# These are functions rather than frozen constants so that reassigning the module-level
# BASE_DIR (as tests do, e.g. `bm.BASE_DIR = str(tmp_memory)`) is picked up by every path
# that derives from it -- a constant computed once at import time would silently keep
# pointing at the real repo's memory/ even after a test repoints BASE_DIR.
def _reports_dir():
    return os.path.join(BASE_DIR, "benchmark_reports")


def _telemetry_path():
    return os.path.join(BASE_DIR, "alpha_telemetry.jsonl")


def _attention_index_path():
    return os.path.join(BASE_DIR, "attention", "index.npz")


DEFAULT_THRESHOLD_QUALITY = 0.7
DEFAULT_THRESHOLD_RETRIEVAL = 0.5
DEFAULT_THRESHOLD_NEVER_HIT = 0.3
DEFAULT_THRESHOLD_UNIMODAL = 0.95
DIFF_FLAG_DELTA = 0.05  # 5 percentage points
SEMANTIC_DUP_THRESHOLD = 0.85


# Delimiter must be its own line (optionally trailing whitespace / CR), not just the
# substring "---" anywhere in the file -- a plain content.split("---", 2) corrupts any
# field whose value contains "---" (e.g. `description: use --- as a separator`), silently
# dropping every field after it. \r is allowed so CRLF-checked-out files parse too.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)


def parse_frontmatter(content):
    if isinstance(content, str) and content.startswith("\ufeff"):
        content = content[1:]
    if not isinstance(content, str) or not content.startswith("---"):
        return None
    delims = list(_DELIM_RE.finditer(content))
    if len(delims) < 2:
        return None
    fm_text = content[delims[0].end():delims[1].start()]
    if HAS_YAML:
        try:
            return yaml.safe_load(fm_text)
        except yaml.YAMLError:
            return parse_yaml_minimal(fm_text)
    return parse_yaml_minimal(fm_text)


# Key names may contain hyphens/digits (e.g. `agent-type:`, `2026:`). The old
# ^[A-Za-z_]\w* pattern rejected those, which made _parse_block bail and return an
# EMPTY dict for the whole document rather than just skipping the odd line.
#
# The colon must be followed by whitespace or end-of-line to count as a mapping
# separator -- this is YAML's own disambiguation rule (a colon with no following space,
# e.g. a URL "http://x" or a ratio "3:1" inside a plain scalar, is NOT a key). Without
# this, a prose value containing a bare colon is misread as a nested "key: value",
# corrupting a plain list item into a bogus one-entry dict.
_KEY_RE = re.compile(r"^([^\s:#][^:]*?):(?:[ \t]+(.*)|)$")

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIMESTAMP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})$")


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


def _coerce_int(val):
    """PyYAML's YAML-1.1 integer resolver, or None if `val` is not an int.

    Ordered so the bare-leading-zero octal form is tested before the decimal
    form, matching PyYAML: "010" is 8, not 10.
    """
    for pattern, base in (
        (r"[-+]?0b[01_]+", 2),
        (r"[-+]?0x[0-9a-fA-F_]+", 16),
        (r"[-+]?0[0-7_]+", 8),
        (r"[-+]?(?:0|[1-9][\d_]*)", 10),
    ):
        if re.fullmatch(pattern, val):
            cleaned = val.replace("_", "")
            sign = -1 if cleaned.startswith("-") else 1
            cleaned = cleaned.lstrip("+-")
            if base != 10:
                cleaned = cleaned[2:] if base in (2, 16) else cleaned
            return sign * int(cleaned, base)
    return None


def _coerce_scalar(val):
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] == '"':
        # YAML double-quoted style escapes almost exactly like JSON (\n, \t, \", \\,
        # \uXXXX), so let json do it; fall back to a naive strip if it's not valid JSON.
        try:
            return json.loads(val)
        except ValueError:
            return val[1:-1]
    if len(val) >= 2 and val[0] == val[-1] and val[0] == "'":
        # YAML single-quoted style escapes only the quote itself, by doubling it.
        return val[1:-1].replace("''", "'")
    if val in ("null", "Null", "NULL", "~", ""):
        return None
    if val in ("true", "True", "TRUE"):
        return True
    if val in ("false", "False", "FALSE"):
        return False
    # Flow collections: [a, b] and {k: v}. Split on TOP-LEVEL commas only -- a naive
    # str.split(",") shreds nested collections like [{date: x, old: 3}] into fragments.
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_coerce_scalar(x) for x in _split_flow(inner)] if inner else []
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for part in _split_flow(inner):
            m = _KEY_RE.match(part)
            if m:
                out[m.group(1).strip()] = _coerce_scalar(m.group(2) or "")
        return out
    # Integers, following PyYAML's YAML-1.1 int resolver: binary, octal (a bare
    # leading zero!), decimal, and hex, each allowing '_' separators. Getting
    # octal wrong is the dangerous one -- "010" is 8, and reading it as 10
    # yields a plausible wrong number rather than a visible failure.
    coerced = _coerce_int(val)
    if coerced is not None:
        return coerced
    # Floats, following PyYAML's YAML-1.1 float resolver. Two rules matter and
    # both are easy to get backwards:
    #   * the mantissa must contain a literal '.'  -> "7E3" is the STRING "7E3"
    #   * the exponent's sign is MANDATORY         -> "7.0e3" is the STRING
    #     "7.0e3"; only "7.0e+3" resolves as a float.
    # Verified against real PyYAML in tests/test_yaml_fallback.py rather than
    # asserted here -- an earlier version of this comment claimed "7.0e3"
    # parsed as a float, which is exactly the kind of thing a differential
    # test catches and a confident comment does not.
    if re.fullmatch(r"[-+]?(\d[\d_]*\.[\d_]*|\.[\d_]+)([eE][-+]\d+)?", val):
        return float(val.replace("_", ""))
    if val in (".inf", ".Inf", ".INF", "+.inf"):
        return float("inf")
    if val in ("-.inf", "-.Inf", "-.INF"):
        return float("-inf")
    if val in (".nan", ".NaN", ".NAN"):
        return float("nan")
    m = _TIMESTAMP_RE.match(val)
    if m:
        try:
            return datetime.datetime(*(int(g) for g in m.groups()))
        except ValueError:
            return val
    m = _DATE_RE.match(val)
    if m:
        try:
            return datetime.date(*(int(g) for g in m.groups()))
        except ValueError:
            return val
    return val


def _split_flow(s):
    """Split a flow-collection body on top-level commas, respecting nesting and quotes."""
    parts, buf, depth = [], [], 0
    in_squote = in_dquote = False
    for ch in s:
        if ch == "'" and not in_dquote:
            in_squote = not in_squote
        elif ch == '"' and not in_squote:
            in_dquote = not in_dquote
        if not in_squote and not in_dquote:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
                continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


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


def _fold_continuations(lines, i, key_indent, first_part):
    """Fold PyYAML's wrapped-scalar continuation lines into one value.

    safe_dump wraps long scalars at width=80, emitting continuation lines indented
    deeper than their key:

        applies_when: some very long sentence that exceeds the default width and so
          continues on this line

    A non-empty value means YAML cannot have a nested block under that key, so any
    following deeper-indented line that isn't a list item must be a continuation.
    They fold with a single space (YAML plain-scalar folding). Without this, the value
    is truncated at the first line AND every later sibling key is silently dropped,
    because the parser stops at the first line whose indent doesn't match.
    """
    parts = [first_part]
    while i < len(lines) and lines[i][0] > key_indent and not lines[i][1].startswith("- "):
        parts.append(lines[i][1])
        i += 1
    return " ".join(p for p in parts if p), i


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
            # A quoted list item (this project never emits quoted mapping keys) can
            # legitimately contain ": " inside the quotes -- e.g. a value PyYAML had to
            # quote FOR containing ": " in the first place. _KEY_RE has no idea it's
            # inside quotes, so skip the key check entirely rather than misreading the
            # quoted colon as a mapping separator.
            is_quoted = len(rest) >= 1 and rest[0] in ("'", '"')
            m = None if is_quoted else _KEY_RE.match(rest)
            if not m:
                folded, i = _fold_continuations(lines, i + 1, indent0, rest)
                result.append(_coerce_scalar(folded))
                continue
            item = {}
            field_indent = indent0 + 2  # column where "key:" starts, right after "- "
            key0, val0 = m.group(1), (m.group(2) or "").strip()
            if val0 == "":
                sub_val, i = _parse_value(lines, i + 1, field_indent)
                item[key0] = sub_val
            else:
                folded, i = _fold_continuations(lines, i + 1, field_indent, val0)
                item[key0] = _coerce_scalar(folded)
            while i < len(lines) and lines[i][0] == field_indent:
                m2 = _KEY_RE.match(lines[i][1])
                if not m2:
                    break
                key, val = m2.group(1), (m2.group(2) or "").strip()
                if val == "":
                    sub_val, i = _parse_value(lines, i + 1, field_indent)
                    item[key] = sub_val
                else:
                    folded, i = _fold_continuations(lines, i + 1, field_indent, val)
                    item[key] = _coerce_scalar(folded)
            result.append(item)
        return result, i

    result = {}
    i = start
    while i < len(lines) and lines[i][0] == indent0:
        m = _KEY_RE.match(lines[i][1])
        if not m:
            break
        key, val = m.group(1), (m.group(2) or "").strip()
        if val == "":
            sub_val, i = _parse_value(lines, i + 1, indent0)
            result[key] = sub_val
        else:
            folded, i = _fold_continuations(lines, i + 1, indent0, val)
            result[key] = _coerce_scalar(folded)
    return result, i


def parse_yaml_minimal(text):
    """Fallback parser for our frontmatter format, used only when PyYAML isn't installed.

    WHY THIS EXISTS (do not delete as dead code). `pyproject.toml` declares
    PyYAML as a hard runtime dependency, so any *installed* commontrace takes
    the HAS_YAML branch and never reaches this code. It is here for the other
    way this file is used: executed directly, as a standalone script, in an
    environment that has not installed the package -- which is exactly how a
    benchmark gets run on a locked-down box or inside someone else's CI. The
    guarantee it buys is that `python measure_performance.py` never fails for
    want of a dependency.

    Its correctness is pinned by tests/test_yaml_fallback.py, which
    differential-tests it against real PyYAML; that is what makes the
    behavioural claims below verifiable rather than assertions.

    Handles flat 'key: value' pairs, one or more levels of nested mapping ('key:' followed
    by more-indented 'subkey: value' lines -- e.g. Trace.outcome), block lists of scalars
    or of dicts (e.g. tags, importance_history -- PyYAML's default block style, not inline
    '[a, b]'), inline '[a, b]'/'{k: v}' flow collections, quoted strings (single- and
    double-quoted escaping), booleans/null/dates/floats/scientific notation, PyYAML's
    line-wrapped-scalar continuation lines (width=80 default), and trailing '# comment'
    stripping. Integer and float resolution follows PyYAML's YAML-1.1 rules,
    including bare-leading-zero octal ('010' is 8), 0x/0b bases, '_' digit
    separators, and the mandatory exponent sign ('7.0e3' is a string, '7.0e+3'
    is a float).

    Known, deliberate gaps (not used by anything this project's own writer emits, so not
    worth the added complexity): a block list nested directly inside another block list
    ('- - item'); a plain scalar containing a literal embedded blank line, which YAML
    folds to a newline rather than a space (this parser always folds wrapped continuation
    lines to a single space); non-string mapping keys (an unquoted numeric key like
    `2026:` is read back as the string '2026', not the int 2026).
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
        with open(p, encoding="utf-8-sig") as fh:
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
        with open(p, encoding="utf-8-sig") as fh:
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
    # `.get`, not `[...]`: an episode file missing `name` is malformed, but a
    # malformed file must not crash `commontrace bench` for the whole store --
    # the benchmark exists to report on a corpus, including a messy one.
    episode_project = {ep["name"]: ep.get("project") for ep in episodes if ep.get("name")}

    def resolve_project(slug):
        if slug in episode_project:
            return episode_project[slug]
        path = os.path.join(BASE_DIR, "episodes", f"{slug}.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as fh:
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


def _safe_int(value, default=0):
    """Coerce a hand-authored frontmatter value to int, never raising.

    `.get("uses", 0)` returns the default only when the KEY is missing, so
    `uses: null` yields None and `uses: "1"` yields a str. Sorting a mix of
    those against ints raises TypeError, and one hand-edited lesson took the
    entire benchmark down with a traceback -- in a command whose whole job is
    to report on a store that may contain anything a human typed.
    bool is excluded deliberately: it is an int subclass, and `uses: true`
    silently counting as 1 use would be a wrong number rather than an error.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_extras(episodes, lessons):
    by_uses = sorted(lessons.items(), key=lambda kv: _safe_int(kv[1].get("uses")), reverse=True)
    top5 = [(n, _safe_int(lesson.get("uses"))) for n, lesson in by_uses[:5]
            if _safe_int(lesson.get("uses")) > 0]
    never_hit = sorted(n for n, lesson in lessons.items() if lesson.get("uses", 0) == 0)

    all_proposed = set()
    all_validated = set()
    for ep in episodes:
        all_proposed |= set(ep.get("lessons_proposed_by_omega") or [])
        all_validated |= set(get_validated(ep))
    proposed_not_validated = sorted(all_proposed - all_validated)

    # Distribution importance
    imp_lessons = {}
    for lesson in lessons.values():
        i = lesson.get("importance")
        imp_lessons[i] = imp_lessons.get(i, 0) + 1
    imp_episodes = {}
    for ep in episodes:
        i = ep.get("importance")
        imp_episodes[i] = imp_episodes.get(i, 0) + 1

    # Coverage by domain
    domain_coverage = {}
    for lesson in lessons.values():
        d = lesson.get("domain", "?")
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
    # Unimodal importance distribution: 95%+ (default) of lessons crammed into a single
    # importance level suggests a broken/degenerate rubric (everything drifts to one value).
    imp_lessons = extras.get("importance_lessons", {})
    total_imp = sum(v for v in imp_lessons.values())
    unimodal_threshold = thresholds.get("unimodal", DEFAULT_THRESHOLD_UNIMODAL)
    if total_imp > 0:
        max_level, max_count = max(imp_lessons.items(), key=lambda kv: kv[1])
        ratio = max_count / total_imp
        if ratio >= unimodal_threshold:
            alerts.append(
                f"Unimodal importance distribution: {max_count}/{total_imp} lessons "
                f"({ratio:.1%}) at importance {max_level!r} — threshold {unimodal_threshold:.0%} "
                f"— rubric may be miscalibrated"
            )
    return alerts


def _percentile(values, pct):
    """Linear-interpolation percentile (same convention as numpy.percentile default)."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(s[int(k)])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def compute_operational_cost(telemetry_path=None):
    """Read memory/alpha_telemetry.jsonl (one JSON object per Alpha retrieval invocation,
    written by memory/attention/query.py) and summarize latency/token cost.

    Returns a dict with `available: bool`. When unavailable, `message` explains why
    (file absent or empty/unusable) -- callers must render that message rather than
    crashing or silently omitting the section.
    """
    path = telemetry_path or _telemetry_path()
    if not os.path.exists(path):
        return {
            "available": False,
            "message": (
                f"No Alpha telemetry found at {path}. Run memory/attention/query.py "
                "at least once to generate retrieval telemetry."
            ),
        }
    latencies, tokens = [], []
    n_lines = 0
    n_malformed = 0
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            n_lines += 1
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                n_malformed += 1
                continue
            lat = rec.get("latency_ms")
            tok = rec.get("estimated_tokens")
            if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                latencies.append(float(lat))
            if isinstance(tok, (int, float)) and not isinstance(tok, bool):
                tokens.append(float(tok))
    if not latencies and not tokens:
        return {
            "available": False,
            "message": f"Telemetry file at {path} exists but has no usable records "
                       f"({n_lines} line(s) read, {n_malformed} malformed).",
        }
    return {
        "available": True,
        "n": n_lines,
        "n_malformed_skipped": n_malformed,
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "tokens_p50": _percentile(tokens, 50),
        "tokens_p95": _percentile(tokens, 95),
    }


def compute_semantic_duplicates(index_path=None, threshold=SEMANTIC_DUP_THRESHOLD):
    """Load memory/attention/index.npz and report lesson pairs with cosine similarity
    above `threshold` as merge candidates (recommendation only -- never merges/deletes).

    Guards: missing `numpy` (the `attention` extra isn't installed) or a missing/unreadable
    index.npz both degrade to `available: False` with an explanatory message, never a crash.
    """
    if not HAS_NUMPY:
        return {
            "available": False,
            "message": (
                "numpy is not installed -- install the 'attention' extra "
                "(`pip install -e '.[attention]'`) to enable semantic near-duplicate detection."
            ),
        }
    path = index_path or _attention_index_path()
    if not os.path.exists(path):
        return {
            "available": False,
            "message": (
                f"No attention index found at {path}. Run memory/attention/build_index.py "
                "first (requires the 'attention' extra)."
            ),
        }
    try:
        with np.load(path, allow_pickle=False) as data:
            slugs = [str(s) for s in data["slugs"]]
            embeddings = np.asarray(data["embeddings"], dtype=np.float64)
    except Exception as exc:  # noqa: BLE001 - any load failure degrades, never crashes
        return {"available": False, "message": f"Failed to load {path}: {exc}"}

    n = len(slugs)
    if n < 2 or embeddings.shape[0] != n:
        return {"available": True, "pairs": [], "n_lessons": n, "threshold": threshold}

    sim = embeddings @ embeddings.T
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            score = float(sim[i, j])
            if score > threshold:
                pairs.append((slugs[i], slugs[j], score))
    pairs.sort(key=lambda t: t[2], reverse=True)
    return {"available": True, "pairs": pairs, "n_lessons": n, "threshold": threshold}


# ---------------------------------------------------------------------------
# Run persistence + trend analysis (P3)
# ---------------------------------------------------------------------------

# The 3 main metrics tracked over time (cf. STATUS.md "Main Metrics (3 axes)"). Each entry
# is (display_name, path_into_the_stored_json_report). implicit_retrieval's permissive
# angle is carried alongside strict for context but is not itself one of the 3 axes.
_TREND_METRIC_PATHS = [
    ("lesson_quality", ("lesson_quality", "value")),
    ("implicit_retrieval_strict", ("implicit_retrieval", "strict")),
    ("implicit_retrieval_permissive", ("implicit_retrieval", "permissive")),
    ("transfer_gap", ("transfer_gap", "value")),
]


def _extract_metric(report, path):
    v = report
    for k in path:
        if not isinstance(v, dict):
            return None
        v = v.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def persist_report(clean_report, ts=None):
    """Write clean_report as JSON to memory/benchmark_reports/YYYY-MM-DD_HHMMSS.json.
    Returns the path written. `ts` (a datetime) lets callers reuse the same instant used
    to build the report's own `timestamp` field, so filenames and content agree.
    """
    ts = ts or datetime.datetime.now()
    out_dir = _reports_dir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{ts.strftime('%Y-%m-%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean_report, fh, indent=2, default=str)
    return path


def load_stored_reports(reports_dir=None):
    """Return [(path, report_dict), ...] for every *.json under benchmark_reports/,
    oldest first (filenames sort chronologically: YYYY-MM-DD_HHMMSS.json). Unreadable
    files (partial write, corrupted JSON) are skipped with a stderr warning rather than
    crashing the whole diff/history run.
    """
    d = reports_dir or _reports_dir()
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            with open(p, encoding="utf-8-sig") as fh:
                out.append((p, json.load(fh)))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[WARN] Skipping unreadable stored report {p}: {exc}", file=sys.stderr)
    return out


def compute_diff(older, newer, flag_delta=DIFF_FLAG_DELTA):
    """Compute deltas on the main metrics between two stored reports. A metric flags when
    it moved by more than `flag_delta` (default 5 percentage points, i.e. 0.05)."""
    rows = []
    for name, path in _TREND_METRIC_PATHS:
        old_v = _extract_metric(older, path)
        new_v = _extract_metric(newer, path)
        delta = None
        flagged = False
        if old_v is not None and new_v is not None:
            delta = new_v - old_v
            flagged = abs(delta) > flag_delta
        rows.append({"metric": name, "old": old_v, "new": new_v, "delta": delta, "flagged": flagged})
    return rows


def render_diff_report(rows, older_label, newer_label):
    out = [f"# Benchmark Diff: `{older_label}` -> `{newer_label}`", ""]
    out.append("| Metric | Old | New | Delta | Flag |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        old_s = fmt_pct(r["old"])
        new_s = fmt_pct(r["new"])
        delta_s = f"{r['delta'] * 100:+.1f}pp" if r["delta"] is not None else "N/A"
        flag_s = "**MOVED >5pp**" if r["flagged"] else ""
        out.append(f"| {r['metric']} | {old_s} | {new_s} | {delta_s} | {flag_s} |")
    flagged = [r for r in rows if r["flagged"]]
    out.append("")
    if flagged:
        out.append("**Flagged deltas (>5 percentage points):**")
        for r in flagged:
            out.append(f"- {r['metric']}: {fmt_pct(r['old'])} -> {fmt_pct(r['new'])} ({r['delta'] * 100:+.1f}pp)")
    else:
        out.append("*(no metric moved by more than 5 percentage points)*")
    return "\n".join(out)


def run_diff(reports_dir=None, strict=False, out=print):
    """Implements --diff. Returns a process exit code (0 unless --strict and something
    flagged). Handles 0 or 1 stored runs gracefully instead of crashing."""
    stored = load_stored_reports(reports_dir)
    if len(stored) < 2:
        out(
            f"Not enough stored benchmark runs to diff (found {len(stored)}, need >= 2). "
            f"Run measure_performance.py a few more times to build history under "
            f"{reports_dir or _reports_dir()}."
        )
        return 0
    (older_path, older), (newer_path, newer) = stored[-2], stored[-1]
    rows = compute_diff(older, newer)
    out(render_diff_report(rows, os.path.basename(older_path), os.path.basename(newer_path)))
    if strict and any(r["flagged"] for r in rows):
        return 2
    return 0


def render_history_report(stored):
    out = [f"# Benchmark History ({len(stored)} stored run(s))", ""]
    if len(stored) == 1:
        out.append("*(only 1 stored run -- not enough yet for a trend, showing it anyway)*")
        out.append("")
    out.append("| Timestamp | lesson_quality | retrieval strict | retrieval permissive | transfer_gap |")
    out.append("|---|---|---|---|---|")
    for path, r in stored:
        ts = r.get("timestamp") or os.path.basename(path)
        lq = _extract_metric(r, ("lesson_quality", "value"))
        irs = _extract_metric(r, ("implicit_retrieval", "strict"))
        irp = _extract_metric(r, ("implicit_retrieval", "permissive"))
        tg = _extract_metric(r, ("transfer_gap", "value"))
        out.append(f"| {ts} | {fmt_pct(lq)} | {fmt_pct(irs)} | {fmt_pct(irp)} | {fmt_pct(tg)} |")
    return "\n".join(out)


def run_history(reports_dir=None, out=print):
    """Implements --history. Returns a process exit code (always 0 -- history is purely
    informational). Handles 0 stored runs gracefully instead of crashing."""
    stored = load_stored_reports(reports_dir)
    if not stored:
        out(
            f"No stored benchmark runs found under {reports_dir or _reports_dir()}. "
            "Run measure_performance.py (without --diff/--history) to persist a run first."
        )
        return 0
    out(render_history_report(stored))
    return 0


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
        out.append("  (precision: proportion of Alpha selections that actually helped)")
        out.append(f"- **permissive** = mean(|hit| / |retrieved|) -> **{fmt_pct(ir['permissive'])}**")
        out.append("  (richness: can exceed 100% if Omega counts hits beyond Alpha's retrieved set)")
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
        ep_path = ep.get("_path", "")
        ep_name = ep.get("name") or ep.get("id") or (os.path.basename(ep_path) if ep_path else "unnamed")
        out.append(
            f"| `{ep_name}` | {ep.get('verdict', '?')} | "
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

    out.append("## Operational Cost (Alpha retrieval telemetry)")
    out.append("")
    oc = r.get("operational_cost") or {}
    if not oc.get("available"):
        out.append(f"*{oc.get('message', 'No telemetry data available.')}*")
    else:
        out.append(
            f"Based on {oc['n']} recorded Alpha retrieval invocation(s) in "
            "`memory/alpha_telemetry.jsonl`"
            + (f" ({oc['n_malformed_skipped']} malformed line(s) skipped)" if oc.get("n_malformed_skipped") else "")
            + "."
        )
        out.append("")
        out.append(f"- latency p50 : {oc['latency_p50_ms']:.1f} ms")
        out.append(f"- latency p95 : {oc['latency_p95_ms']:.1f} ms")
        out.append(f"- tokens p50 : {oc['tokens_p50']:.0f}")
        out.append(f"- tokens p95 : {oc['tokens_p95']:.0f}")
    out.append("")

    out.append("## Semantic near-duplicates (merge candidates)")
    out.append("")
    sd = r.get("semantic_duplicates") or {}
    if not sd.get("available"):
        out.append(f"*{sd.get('message', 'No semantic duplicate data available.')}*")
    else:
        pairs = sd.get("pairs") or []
        thr = sd.get("threshold", SEMANTIC_DUP_THRESHOLD)
        if not pairs:
            out.append(f"*(no lesson pairs above cosine {thr:.2f}, among {sd.get('n_lessons', 0)} indexed lessons)*")
        else:
            out.append(
                f"{len(pairs)} candidate pair(s) above cosine {thr:.2f} among "
                f"{sd.get('n_lessons', 0)} indexed lessons — recommendation only, "
                "no lesson is ever auto-merged or deleted:"
            )
            out.append("")
            for a, b, score in pairs:
                out.append(f"- `{a}` <-> `{b}` : cosine {score:.3f}")
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
        # Escape, as every other text path in this file does. Alert strings
        # interpolate frontmatter values (a lesson's `importance`, its slug),
        # which are attacker-controllable by whoever can write a lesson file --
        # so an unescaped banner is the one hole in the escaping this module
        # otherwise applies consistently.
        items = "\n".join(f"<li>{html.escape(str(a))}</li>" for a in alerts)
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
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 960px;
  margin: 2em auto; padding: 0 1.5em; line-height: 1.6; color: #2d2d2d;
}}
h1, h2, h3 {{ color: #1a1a1a; }}
h1 {{ border-bottom: 2px solid #444; padding-bottom: 0.3em; }}
h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 0.2em; margin-top: 2em; }}
h3 {{ margin-top: 1.5em; color: #444; }}
code {{
  background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.9em;
  font-family: "SF Mono", Menlo, Consolas, monospace;
}}
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
    parser.add_argument(
        "--save", action="store_true",
        help="Deprecated no-op: every run persists its JSON report by default now. "
             "Kept only so old invocations that pass --save don't break. Use --no-save to opt out.",
    )
    parser.add_argument(
        "--no-save", action="store_true",
        help="Do not persist the JSON report to memory/benchmark_reports/ (persisted by default)",
    )
    parser.add_argument(
        "--diff", action="store_true",
        help="Compare the 2 most recent stored runs under memory/benchmark_reports/ and flag deltas > 5pp",
    )
    parser.add_argument(
        "--history", action="store_true",
        help="Print a compact table of the 3 main metrics across all stored runs",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero (2) if any alert fires (or, with --diff, if any metric moved >5pp). "
             "Without this flag alerts/deltas are informational only (exit 0).",
    )
    parser.add_argument(
        "--threshold-quality", type=float, default=DEFAULT_THRESHOLD_QUALITY,
        metavar="FLOAT",
        help=f"lesson_quality alert threshold (default: {DEFAULT_THRESHOLD_QUALITY})",
    )
    parser.add_argument(
        "--threshold-retrieval", type=float, default=DEFAULT_THRESHOLD_RETRIEVAL,
        metavar="FLOAT",
        help=f"implicit_retrieval strict alert threshold (default: {DEFAULT_THRESHOLD_RETRIEVAL})",
    )
    parser.add_argument(
        "--threshold-never-hit", type=float, default=DEFAULT_THRESHOLD_NEVER_HIT,
        metavar="FLOAT",
        help=f"Never-hit lesson ratio alert threshold (default: {DEFAULT_THRESHOLD_NEVER_HIT})",
    )
    parser.add_argument(
        "--threshold-unimodal", type=float, default=DEFAULT_THRESHOLD_UNIMODAL,
        metavar="FLOAT",
        help=f"Unimodal importance-distribution alert threshold (default: {DEFAULT_THRESHOLD_UNIMODAL})",
    )
    parser.add_argument(
        "--threshold-semantic", type=float, default=None,
        metavar="FLOAT",
        help=f"Semantic similarity threshold (default: {SEMANTIC_DUP_THRESHOLD})",
    )
    parser.add_argument(
        "--threshold-lexical", type=float, default=None,
        metavar="FLOAT",
        help="Lexical similarity threshold",
    )
    parser.add_argument(
        "--threshold-freshness", type=float, default=None,
        metavar="FLOAT",
        help="Freshness threshold",
    )
    parser.add_argument(
        "--threshold-composite", type=float, default=None,
        metavar="FLOAT",
        help="Composite threshold",
    )
    args = parser.parse_args()
    if args.json and args.html:
        parser.error("--json and --html are mutually exclusive (choose one output format).")
    if args.diff and args.history:
        parser.error("--diff and --history are mutually exclusive.")
    if (args.diff or args.history) and (args.json or args.html):
        parser.error("--diff/--history report on stored history, not a fresh computation -- "
                      "cannot be combined with --json/--html.")

    # --diff / --history only read previously-persisted reports; they don't touch
    # episodes/lessons at all, and never persist a new report themselves.
    if args.diff:
        sys.exit(run_diff(strict=args.strict))
    if args.history:
        sys.exit(run_history())

    episodes = load_episodes(args.n if args.n > 0 else None)
    lessons = load_lessons()

    if not episodes:
        print("Not enough episodes to compute. Run /commontrace a few times first.")
        sys.exit(0)

    now = datetime.datetime.now()
    lq_value, lq_n = compute_lesson_quality(episodes)
    ir_strict, ir_permissive, ir_n = compute_implicit_retrieval(episodes)
    tg_value, tg_n, tg_untraceable = compute_transfer_gap(episodes, lessons)
    extras = compute_extras(episodes, lessons)
    operational_cost = compute_operational_cost()
    dup_threshold = (
        args.threshold_semantic
        if args.threshold_semantic is not None
        else SEMANTIC_DUP_THRESHOLD
    )
    semantic_duplicates = compute_semantic_duplicates(threshold=dup_threshold)

    report = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": now.isoformat(timespec="seconds"),
        "n_episodes": len(episodes),
        "n_lessons": len(lessons),
        "lesson_quality": {"value": lq_value, "n": lq_n},
        "implicit_retrieval": {"strict": ir_strict, "permissive": ir_permissive, "n": ir_n},
        "transfer_gap": {"value": tg_value, "n": tg_n, "untraceable": tg_untraceable},
        "episodes": episodes,
        "extras": extras,
        "operational_cost": operational_cost,
        "semantic_duplicates": semantic_duplicates,
    }

    thresholds = {
        "quality": args.threshold_quality,
        "retrieval": args.threshold_retrieval,
        "never_hit": args.threshold_never_hit,
        "unimodal": args.threshold_unimodal,
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
        out_dir = _reports_dir()
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{now.strftime('%Y-%m-%d_%H%M%S')}.html")
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

    # Every invocation persists its JSON report by default (P3) -- pass --no-save to opt out
    # (e.g. a throwaway/read-only invocation you don't want cluttering the history used by
    # --diff/--history). --save is kept as an accepted no-op for backward compatibility.
    if not args.no_save:
        json_path = persist_report(clean_report, ts=now)
        print(f"JSON report saved: {json_path}", file=sys.stderr)

    # Non-zero exit only when explicitly requested via --strict (after all output is flushed)
    if alerts and args.strict:
        sys.exit(2)


if __name__ == "__main__":
    main()
