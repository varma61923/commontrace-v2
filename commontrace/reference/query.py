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
import contextlib
import datetime
import glob
import json
import os
import re
import sys
import time
import zipfile

try:
    import numpy as np
except ImportError:
    np = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

TRUSTED_MODELS = {
    "multi-qa-mpnet-base-dot-v1": "",
    "Snowflake/snowflake-arctic-embed-m-v1.5":
        "Represent this sentence for searching relevant passages: ",
}
DEFAULT_MODEL_NAME = "Snowflake/snowflake-arctic-embed-m-v1.5"
_TRUSTED_MODEL_NAME = DEFAULT_MODEL_NAME

_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_ROOT = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT") or _AUTO_ROOT
INDEX_PATH = os.path.join(_ROOT, "memory", "attention", "index.npz")
LESSONS_DIR = os.path.join(_ROOT, "memory", "lessons")
TELEMETRY_PATH = os.path.join(_ROOT, "memory", "alpha_telemetry.jsonl")


def _load_frontmatter(fm_text: str):
    try:
        from commontrace.frontmatter import load_text
    except Exception:  # noqa: BLE001 - standalone use, any import problem
        return yaml.safe_load(fm_text)
    return load_text(fm_text)


class ImportancesResult(tuple):
    newest_active_mtime: float

    def __new__(cls, importances: dict[str, int], n_parsed: int, newest_active_mtime: float = 0.0):
        obj = super().__new__(cls, (importances, n_parsed))
        obj.newest_active_mtime = newest_active_mtime
        return obj


def load_importances(lessons_dir: str | None = None) -> "ImportancesResult":
    out: dict[str, int] = {}
    n_parsed = 0
    newest_active_mtime = 0.0
    lessons_dir = LESSONS_DIR if lessons_dir is None else lessons_dir
    for path in sorted(glob.glob(os.path.join(lessons_dir, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                content = fh.read()
        except OSError as exc:
            print(f"[WARN] skipping unreadable lesson {path}: {exc}", file=sys.stderr)
            continue
        delims = list(_DELIM_RE.finditer(content))
        if len(delims) < 2:
            continue
        try:
            frontmatter = _load_frontmatter(content[delims[0].end():delims[1].start()]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(frontmatter, dict):
            continue
        n_parsed += 1
        if frontmatter.get("status", "active") != "active":
            continue
        newest_active_mtime = max(newest_active_mtime, mtime)
        slug = frontmatter.get("name")
        if not slug or not _SLUG_RE.match(str(slug)):
            continue
        try:
            out[str(slug)] = int(frontmatter.get("importance", 3))
        except (TypeError, ValueError):
            out[str(slug)] = 3
    return ImportancesResult(out, n_parsed, newest_active_mtime)


def load_importances_from_index(data) -> "ImportancesResult | None":
    if isinstance(data, (str, os.PathLike)):
        try:
            with np.load(data, allow_pickle=False) as npz:
                return load_importances_from_index(npz)
        except Exception:
            return None
    files = data.files if hasattr(data, "files") else data
    if "importances" not in files or "statuses" not in files:
        return None
    try:
        raw_importances = data["importances"]
        raw_statuses = data["statuses"]
        slugs = data["slugs"]
        out: dict[str, int] = {}
        for i, s in enumerate(slugs):
            if str(raw_statuses[i]) == "active":
                try:
                    out[str(s)] = int(raw_importances[i])
                except (TypeError, ValueError):
                    out[str(s)] = 3
        return ImportancesResult(out, 0, 0.0)
    except Exception:
        return None


_TELEMETRY_MAX_BYTES = 10 * 1024 * 1024


def _append_telemetry(record, path=None):
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
                try:
                    os.replace(path, path + ".1")
                except OSError:
                    pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"[WARN] Failed to write Alpha telemetry to {path}: {exc}", file=sys.stderr)


def check_staleness(
    index_path: str,
    lessons_dir: str,
    indexed_slugs: "set[str]",
    active_slugs: "set[str]",
    newest_active_mtime: "float | None" = None,
    lesson_mtimes=None,
):
    reasons: list[str] = []

    added = active_slugs - indexed_slugs
    removed = indexed_slugs - active_slugs
    if added:
        reasons.append(
            f"{len(added)} active lesson(s) not embedded in the index: {', '.join(sorted(added))}"
        )
    if removed:
        reasons.append(
            f"{len(removed)} embedded slug(s) no longer active on disk: {', '.join(sorted(removed))}"
        )

    try:
        index_mtime = os.path.getmtime(index_path)
    except OSError:
        index_mtime = None
    if index_mtime is not None:
        if newest_active_mtime is not None:
            if newest_active_mtime > index_mtime:
                reasons.append("an active lesson file was modified after the index was last built")
        else:
            newest_active = 0.0
            if lesson_mtimes is None:
                lesson_mtimes = []
                for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
                    try:
                        lesson_mtimes.append((path, os.path.getmtime(path)))
                    except OSError:
                        continue
            for path, mtime in lesson_mtimes:
                if mtime <= index_mtime:
                    continue
                if os.path.basename(path) == "lesson_template.md":
                    continue
                try:
                    with open(path, "r", encoding="utf-8-sig") as fh:
                        content = fh.read()
                except OSError:
                    continue
                delims = list(_DELIM_RE.finditer(content))
                if len(delims) < 2:
                    continue
                try:
                    frontmatter = _load_frontmatter(content[delims[0].end():delims[1].start()]) or {}
                except yaml.YAMLError:
                    continue
                if not isinstance(frontmatter, dict):
                    continue
                if frontmatter.get("status", "active") != "active":
                    continue
                newest_active = max(newest_active, mtime)
            if newest_active > index_mtime:
                reasons.append("an active lesson file was modified after the index was last built")

    return reasons


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"--top-k must be >= 1, got {value}")
    return value


@contextlib.contextmanager
def _no_progress_bars():
    restore = []
    try:
        from huggingface_hub import utils as hub_utils

        if not hub_utils.are_progress_bars_disabled():
            hub_utils.disable_progress_bars()
            restore.append(hub_utils.enable_progress_bars)
    except Exception:  # noqa: BLE001 - an older library: leave its bars alone
        pass
    try:
        from transformers.utils import logging as transformers_logging

        if transformers_logging.is_progress_bar_enabled():
            transformers_logging.disable_progress_bar()
            restore.append(transformers_logging.enable_progress_bar)
    except Exception:  # noqa: BLE001 - an older library: leave its bars alone
        pass
    try:
        yield
    finally:
        for enable in restore:
            enable()


def _load_cached_first(model_name):
    try:
        with _no_progress_bars():
            return SentenceTransformer(model_name, local_files_only=True)
    except Exception:  # noqa: BLE001 - not cached, or a library without the flag
        return SentenceTransformer(model_name)


class Ranked:
    """What one semantic retrieval produced, before anything is printed."""

    def __init__(self, rc, lines=(), stderr=(), stats=None):
        self.rc = rc
        self.lines = list(lines)
        self.stderr = list(stderr)
        self.stats = stats or {}


_MODELS: dict = {}


def load_index(index_path):
    if not os.path.exists(index_path):
        return Ranked(1, stderr=[
            f"[ERR] No index found at {index_path}. Run build_index.py first.",
        ])
    try:
        with np.load(index_path, allow_pickle=False) as data:
            model_name = str(data["model_name"])
            embeddings = data["embeddings"]
            slugs = data["slugs"]
            agent_types = data["agent_types"] if "agent_types" in data.files else None
            n_lessons = int(data["n_lessons"])
            fast_importances = load_importances_from_index(data)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, KeyError) as exc:
        return Ranked(1, stderr=[
            f"[ERR] Index file at {index_path} is corrupted ({exc}). "
            "Please rebuild the index: python memory/attention/build_index.py --force",
        ])
    return (model_name, embeddings, slugs, agent_types, n_lessons, fast_importances)


def load_model(model_name=DEFAULT_MODEL_NAME):
    if model_name not in TRUSTED_MODELS:
        return Ranked(1, stderr=[
            f"[ERR] {model_name!r} is not a trusted embedding model; expected one of "
            f"{sorted(TRUSTED_MODELS)}.",
        ])
    try:
        return _load_cached_first(model_name)
    except OSError as exc:
        return Ranked(1, stderr=[
            f"[ERR] Could not load model {model_name!r}: {exc}\n"
            "If this host has no internet access, pre-download the model on a "
            "connected machine and copy ~/.cache/huggingface/ over, or set "
            "HF_HUB_OFFLINE=1 once it's cached locally.",
        ])


def rank(query, top_k=10, include_importance_floor=4, agent_type=None, *,
         index_path=None, lessons_dir=None, index=None, model=None,
         lesson_mtimes=None) -> Ranked:
    index_path = INDEX_PATH if index_path is None else index_path
    lessons_dir = LESSONS_DIR if lessons_dir is None else lessons_dir
    stderr: list[str] = []

    if index is None:
        index = load_index(index_path)
    if isinstance(index, Ranked):
        return index
    model_name, embeddings, slugs, agent_types, n_lessons, fast_importances = index

    if model_name not in TRUSTED_MODELS:
        return Ranked(1, stderr=[
            f"[WARN] {index_path} declares model_name={model_name!r}, which is not "
            f"one of the trusted {sorted(TRUSTED_MODELS)}. Refusing to load an "
            "untrusted model name from an index file -- run build_index.py --force "
            "to regenerate a trustworthy index.",
        ])
    if model is None:
        model = load_model(model_name)
    if isinstance(model, Ranked):
        return model
    q_emb = model.encode(
        TRUSTED_MODELS[model_name] + query, normalize_embeddings=True, convert_to_numpy=True,
        show_progress_bar=False)

    if embeddings.ndim != 2 or embeddings.shape[1] != q_emb.shape[0]:
        return Ranked(1, stderr=[
            f"[ERR] {index_path} has embedding dimension {embeddings.shape}, which "
            f"does not match this model's {q_emb.shape[0]}. Rebuild the index: "
            "python memory/attention/build_index.py --force",
        ])
    if embeddings.shape[0] != len(slugs):
        return Ranked(1, stderr=[
            f"[ERR] {index_path} has {embeddings.shape[0]} embedding row(s) but "
            f"{len(slugs)} slug(s) -- the index is truncated or corrupted. Rebuild it: "
            "python memory/attention/build_index.py --force",
        ])

    scores = embeddings @ q_emb

    if fast_importances is not None:
        importances_res = fast_importances
        importances, n_frontmatters_parsed = dict(importances_res[0]), importances_res[1]
        try:
            idx_mtime = os.path.getmtime(index_path)
        except OSError:
            idx_mtime = 0.0
        if lesson_mtimes is None:
            lesson_mtimes = []
            for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
                try:
                    lesson_mtimes.append((path, os.path.getmtime(path)))
                except OSError:
                    pass
        for path, mtime in lesson_mtimes:
            if path.endswith("lesson_template.md") and os.path.basename(path) == "lesson_template.md":
                continue
            try:
                if mtime > idx_mtime:
                    with open(path, "r", encoding="utf-8-sig") as fh:
                        content = fh.read()
                    delims = list(_DELIM_RE.finditer(content))
                    if len(delims) >= 2:
                        fm = _load_frontmatter(content[delims[0].end():delims[1].start()]) or {}
                        if isinstance(fm, dict) and fm.get("status", "active") == "active":
                            s = fm.get("name")
                            if s and _SLUG_RE.match(str(s)):
                                try:
                                    importances[str(s)] = int(fm.get("importance", 3))
                                except (TypeError, ValueError):
                                    importances[str(s)] = 3
                                n_frontmatters_parsed += 1
            except OSError:
                pass
    else:
        importances_res = load_importances(lessons_dir)
        importances, n_frontmatters_parsed = importances_res

    order = np.argsort(scores)[::-1]
    active_order = [idx for idx in order if str(slugs[idx]) in importances]

    if agent_type:
        if agent_types is None:
            stderr.append(
                f"[WARN] --agent-type {agent_type!r} was NOT applied: this index "
                "predates the agent_types column. Rebuild with `commontrace index "
                "--force` to filter by fleet."
            )
        else:
            active_order = [
                idx for idx in active_order
                if str(agent_types[idx]) == agent_type
            ]
    top_k_idx = list(active_order[: top_k])

    indexed_slugs = {str(s) for s in slugs}
    floor = include_importance_floor
    missing_from_index = []
    if floor is not None and floor > 0:
        existing = set(top_k_idx)
        for i, slug in enumerate(slugs):
            if i in existing:
                continue
            if importances.get(str(slug), 0) >= floor:
                top_k_idx.append(i)
                existing.add(i)
        for slug, imp in importances.items():
            if imp >= floor and slug not in indexed_slugs:
                missing_from_index.append((slug, imp))

    override_desc = f"+ importance>={floor} override" if floor is not None and floor > 0 else "override disabled"

    stale_reasons = check_staleness(
        index_path,
        lessons_dir,
        indexed_slugs,
        set(importances.keys()),
        newest_active_mtime=(
            getattr(importances_res, "newest_active_mtime", None) if n_frontmatters_parsed > 0 else None
        ),
        lesson_mtimes=lesson_mtimes,
    )

    brief_lines = [
        f"# Top-{top_k} retrieval ({override_desc})",
        f"# Index: {n_lessons} lessons, model={model_name}",
        f"# Query: {query!r}",
    ]
    if stale_reasons:
        brief_lines.append(f"# WARNING: index may be stale -- {'; '.join(stale_reasons)}")
        stderr.append(
            f"[WARN] {index_path} may be stale -- {'; '.join(stale_reasons)}. "
            "Cosine scores below may not reflect current lesson content. Rebuild: "
            "python memory/attention/build_index.py --force"
        )
    if missing_from_index:
        stderr.append(
            f"# WARNING: {len(missing_from_index)} importance>={floor} lesson(s) not yet in "
            "the index (run build_index.py) -- included below with cosine=N/A"
        )
    for idx in top_k_idx:
        slug = str(slugs[idx])
        score = float(scores[idx])
        imp = importances.get(slug, 0)
        brief_lines.append(f"{slug} | cosine={score:.3f} | importance={imp}")
    for slug, imp in missing_from_index:
        brief_lines.append(f"{slug} | cosine=N/A | importance={imp}")

    return Ranked(0, brief_lines, stderr, {
        "n_frontmatters_parsed": n_frontmatters_parsed,
        "n_candidates_surfaced": len(top_k_idx),
        "n_missing_from_index": len(missing_from_index),
        "index_stale": bool(stale_reasons),
    })


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
    parser.add_argument(
        "--agent-type", default=None,
        help="Restrict results to one fleet. Requires an index built with the "
             "agent_types column; an older index has no way to tell fleets apart "
             "and the filter is reported as not applied rather than silently ignored.",
    )
    args = parser.parse_args()

    _t0 = time.monotonic()
    result = rank(args.query, args.top_k, args.include_importance_floor, args.agent_type)
    for message in result.stderr:
        print(message, file=sys.stderr)
    if result.rc != 0:
        return result.rc
    for line in result.lines:
        print(line)

    elapsed_ms = (time.monotonic() - _t0) * 1000.0
    brief_text = "\n".join(result.lines)
    estimated_tokens = len(brief_text.split()) * 1.3
    _append_telemetry(
        {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "latency_ms": elapsed_ms,
            "n_frontmatters_parsed": result.stats["n_frontmatters_parsed"],
            "n_candidates_surfaced": result.stats["n_candidates_surfaced"],
            "n_missing_from_index": result.stats["n_missing_from_index"],
            "index_stale": result.stats["index_stale"],
            "estimated_tokens": estimated_tokens,
            "top_k": args.top_k,
            "query_chars": len(args.query),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
