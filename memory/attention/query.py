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


def _load_frontmatter(fm_text: str):
    """Parse with commontrace's strict loader when it is importable.

    Identical to build_index.py's helper of the same name, and deliberately
    kept in sync with it rather than shared via import: plain
    yaml.safe_load applies YAML 1.1 rules, so a lesson `name: on` parses
    here as the boolean True while build_index.py's index (built with
    _StrictBoolLoader) keys the same lesson under the string "on" -- an
    importance>=floor safety-override lookup in this module for that lesson
    then misses under `importances[slug]` even though the lesson genuinely
    has a high importance, because the two loaders disagree on what `slug`
    even is. Falls back to safe_load so this script still runs standalone
    from a checkout without the package installed, same as build_index.py.
    """
    try:
        from commontrace.frontmatter import _StrictBoolLoader
    except Exception:  # noqa: BLE001 - standalone use, any import problem
        return yaml.safe_load(fm_text)
    # See build_index.py's identical comment: _StrictBoolLoader IS a
    # yaml.SafeLoader subclass that only narrows two implicit-conversion
    # rules, so this carries none of the arbitrary-object-instantiation
    # risk bandit's B506 exists to catch.
    return yaml.load(fm_text, Loader=_StrictBoolLoader)  # nosec B506


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
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                content = fh.read()
        except OSError as exc:
            # A file glob matched but the file itself is unreadable by the
            # time we get to it (permissions, deleted between glob() and
            # open() by a concurrent capture/lesson command, a broken
            # symlink) -- one such lesson must not abort retrieval for
            # every other lesson in the store.
            print(f"[WARN] skipping unreadable lesson {path}: {exc}", file=sys.stderr)
            continue
        delims = list(_DELIM_RE.finditer(content))
        if len(delims) < 2:
            continue
        try:
            frontmatter = _load_frontmatter(content[delims[0].end():delims[1].start()]) or {}
        except yaml.YAMLError:
            continue
        # Same guard as build_index.py: a scalar frontmatter block parses to
        # a str, and .get() on it raises AttributeError past the yaml-only
        # except above.
        if not isinstance(frontmatter, dict):
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


# Each record here is small, fixed-shape operational-cost metadata (see the
# call site: latency, counts, a token-count estimate, query LENGTH -- never
# the query text itself), but one gets appended per invocation with no
# retention limit, so a long-lived store's telemetry file grows without
# bound. Rotated once it crosses this size rather than left to grow
# forever.
_TELEMETRY_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB


def _append_telemetry(record, path=None):
    """Append one JSON line to memory/alpha_telemetry.jsonl -- create the file if absent,
    always append, never truncate existing history. A telemetry write failure (e.g.
    read-only filesystem) must never break the actual retrieval it's instrumenting, so
    failures are reported to stderr and swallowed rather than raised.

    Rotation (getsize -> os.replace) and the append that follows are guarded by
    commontrace.frontmatter.locked(): without it, two concurrent query.py invocations
    (a multi-agent fleet, or several parallel Alpha calls) can race the check-then-act
    rotation -- one process's os.replace() can swap the file out from under another
    that already decided not to rotate, so that process's append lands in the freshly
    rotated `.1` file instead of a fresh `path`, or raises FileNotFoundError against an
    inode that no longer exists at that name. The lock is degrade-only (see
    frontmatter.locked's own docstring): on a platform with neither fcntl nor msvcrt it
    is a no-op, same as everywhere else this module is used, rather than a reason a
    telemetry write -- or the retrieval it's instrumenting -- ever fails outright.
    """
    path = path or TELEMETRY_PATH
    try:
        from commontrace.frontmatter import locked
    except Exception:  # noqa: BLE001 - standalone use, any import problem
        import contextlib
        locked = lambda _p: contextlib.nullcontext()  # noqa: E731
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with locked(path):
            if os.path.exists(path) and os.path.getsize(path) >= _TELEMETRY_MAX_BYTES:
                # Keep exactly one prior generation, the simplest form of
                # logrotate's own default behavior -- overwrites any previous
                # .1 rather than accumulating .1, .2, .3, ... forever, which
                # would just move the unbounded-growth problem sideways.
                try:
                    os.replace(path, path + ".1")
                except OSError:
                    pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"[WARN] Failed to write Alpha telemetry to {path}: {exc}", file=sys.stderr)


def _positive_int(raw: str) -> int:
    """argparse type= for --top-k. `order[:top_k]` below is a Python slice,
    not a bounds check: `order[:-1]` means "all but the last", not
    "nothing", so a negative --top-k silently returned nearly the entire
    index instead of failing -- the opposite of "a small number of
    results". Rejected at parse time rather than clamped silently, since a
    negative top-k is a caller bug worth surfacing."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"--top-k must be >= 1, got {value}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("query", help="Incoming task / query string (verbatim)")
    parser.add_argument("--top-k", type=_positive_int, default=10, help="Top-K cosine hits (default 10)")
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
        # `with`, not a bare np.load(): NpzFile keeps the underlying zip
        # file open until closed, and array access below (data[...])
        # decompresses each array into its own independent ndarray in
        # memory -- so extracting them here and closing on exit from the
        # `with` loses nothing, while a bare np.load() leaked the file
        # handle for the rest of the process's lifetime, which on Windows
        # blocks a subsequent `build_index.py --force` from replacing this
        # same file (already fixed the same way in build_index.py's own
        # np.load call; this brings query.py in line with it).
        with np.load(INDEX_PATH, allow_pickle=False) as data:
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
    try:
        # SentenceTransformer downloads the model from Hugging Face Hub on
        # first use if it isn't already in the local cache
        # (~/.cache/huggingface/), which needs internet access this
        # process may not have -- an air-gapped deployment, or a
        # cache the operator didn't realize was never populated. Left
        # uncaught this raised a raw OSError/traceback from deep inside
        # huggingface_hub instead of the clean, actionable error every
        # other failure path in this function already gives.
        model = SentenceTransformer(_TRUSTED_MODEL_NAME)
    except OSError as exc:
        print(
            f"[ERR] Could not load model {_TRUSTED_MODEL_NAME!r}: {exc}\n"
            "If this host has no internet access, pre-download the model on a "
            "connected machine and copy ~/.cache/huggingface/ over, or set "
            "HF_HUB_OFFLINE=1 once it's cached locally.",
            file=sys.stderr,
        )
        return 1
    q_emb = model.encode(args.query, normalize_embeddings=True, convert_to_numpy=True)

    # An index built by a different (or later, wider) embedding model has a
    # different column count, and `embeddings @ q_emb` raises numpy's own
    # ValueError -- "matmul: Input operand 1 has a mismatch in its core
    # dimension" -- with no mention of build_index.py, from deep inside a
    # matrix multiply rather than from a guard that names the fix. The
    # model_name check above catches a MISLABELED index; this catches a
    # correctly-labeled one that is simply the wrong shape, which the
    # model_name string alone cannot detect. Row count is checked too:
    # `slugs[idx]` below indexes unconditionally, so an index truncated by a
    # previous crash mid-write would raise IndexError past the same point.
    if embeddings.ndim != 2 or embeddings.shape[1] != q_emb.shape[0]:
        print(
            f"[ERR] {INDEX_PATH} has embedding dimension {embeddings.shape}, which "
            f"does not match this model's {q_emb.shape[0]}. Rebuild the index: "
            "python memory/attention/build_index.py --force",
            file=sys.stderr,
        )
        return 1
    if embeddings.shape[0] != len(slugs):
        print(
            f"[ERR] {INDEX_PATH} has {embeddings.shape[0]} embedding row(s) but "
            f"{len(slugs)} slug(s) -- the index is truncated or corrupted. Rebuild it: "
            "python memory/attention/build_index.py --force",
            file=sys.stderr,
        )
        return 1

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
