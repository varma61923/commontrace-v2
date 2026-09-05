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

# Same charset commontrace/commands/lesson_cmd.py's own _SLUG_RE enforces
# when a lesson is created or looked up through the CLI. A lesson file is
# meant to be hand-editable, though (commontrace/frontmatter.py), so a
# `name` that never went through the CLI at all reaches this read path
# unvalidated -- and query.py's retrieval brief is `|`-delimited
# (`f"{slug} | cosine=... | importance=..."`), so a `name` containing a
# pipe corrupts every line built from it, not just its own.
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

MODEL_NAME = "multi-qa-mpnet-base-dot-v1"
# multi-qa-mpnet-base-dot-v1's fixed sentence-embedding output width. Needed
# to write a correctly-shaped 0-row embeddings array when there are no
# active lessons to encode (see main()'s `not slugs` branch below), without
# having to load the model just to ask it -- the whole point of that branch
# is to skip the (slow) model load entirely when there is nothing to encode.
EMBEDDING_DIM = 768

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


_RULE_RE = re.compile(r"^##[ \t]*Rule[ \t]*\r?\n(.*?)(?=\n##[ \t]|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0



def extract_rule(body: str) -> str:
    """Extract the ## Rule section content from a lesson body (between ## Rule and next ##).

    IGNORECASE, and tolerant of trailing whitespace after "Rule": lesson
    bodies are explicitly meant to be hand-edited (commontrace/frontmatter.py),
    and a literal `"## Rule" not in body` / `body.split("## Rule", 1)` pair
    silently extracted nothing for a hand-written `## rule` or `## Rule ` --
    the exact class of case-sensitivity bug trace_io.py's own `_SECTION_RE`
    already fixed for `## Context`/`## Solution` (see its docstring).
    """
    m = _RULE_RE.search(body)
    return m.group(1).strip() if m else ""


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
    # bandit flags any yaml.load() call regardless of Loader, but
    # _StrictBoolLoader IS a yaml.SafeLoader subclass (see its docstring in
    # commontrace/frontmatter.py) that only narrows two implicit-conversion
    # rules -- it accepts no more of the YAML spec than SafeLoader does, so
    # this carries none of the arbitrary-object-instantiation risk B506
    # exists to catch.
    return yaml.load(fm_text, Loader=_StrictBoolLoader)  # nosec B506


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
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                content = fh.read()
        except OSError as exc:
            # A file glob matched but became unreadable by the time we get
            # here (permissions, deleted between glob() and open() by a
            # concurrent capture/lesson command, a broken symlink) -- one
            # such lesson must not abort the whole index rebuild. Same
            # guard memory/attention/query.py's load_importances() already
            # has for the identical failure mode; this script predates it
            # and had fallen out of sync.
            print(f"[WARN] skipping unreadable lesson {fname}: {exc}", file=sys.stderr)
            continue
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
        if not _SLUG_RE.match(str(slug)):
            print(
                f"[WARN] 'name' in {fname} ({slug!r}) is not a plain slug "
                f"({_SLUG_RE.pattern}), skipping", file=sys.stderr
            )
            continue
        yield slug, build_query_text(frontmatter, body)


def _write_index(index_path: str, slugs: "list[str]", embeddings: np.ndarray) -> None:
    """Atomically write index.npz: build to a unique per-process tmp file
    under the same directory, then os.replace() over the final path.

    A hardcoded ".tmp.npz" name would let two processes rebuilding the index
    at once (realistic if a rebuild is ever triggered from a hook rather
    than run by hand) interleave or clobber each other's np.savez before
    either reached os.replace; a unique name per call avoids that.
    """
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(index_path), prefix=os.path.basename(index_path) + ".", suffix=".tmp.npz"
    )
    os.close(tmp_fd)  # np.savez wants a path/fd it opens itself, not this one held open
    try:
        np.savez(
            tmp_path,
            slugs=np.array(slugs),
            embeddings=embeddings.astype(np.float32),
            model_name=np.array(MODEL_NAME),
            encoded_field=np.array(ENCODED_FIELD),
            # UTC, not a naive local timestamp: PROTOCOL.md specifies
            # ISO-8601 UTC everywhere, and a naive local time cannot be
            # sorted or compared across multi-agent runners in different
            # timezones -- see the identical fix in query.py's telemetry.
            timestamp=np.array(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")),
            n_lessons=np.array(len(slugs)),
        )
        os.replace(tmp_path, index_path)
    except BaseException:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


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
        # A freshly initialized repository has 0 active lessons -- that is
        # not an error, it is the starting state every repo passes through.
        # Refusing to write index.npz here used to trap query.py in an
        # unrecoverable loop: query.py requires index.npz to exist, its own
        # error message says "run build_index.py first", and build_index.py
        # exited 1 without writing one. Writing an empty (0-row) index lets
        # query.py load it and correctly report "no lessons match" instead.
        print(f"[INFO] No active lessons found under {LESSONS_DIR}. Writing an empty index.")
        embeddings = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        _write_index(INDEX_PATH, slugs, embeddings)
        return 0

    if os.path.exists(INDEX_PATH) and not args.force:
        # Staleness check has three parts: (a) mtime, which catches additions/edits,
        # (b) the indexed slug set vs. the current one, which mtime alone can't catch --
        # deleting a lesson file doesn't advance any *remaining* file's mtime, so an
        # mtime-only check would report "up to date" while a stale slug lingers in
        # index.npz -- and (c) the model/field/dimension the index was built with.
        # Without (c), an index.npz built with a different embedding model (or copied
        # in from another environment/checkout) still passes (a) and (b) and gets
        # reported "up-to-date", so query.py's own model_name/dimension guards reject
        # it on the very next run with "Model mismatch ... rebuild the index" -- a
        # loop this same staleness check should have caught instead of deferring to
        # query.py's stricter, later checks.
        index_mtime = _safe_mtime(INDEX_PATH)
        newest_lesson = max(
            (_safe_mtime(p) for p in glob.glob(os.path.join(LESSONS_DIR, "lesson_*.md"))),
            default=0.0,
        )
        try:
            with np.load(INDEX_PATH, allow_pickle=False) as data:
                indexed_slugs = {str(s) for s in data["slugs"]}
                model_matches = (
                    str(data["model_name"]) == MODEL_NAME
                    and str(data["encoded_field"]) == ENCODED_FIELD
                    and data["embeddings"].ndim == 2
                    and data["embeddings"].shape[1] == EMBEDDING_DIM
                )
        except Exception:
            indexed_slugs = None
            model_matches = False
        same_slugs = indexed_slugs is not None and indexed_slugs == set(slugs)
        if newest_lesson <= index_mtime and same_slugs and model_matches:
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

    _write_index(INDEX_PATH, slugs, embeddings)
    print(
        f"Index built: {len(slugs)} lessons, "
        f"model={MODEL_NAME}, dim={embeddings.shape[1]}, "
        f"path={INDEX_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
