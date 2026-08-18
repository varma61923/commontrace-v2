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
import glob
import os
import re
import sys

import numpy as np
import yaml
from sentence_transformers import SentenceTransformer

# Delimiter must be its own line, not just the substring "---" anywhere in the file --
# a plain content.split("---", 2) corrupts any field whose value contains "---".
_DELIM_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)

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


def load_importances() -> dict[str, int]:
    """Return {slug: importance} for every ACTIVE lesson (default 3 if missing)."""
    out: dict[str, int] = {}
    for path in sorted(glob.glob(os.path.join(LESSONS_DIR, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        delims = list(_DELIM_RE.finditer(content))
        if len(delims) < 2:
            continue
        try:
            frontmatter = yaml.safe_load(content[delims[0].end():delims[1].start()]) or {}
        except yaml.YAMLError:
            continue
        if frontmatter.get("status", "active") != "active":
            continue
        slug = frontmatter.get("name")
        if not slug:
            continue
        try:
            out[str(slug)] = int(frontmatter.get("importance", 3))
        except (TypeError, ValueError):
            out[str(slug)] = 3
    return out


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

    if not os.path.exists(INDEX_PATH):
        print(
            f"[ERR] No index found at {INDEX_PATH}. Run build_index.py first.",
            file=sys.stderr,
        )
        return 1

    data = np.load(INDEX_PATH, allow_pickle=True)
    model_name = str(data["model_name"])
    embeddings = data["embeddings"]  # already L2-normalized
    slugs = data["slugs"]
    n_lessons = int(data["n_lessons"])

    model = SentenceTransformer(model_name)
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
    importances = load_importances()
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

    print(f"# Top-{args.top_k} retrieval (+ importance>={floor} override)")
    print(f"# Index: {n_lessons} lessons, model={model_name}")
    print(f"# Query: {args.query!r}")
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
        print(f"{slug} | cosine={score:.3f} | importance={imp}")
    for slug, imp in missing_from_index:
        print(f"{slug} | cosine=N/A | importance={imp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
