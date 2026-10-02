#!/usr/bin/env python3
"""Build attention index for /commontrace memory lessons (v2.3).

Encode each ACTIVE lesson (description + domain + tags + applies_when +
do_not_apply_when + rule) with a trusted sentence-embedding model (local
execution after first download — no runtime API calls, no telemetry): the
model the existing index was built with, else DEFAULT_MODEL_NAME, unless
--model names another.

Output: memory/attention/index.npz with fields:
    - slugs (np.ndarray[str])      : lesson identifiers, ordered
    - embeddings (np.ndarray[N,D]) : L2-normalized embeddings (cosine == dot)
    - model_name (str)             : one of TRUSTED_MODELS
    - encoded_field (str)          : human-readable schema of what was encoded
    - timestamp (str)              : ISO-8601 build time
    - n_lessons (int)              : number of active lessons indexed

Usage:
    python build_index.py            # rebuild if outdated (or if missing)
    python build_index.py --force    # rebuild always
    python build_index.py --force --model multi-qa-mpnet-base-dot-v1
                                     # rebuild with another trusted model

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
from __future__ import annotations

import argparse
import contextlib
import datetime
import glob
import hashlib
import os
import re
import sys
import tempfile
from typing import Any

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

_DELIM_RE = re.compile(r"^---[ \t]*\r?$", re.MULTILINE)

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

TRUSTED_MODELS = ("multi-qa-mpnet-base-dot-v1", "Snowflake/snowflake-arctic-embed-m-v1.5")
DEFAULT_MODEL_NAME = "Snowflake/snowflake-arctic-embed-m-v1.5"
_TRUSTED_MODEL_NAME = DEFAULT_MODEL_NAME
MODEL_NAME = DEFAULT_MODEL_NAME
EMBEDDING_DIM = 768

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_AUTO_ROOT = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
_ROOT = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT") or _AUTO_ROOT
LESSONS_DIR = os.path.join(_ROOT, "memory", "lessons")
INDEX_PATH = os.path.join(_ROOT, "memory", "attention", "index.npz")
ENCODED_FIELD = "description+domain+tags+applies_when+do_not_apply_when+rule|v2"


_RULE_RE = re.compile(r"^##[ \t]*Rule[ \t]*\r?\n(.*?)(?=\n##[ \t]|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def extract_rule(body: str) -> str:
    """Extract the ## Rule section content from a lesson body (between ## Rule and next ##)."""
    m = _RULE_RE.search(body)
    return m.group(1).strip() if m else ""


def _load_frontmatter(fm_text: str):
    try:
        from commontrace.frontmatter import load_text
    except Exception:  # noqa: BLE001 - standalone use, any import problem
        return yaml.safe_load(fm_text)
    return load_text(fm_text)


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


class ActiveLesson(tuple):
    """3-tuple (slug, query_text, agent_type) with metadata attributes."""

    def __new__(cls, slug: str, query_text: str, agent_type: str, importance: int = 3, status: str = "active"):
        return super().__new__(cls, (slug, query_text, agent_type))

    def __init__(self, slug: str, query_text: str, agent_type: str, importance: int = 3, status: str = "active"):
        self.slug = slug
        self.query_text = query_text
        self.agent_type = agent_type
        self.importance = importance
        self.status = status


def iter_active_lessons(lessons_dir: str):
    """Yield ActiveLesson(slug, query_text, agent_type, importance, status) for each ACTIVE lesson."""
    for path in sorted(glob.glob(os.path.join(lessons_dir, "lesson_*.md"))):
        fname = os.path.basename(path)
        if fname == "lesson_template.md":
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                content = fh.read()
        except OSError as exc:
            print(f"[WARN] skipping unreadable lesson {fname}: {exc}", file=sys.stderr)
            continue
        delims = list(_DELIM_RE.finditer(content))
        if len(delims) < 2:
            continue
        fm_text = content[delims[0].end():delims[1].start()]
        body = content[delims[1].end():]
        try:
            frontmatter = _load_frontmatter(fm_text) or {}
        except yaml.YAMLError as exc:
            print(f"[WARN] YAML parse failed for {fname}: {exc}", file=sys.stderr)
            continue
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

        try:
            importance_val = int(frontmatter.get("importance", 3))
        except (TypeError, ValueError):
            importance_val = 3
        status_val = str(frontmatter.get("status", "active") or "active")

        yield ActiveLesson(
            str(slug),
            build_query_text(frontmatter, body),
            str(frontmatter.get("agent_type") or ""),
            importance=importance_val,
            status=status_val,
        )


def _write_index(
    index_path: str,
    slugs: "list[str]",
    embeddings: "np.ndarray",
    agent_types: "list[str]" = None,
    hashes: "list[str]" = None,
    importances: "list[int]" = None,
    statuses: "list[str]" = None,
    model_name: str = MODEL_NAME,
) -> None:
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(index_path), prefix=os.path.basename(index_path) + ".", suffix=".tmp.npz"
    )
    os.close(tmp_fd)
    try:
        np.savez(
            tmp_path,
            slugs=np.array(slugs),
            agent_types=np.array(agent_types if agent_types is not None else [""] * len(slugs)),
            embeddings=embeddings.astype(np.float32),
            hashes=np.array(hashes if hashes is not None else [""] * len(slugs)),
            importances=np.array(importances if importances is not None else [3] * len(slugs), dtype=np.int16),
            statuses=np.array(statuses if statuses is not None else ["active"] * len(slugs)),
            model_name=np.array(model_name),
            encoded_field=np.array(ENCODED_FIELD),
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


def index_model(index_path: str, fallback: "str | None" = None) -> str:
    try:
        with np.load(index_path, allow_pickle=False) as data:
            name = str(data["model_name"])
            if name in TRUSTED_MODELS and int(data["embeddings"].shape[0]) > 0:
                return name
    except Exception:
        pass
    return fallback if fallback in TRUSTED_MODELS else DEFAULT_MODEL_NAME


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


def build_or_update_index(
    lessons_dir: str,
    output_path: str,
    model_name: "str | None" = None,
    force_rebuild: bool = False,
    model: Any = None,
    log: Any = print,
    items: Any = None,
) -> dict[str, Any]:
    if np is None:
        raise ImportError("numpy is required to build or update the attention index.")
    if model_name is None:
        model_name = index_model(output_path)
    if model_name not in TRUSTED_MODELS:
        raise ValueError(
            f"{model_name!r} is not a trusted embedding model; expected one of {list(TRUSTED_MODELS)}")

    active_items = list(items) if items is not None else list(iter_active_lessons(lessons_dir))
    slugs = [item[0] for item in active_items]
    texts = [item[1] for item in active_items]
    agent_types = [item[2] for item in active_items]
    importances = [getattr(item, "importance", 3) for item in active_items]
    statuses = [getattr(item, "status", "active") for item in active_items]
    hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]

    if not slugs:
        embeddings = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        _write_index(
            output_path,
            slugs,
            embeddings,
            agent_types,
            hashes=hashes,
            importances=importances,
            statuses=statuses,
            model_name=model_name,
        )
        return {
            "output_path": output_path,
            "n_lessons": 0,
            "reused_count": 0,
            "encoded_count": 0,
            "model_name": model_name,
        }

    cached_vectors: dict[tuple[str, str], np.ndarray] = {}
    if os.path.exists(output_path) and not force_rebuild:
        try:
            with np.load(output_path, allow_pickle=False) as data:
                if (
                    str(data.get("model_name", "")) == model_name
                    and str(data.get("encoded_field", "")) == ENCODED_FIELD
                    and data["embeddings"].ndim == 2
                    and data["embeddings"].shape[1] == EMBEDDING_DIM
                ):
                    c_slugs = [str(s) for s in data["slugs"]]
                    c_embs = np.asarray(data["embeddings"], dtype=np.float32)
                    if "hashes" in data.files:
                        c_hashes = [str(h) for h in data["hashes"]]
                        for s, h, emb in zip(c_slugs, c_hashes, c_embs):
                            cached_vectors[(s, h)] = emb
        except Exception:
            cached_vectors = {}

    texts_to_encode: list[str] = []
    indices_to_encode: list[int] = []
    reused_embeddings: dict[int, np.ndarray] = {}

    for idx, (slug, text, chash) in enumerate(zip(slugs, texts, hashes)):
        if not force_rebuild and (slug, chash) in cached_vectors:
            reused_embeddings[idx] = cached_vectors[(slug, chash)]
        else:
            indices_to_encode.append(idx)
            texts_to_encode.append(text)

    if texts_to_encode:
        if model is None:
            if SentenceTransformer is None:
                raise ImportError(
                    "sentence_transformers is required to encode new or modified lessons.")
            log(f"Loading model {model_name} (cached under ~/.cache/huggingface/) ...")
            try:
                with _no_progress_bars():
                    model = SentenceTransformer(model_name, local_files_only=True)
            except Exception:  # noqa: BLE001 - not cached, or an older library: fetch it
                model = SentenceTransformer(model_name)
        log(f"Encoding {len(texts_to_encode)} lessons (reusing {len(reused_embeddings)} cached) ...")
        new_embs = model.encode(
            texts_to_encode,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        embeddings = np.zeros((len(slugs), EMBEDDING_DIM), dtype=np.float32)
        for idx, emb in reused_embeddings.items():
            embeddings[idx] = emb
        for idx, emb in zip(indices_to_encode, new_embs):
            embeddings[idx] = emb
    else:
        embeddings = np.zeros((len(slugs), EMBEDDING_DIM), dtype=np.float32)
        for idx, emb in reused_embeddings.items():
            embeddings[idx] = emb

    _write_index(
        output_path,
        slugs,
        embeddings,
        agent_types,
        hashes=hashes,
        importances=importances,
        statuses=statuses,
        model_name=model_name,
    )
    return {
        "output_path": output_path,
        "n_lessons": len(slugs),
        "reused_count": len(reused_embeddings),
        "encoded_count": len(indices_to_encode),
        "model_name": model_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--force",
        "--rebuild",
        action="store_true",
        dest="force",
        help="Rebuild even if index.npz already exists, bypassing vector cache",
    )
    parser.add_argument(
        "--model",
        choices=TRUSTED_MODELS,
        default=None,
        help="Embedding model to build with (default: the one the existing index "
             f"was built with, else {DEFAULT_MODEL_NAME}). A different model is a "
             "different semantic ranking: a store mid-experiment records it as a new "
             "treatment.",
    )
    parser.add_argument(
        "--fallback-model",
        choices=TRUSTED_MODELS,
        default=None,
        help="Model to build with when the existing index pins none (it is missing "
             "or empty) and --model is not given: the one a store's experiment "
             "ranked with (`commontrace index` passes it).",
    )
    args = parser.parse_args()
    model_name = args.model or index_model(INDEX_PATH, args.fallback_model)

    items = None
    if os.path.exists(INDEX_PATH) and not args.force:
        index_mtime = _safe_mtime(INDEX_PATH)
        newest_lesson = max(
            (_safe_mtime(p) for p in glob.glob(os.path.join(LESSONS_DIR, "lesson_*.md"))),
            default=0.0,
        )
        try:
            with np.load(INDEX_PATH, allow_pickle=False) as data:
                indexed_slugs = {str(s) for s in data["slugs"]}
                model_matches = (
                    str(data["model_name"]) == model_name
                    and str(data["encoded_field"]) == ENCODED_FIELD
                    and data["embeddings"].ndim == 2
                    and data["embeddings"].shape[1] == EMBEDDING_DIM
                )
        except Exception:
            indexed_slugs = None
            model_matches = False

        items = list(iter_active_lessons(LESSONS_DIR))
        active_slugs = {item[0] for item in items}
        same_slugs = indexed_slugs is not None and indexed_slugs == active_slugs
        if newest_lesson <= index_mtime and same_slugs and model_matches:
            print(f"Index up-to-date at {INDEX_PATH} (use --force or --rebuild to rebuild anyway)")
            return 0

    res = build_or_update_index(
        LESSONS_DIR, INDEX_PATH, model_name=model_name, force_rebuild=args.force, items=items)
    print(
        f"Index built: {res['n_lessons']} lessons ({res['encoded_count']} encoded, "
        f"{res['reused_count']} reused from cache), "
        f"model={res['model_name']}, dim={EMBEDDING_DIM}, path={INDEX_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
