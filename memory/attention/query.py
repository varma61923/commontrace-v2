#!/usr/bin/env python3
"""Query the /commontrace attention index for top-K relevant lessons (v2.3).

Pre-filter step used by Alpha (Phase 0) BEFORE qualitative judgement on
`applies_when` / `do_not_apply_when`. Output is parseable (one lesson per
line) and includes both the cosine score and the lesson importance, so
Alpha can keep using `score = importance × tag_match` as a qualitative
complement to cosine ranking — cosine is a COMPLEMENT, not a replacement.

Safety override (fixed by design decision v2.3):
    All ACTIVE lessons with importance >= floor (default 4) are always
    included in the returned set, even if absent from the top-K cosine
    ranking. Ensures a critical / showstopper lesson is never silently
    dropped because of an orthogonal query.

Usage:
    python query.py "my incoming task"
    python query.py "my incoming task" --top-k=10
    python query.py "my task" --top-k=10 --include-importance-floor=4
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys
import time
import zipfile

import numpy as np
import yaml
from sentence_transformers import SentenceTransformer

# The only model this project's build_index.py ever writes into index.npz. index.npz
# is a local build artifact, but it can arrive on a machine via a git clone/fork/sync
# rather than a local `build_index.py` run -- so its `model_name` field is not
# trustworthy input. Loading whatever string it contains via SentenceTransformer(...)
# would let a tampered index file point at an arbitrary Hugging Face Hub repo ID,
# which (per known transformers/sentence-transformers CVEs around
# trust_remote_code/torch.load) can execute attacker-supplied code on load. Only ever
# load this fixed, known-safe model name -- warn, don't trust, if the file disagrees.
_TRUSTED_MODEL_NAME = "multi-qa-mpnet-base-dot-v1"

# Delimiter must be its own line, not just the substring "---" anywhere in the file --
# a plain content.split("---", 2) corrupts any field whose value contains "---".
# \r is allowed so CRLF content parses too.
_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)

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
INDEX_PATH = os.path.join(_ROOT, "memory", "attention", "index.npz")
LESSONS_DIR = os.path.join(_ROOT, "memory", "lessons")
# Alpha operational-cost telemetry (Phase 3, P5): one JSON object appended per invocation.
# A module-level path (not a literal inlined at the call site) so tests can monkeypatch it
# to a tmp path, same pattern already used for INDEX_PATH/LESSONS_DIR above.
TELEMETRY_PATH = os.path.join(_ROOT, "memory", "alpha_telemetry.jsonl")


def load_importances() -> "tuple[dict[str, int], int]":
    """Return ({slug: importance} for every ACTIVE lesson (default 3 if missing),
    n_frontmatters_parsed) -- the second value counts every lesson_*.md (excluding the
    template) whose frontmatter was successfully parsed, active or not, for Alpha
    operational-cost telemetry (how many frontmatters retrieval had to read)."""
    out: dict[str, int] = {}
    n_parsed = 0
    for path in sorted(glob.glob(os.path.join(LESSONS_DIR, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
        delims = list(_DELIM_RE.finditer(content))
        if len(delims) < 2:
            continue
        try:
            frontmatter = yaml.safe_load(content[delims[0].end():delims[1].start()]) or {}
        except yaml.YAMLError:
            continue
        n_parsed += 1
        if frontmatter.get("status", "active") != "active":
            continue
        slug = frontmatter.get("name")
        if not slug:
            continue
        try:
            out[str(slug)] = int(frontmatter.get("importance", 3))
        except (TypeError, ValueError):
            out[str(slug)] = 3
    return out, n_parsed


def _append_telemetry(record, path=None):
    """Append one JSON line to memory/alpha_telemetry.jsonl -- create the file if absent,
    always append, never truncate existing history. A telemetry write failure (e.g.
    read-only filesystem) must never break the actual retrieval it's instrumenting, so
    failures are reported to stderr and swallowed rather than raised.
    """
    path = path or TELEMETRY_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"[WARN] Failed to write Alpha telemetry to {path}: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("query", help="Incoming task / query string (verbatim)")
    parser.add_argument("--top-k", type=int, default=10, help="Top-K cosine hits (default 10)")
    parser.add_argument(
        "--include-importance-floor",
        type=int,
        default=4,
        help="Always include lessons with importance >= this (safety override, default 4)",
    )
    args = parser.parse_args()

    # Latency covers the whole retrieval stage (index load through brief assembly below),
    # not just the cosine matmul -- that's what actually costs an Alpha invocation wall-clock
    # time and is what STATUS.md P5 asks to measure.
    _t0 = time.monotonic()

    if not os.path.exists(INDEX_PATH):
        print(
            f"[ERR] No index found at {INDEX_PATH}. Run build_index.py first.",
            file=sys.stderr,
        )
        return 1

    try:
        data = np.load(INDEX_PATH, allow_pickle=False)
        model_name = str(data["model_name"])
        embeddings = data["embeddings"]  # already L2-normalized
        slugs = data["slugs"]
        n_lessons = int(data["n_lessons"])
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, KeyError) as exc:
        print(
            f"[ERR] Index file at {INDEX_PATH} is corrupted ({exc}). "
            "Please rebuild the index: python memory/attention/build_index.py --force",
            file=sys.stderr,
        )
        return 1

    if model_name != _TRUSTED_MODEL_NAME:
        print(
            f"[WARN] {INDEX_PATH} declares model_name={model_name!r}, which does not "
            f"match the expected {_TRUSTED_MODEL_NAME!r}. Refusing to load an "
            "untrusted model name from an index file -- run build_index.py --force "
            "to regenerate a trustworthy index.",
            file=sys.stderr,
        )
        return 1
    model = SentenceTransformer(_TRUSTED_MODEL_NAME)
    q_emb = model.encode(args.query, normalize_embeddings=True, convert_to_numpy=True)
    # cosine == dot when both are unit-norm
    scores = embeddings @ q_emb

    # Top-K by cosine (descending)
    order = np.argsort(scores)[::-1]
    top_k_idx = list(order[: args.top_k])

    # Safety override: include all active lessons with importance >= floor. This must
    # check every lesson currently on disk (`importances`, from load_importances()), not
    # just slugs already present in `slugs` (the index) -- a lesson added/edited since the
    # last `build_index.py` run exists on disk but not in the index, so iterating only the
    # index's own slugs silently breaks this script's own documented safety guarantee for
    # exactly the lessons most likely to need it (freshly-authored critical rules).
    importances, n_frontmatters_parsed = load_importances()
    floor = args.include_importance_floor
    missing_from_index = []
    if floor is not None:
        existing = set(top_k_idx)
        indexed_slugs = {str(s) for s in slugs}
        for i, slug in enumerate(slugs):
            if i in existing:
                continue
            if importances.get(str(slug), 0) >= floor:
                top_k_idx.append(i)
                existing.add(i)
        for slug, imp in importances.items():
            if imp >= floor and slug not in indexed_slugs:
                missing_from_index.append((slug, imp))

    brief_lines = [
        f"# Top-{args.top_k} retrieval (+ importance>={floor} override)",
        f"# Index: {n_lessons} lessons, model={model_name}",
        f"# Query: {args.query!r}",
    ]
    if missing_from_index:
        print(
            f"# WARNING: {len(missing_from_index)} importance>={floor} lesson(s) not yet in "
            "the index (run build_index.py) -- included below with cosine=N/A",
            file=sys.stderr,
        )
    for idx in top_k_idx:
        slug = str(slugs[idx])
        score = float(scores[idx])
        imp = importances.get(slug, 0)
        brief_lines.append(f"{slug} | cosine={score:.3f} | importance={imp}")
    for slug, imp in missing_from_index:
        brief_lines.append(f"{slug} | cosine=N/A | importance={imp}")

    for line in brief_lines:
        print(line)

    # Alpha operational-cost telemetry (Phase 3, P5): latency, frontmatters parsed, number
    # of candidates the attention layer itself surfaced (top_k_idx -- entries with an actual
    # embedding/cosine score; missing_from_index entries are a disk fallback, not something
    # the attention layer surfaced), and a cheap word-count*1.3 estimate of the resulting
    # brief's token cost (no tokenizer dependency added just for an estimate).
    elapsed_ms = (time.monotonic() - _t0) * 1000.0
    brief_text = "\n".join(brief_lines)
    estimated_tokens = len(brief_text.split()) * 1.3
    _append_telemetry(
        {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "latency_ms": elapsed_ms,
            "n_frontmatters_parsed": n_frontmatters_parsed,
            "n_candidates_surfaced": len(top_k_idx),
            "n_missing_from_index": len(missing_from_index),
            "estimated_tokens": estimated_tokens,
            "top_k": args.top_k,
            "query_chars": len(args.query),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
