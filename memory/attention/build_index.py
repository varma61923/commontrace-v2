#!/usr/bin/env python3
"""Build attention index for /commontrace memory lessons (v2.3).

Encode each ACTIVE lesson (description + domain + tags + applies_when +
do_not_apply_when + rule) using multi-qa-mpnet-base-dot-v1 (local execution
after first download — no runtime API calls, no telemetry).

Output: memory/attention/index.npz with fields:
    - slugs (np.ndarray[str])      : lesson identifiers, ordered
    - embeddings (np.ndarray[N,D]) : L2-normalized embeddings (cosine == dot)
    - model_name (str)             : "multi-qa-mpnet-base-dot-v1"
    - encoded_field (str)          : human-readable schema of what was encoded
    - timestamp (str)              : ISO-8601 build time
    - n_lessons (int)              : number of active lessons indexed

Usage:
    python build_index.py            # rebuild if outdated (or if missing)
    python build_index.py --force    # rebuild always

Trigger:
    - Auto: end of Phase 11 if Lambda created/updated/revised any lesson
    - Manual: --force after manual edits / archives / fusions

Strictly local: no API call at runtime, no telemetry, model cached under
~/.cache/huggingface/ (~420 MB) after first run.

Hooks Dreamer v2.4 (NOT implemented here, documented for future use):
    index.npz is intentionally exposed so a future Dreamer agent can detect
    lesson fusion candidates by computing pairwise cosine similarity on the
    `embeddings` matrix (sim > 0.85 = candidate fusion). The fields above are
    stable contract for that downstream use.
"""
import argparse
import datetime
import glob
import os
import re
import sys
import tempfile

import numpy as np
import yaml
from sentence_transformers import SentenceTransformer

# Delimiter must be its own line, not just the substring "---" anywhere in the file --
# a plain content.split("---", 2) corrupts any field whose value contains "---".
# \r is allowed so CRLF content parses too.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)

MODEL_NAME = "multi-qa-mpnet-base-dot-v1"

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
_AUTO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))  # memory/attention → memory → ROOT
_ROOT = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT") or _AUTO_ROOT
LESSONS_DIR = os.path.join(_ROOT, "memory", "lessons")
INDEX_PATH = os.path.join(_ROOT, "memory", "attention", "index.npz")
ENCODED_FIELD = "description+domain+tags+applies_when+do_not_apply_when+rule"


def extract_rule(body: str) -> str:
    """Extract the ## Rule section content from a lesson body (between ## Rule and next ##)."""
    if "## Rule" not in body:
        return ""
    after = body.split("## Rule", 1)[1]
    # Next "## " (section break) — split on newline-then-##
    next_section = after.split("\n##", 1)
    return next_section[0].strip()


def _load_frontmatter(fm_text: str):
    """Parse with commontrace's strict loader when it is importable.

    Plain yaml.safe_load applies YAML 1.1 rules, so `domain: NO` became
    False and `tags: [on, off]` became [True, False] -- the domain then
    dropped out of the embedded query text entirely and the tags embedded as
    booleans. commontrace/frontmatter.py already solved this; this script
    predates that and kept its own parse. Falls back to safe_load so the
    script still runs standalone from a checkout without the package
    installed, which is how it is documented to be usable.
    """
    try:
        from commontrace.frontmatter import _StrictBoolLoader
    except Exception:  # noqa: BLE001 - standalone use, any import problem
        return yaml.safe_load(fm_text)
    return yaml.load(fm_text, Loader=_StrictBoolLoader)


def build_query_text(frontmatter: dict, body: str) -> str:
    """Concatenate the 6 lesson fields with explicit labels and separators."""
    tags = frontmatter.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    parts = [
        f"Description: {frontmatter.get('description') or ''}",
        f"Domain: {frontmatter.get('domain') or ''}",
        f"Tags: {', '.join(str(t) for t in tags)}",
        f"Applies when: {frontmatter.get('applies_when') or ''}",
        f"Do not apply when: {frontmatter.get('do_not_apply_when') or ''}",
        f"Rule: {extract_rule(body)}",
    ]
    return " | ".join(parts)


def iter_active_lessons(lessons_dir: str):
    """Yield (slug, query_text) for each ACTIVE lesson (excludes template, README, archived)."""
    for path in sorted(glob.glob(os.path.join(lessons_dir, "lesson_*.md"))):
        fname = os.path.basename(path)
        if fname == "lesson_template.md":
            continue
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
        delims = list(_DELIM_RE.finditer(content))
        if len(delims) < 2:
            # Malformed: no closing frontmatter
            continue
        fm_text = content[delims[0].end():delims[1].start()]
        body = content[delims[1].end():]
        try:
            frontmatter = _load_frontmatter(fm_text) or {}
        except yaml.YAMLError as exc:
            print(f"[WARN] YAML parse failed for {fname}: {exc}", file=sys.stderr)
            continue
        # A frontmatter block that parses to a scalar (`---\njust text\n---`)
        # yields a str, and `.get()` on it raises AttributeError -- which the
        # except above does not catch, so one malformed lesson crashed the
        # whole indexer and took the semantic retrieval pipeline with it.
        if not isinstance(frontmatter, dict):
            print(f"[WARN] frontmatter in {fname} is {type(frontmatter).__name__}, "
                  "not a mapping -- skipping", file=sys.stderr)
            continue
        if frontmatter.get("status", "active") != "active":
            continue
        slug = frontmatter.get("name")
        if not slug:
            print(f"[WARN] No 'name' field in {fname}, skipping", file=sys.stderr)
            continue
        yield slug, build_query_text(frontmatter, body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even if index.npz already exists",
    )
    args = parser.parse_args()

    slugs: list[str] = []
    texts: list[str] = []
    for slug, query_text in iter_active_lessons(LESSONS_DIR):
        slugs.append(slug)
        texts.append(query_text)

    if not slugs:
        print(f"[ERR] No active lessons found under {LESSONS_DIR}", file=sys.stderr)
        return 1

    if os.path.exists(INDEX_PATH) and not args.force:
        # Staleness check has two parts: (a) mtime, which catches additions/edits, and
        # (b) the indexed slug set vs. the current one, which mtime alone can't catch --
        # deleting a lesson file doesn't advance any *remaining* file's mtime, so an
        # mtime-only check would report "up to date" while a stale slug lingers in
        # index.npz.
        index_mtime = os.path.getmtime(INDEX_PATH)
        newest_lesson = max(
            (os.path.getmtime(p) for p in glob.glob(os.path.join(LESSONS_DIR, "lesson_*.md"))),
            default=0.0,
        )
        try:
            with np.load(INDEX_PATH, allow_pickle=False) as data:
                indexed_slugs = {str(s) for s in data["slugs"]}
        except Exception:
            indexed_slugs = None
        same_slugs = indexed_slugs is not None and indexed_slugs == set(slugs)
        if newest_lesson <= index_mtime and same_slugs:
            print(f"Index up-to-date at {INDEX_PATH} (use --force to rebuild anyway)")
            return 0

    print(f"Loading model {MODEL_NAME} (cached under ~/.cache/huggingface/) ...")
    model = SentenceTransformer(MODEL_NAME)
    print(f"Encoding {len(texts)} lessons ...")
    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    # A unique per-process/per-call name, not a hardcoded ".tmp.npz". Two
    # processes rebuilding the index at once -- realistic if a rebuild is
    # ever triggered from a hook rather than run by hand -- both wrote to
    # the exact same path, so one process's np.savez could interleave with
    # or be clobbered by the other's before either reached os.replace.
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(INDEX_PATH), prefix=os.path.basename(INDEX_PATH) + ".", suffix=".tmp.npz"
    )
    os.close(tmp_fd)  # np.savez wants a path/fd it opens itself, not this one held open
    try:
        np.savez(
            tmp_path,
            slugs=np.array(slugs),
            embeddings=embeddings.astype(np.float32),
            model_name=np.array(MODEL_NAME),
            encoded_field=np.array(ENCODED_FIELD),
            timestamp=np.array(datetime.datetime.now().isoformat(timespec="seconds")),
            n_lessons=np.array(len(slugs)),
        )
        os.replace(tmp_path, INDEX_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    print(
        f"Index built: {len(slugs)} lessons, "
        f"model={MODEL_NAME}, dim={embeddings.shape[1]}, "
        f"path={INDEX_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
