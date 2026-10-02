#!/usr/bin/env python3
"""Incremental semantic index for memory lessons (perf-scale path).

Unlike a full rebuild (re-read every lesson, re-embed every text, rewrite the
whole ``index.npz``), :func:`build_incremental` reuses cached vectors keyed by
**file stamp** ``(mtime_ns, size)``:

- Unchanged files are never re-read and never re-embedded; their rows are
  copied straight from the existing ``index.npz``.
- Only new/changed files are read and embedded (one batch, one call).
- The ``.npz`` is written exactly once per build with an atomic
  tmp-file + ``os.replace``. When nothing changed, nothing is written at all
  (``wrote_index`` is ``False``) so a one-file edit never pays the full-corpus
  rewrite cost.

First build (no usable cache) is a plain full build: every row is embedded and
the output is byte-for-byte equivalent to :func:`build_full` with the same
``embed_fn``/``dim``.

Only ``numpy`` is required (stdlib otherwise). Embedding is injected via
``embed_fn(texts, dim) -> np.ndarray`` so tests and operators can supply a
lightweight deterministic embedder; the default (:func:`default_embed`) is a
hash-based L2-normalized embedder with no heavy dependencies. Any ``embed_fn``
must be deterministic per text for incremental vectors to match full-rebuild
vectors exactly.

Slug derivation: file stem (``lesson_foo.md`` -> ``lesson_foo``). Embedded
text: raw file content. This keeps the module dependency-free and suitable
for synthetic scale corpora; the production field-concatenated builder lives
in ``commontrace/reference/build_index.py``.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import sys
import tempfile

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

DEFAULT_DIM = 64
INDEX_VERSION = 1

LESSON_PREFIX = "lesson_"
LESSON_SUFFIX = ".md"
TEMPLATE_NAME = "lesson_template.md"


def _require_numpy():
    if np is None:
        raise ImportError("numpy is required to build the incremental attention index.")


def file_stamp(path: str) -> tuple[int, int]:
    """Return ``(mtime_ns, size)`` for *path*; ``(0, 0)`` when missing."""
    try:
        st = os.stat(path)
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (0, 0)


def _iter_lesson_files(lessons_dir: str) -> list[str]:
    try:
        names = os.listdir(lessons_dir)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.startswith(LESSON_PREFIX) or not name.endswith(LESSON_SUFFIX):
            continue
        if name == TEMPLATE_NAME:
            continue
        full = os.path.join(lessons_dir, name)
        if os.path.isfile(full):
            out.append(full)
    out.sort()
    return out


def _slug_for_file(path: str) -> str:
    base = os.path.basename(path)
    if base.endswith(LESSON_SUFFIX):
        base = base[: -len(LESSON_SUFFIX)]
    return base


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_embed(texts: list[str], dim: int = DEFAULT_DIM) -> "np.ndarray":
    """Deterministic hash-based embedder (L2-normalized, float32).

    Each text maps to a pseudo-random unit vector derived from its SHA-256
    digest, so identical texts always yield identical vectors regardless of
    batch composition or order.
    """
    _require_numpy()
    dim = int(dim)
    embs = np.zeros((len(texts), dim), dtype=np.float32)
    for i, text in enumerate(texts):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        row = np.zeros(dim, dtype=np.float32)
        for j in range(dim):
            row[j] = (digest[j % len(digest)] / 255.0) * 2.0 - 1.0 + (j // len(digest)) * 0.01
        norm = float(np.linalg.norm(row))
        if norm == 0.0:
            row[0] = 1.0
            norm = 1.0
        embs[i] = row / norm
    return embs


def _run_embed(embed_fn, texts: list[str], dim: int) -> "np.ndarray":
    _require_numpy()
    if embed_fn is None:
        return default_embed(texts, dim)
    try:
        result = embed_fn(texts, dim)
    except TypeError:
        result = embed_fn(texts)
    arr = np.asarray(result, dtype=np.float32)
    if arr.shape != (len(texts), int(dim)):
        raise ValueError(
            f"embed_fn returned shape {arr.shape}, expected ({len(texts)}, {int(dim)})"
        )
    return arr


def _load_cache(output_path: str, dim: int):
    """Load a usable cache or return ``None`` (missing/corrupt/dim mismatch)."""
    _require_numpy()
    try:
        with np.load(output_path, allow_pickle=False) as data:
            files = set(data.files)
            if not {"slugs", "embeddings", "mtimes_ns", "sizes", "content_hashes"} <= files:
                return None
            slugs = [str(s) for s in data["slugs"]]
            embs = np.asarray(data["embeddings"], dtype=np.float32)
            mtimes = [int(v) for v in data["mtimes_ns"]]
            sizes = [int(v) for v in data["sizes"]]
            hashes = [str(v) for v in data["content_hashes"]]
            if embs.ndim != 2 or embs.shape[1] != int(dim):
                return None
            if not (len(slugs) == embs.shape[0] == len(mtimes) == len(sizes) == len(hashes)):
                return None
            by_slug = {}
            for idx, slug in enumerate(slugs):
                by_slug[slug] = (idx, mtimes[idx], sizes[idx], hashes[idx])
            return {"slugs": slugs, "embeddings": embs, "by_slug": by_slug}
    except Exception:
        return None


def _write_index_atomic(
    output_path: str,
    slugs: list[str],
    embeddings: "np.ndarray",
    mtimes: list[int],
    sizes: list[int],
    hashes: list[str],
    dim: int,
) -> None:
    _require_numpy()
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=parent, prefix=os.path.basename(output_path) + ".", suffix=".tmp.npz"
    )
    os.close(fd)
    try:
        np.savez(
            tmp_path,
            slugs=np.array(slugs),
            embeddings=np.asarray(embeddings, dtype=np.float32),
            mtimes_ns=np.array(mtimes, dtype=np.int64),
            sizes=np.array(sizes, dtype=np.int64),
            content_hashes=np.array(hashes),
            dim=np.array(int(dim)),
            index_version=np.array(INDEX_VERSION),
            timestamp=np.array(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")),
            n_lessons=np.array(len(slugs)),
        )
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def build_full(
    lessons_dir: str,
    output_path: str,
    embed_fn=None,
    dim: int = DEFAULT_DIM,
) -> dict:
    """Embed every lesson file and write the index once."""
    _require_numpy()
    dim = int(dim)
    files = _iter_lesson_files(lessons_dir)
    slugs = [_slug_for_file(p) for p in files]
    texts = [_read_text(p) for p in files]
    hashes = [_content_hash(t) for t in texts]
    stamps = [file_stamp(p) for p in files]
    if texts:
        embeddings = _run_embed(embed_fn, texts, dim)
    else:
        embeddings = np.zeros((0, dim), dtype=np.float32)
    _write_index_atomic(
        output_path,
        slugs,
        embeddings,
        [s[0] for s in stamps],
        [s[1] for s in stamps],
        hashes,
        dim,
    )
    return {
        "output_path": output_path,
        "n_lessons": len(slugs),
        "encoded_count": len(slugs),
        "reused_count": 0,
        "dim": dim,
        "wrote_index": True,
        "full_rebuild": True,
    }


def build_incremental(
    lessons_dir: str,
    output_path: str,
    embed_fn=None,
    dim: int = DEFAULT_DIM,
) -> dict:
    """Rebuild the index, re-embedding only new/changed files.

    Rows whose ``(mtime_ns, size)`` stamp matches the cached row are reused
    without reading the file. Rows whose stamp changed are re-read; if the
    content hash is unchanged (mtime-only bump, e.g. ``touch``) the cached
    vector is still reused. The ``.npz`` is written exactly once, and not at
    all when every stamp hits.
    """
    _require_numpy()
    dim = int(dim)
    files = _iter_lesson_files(lessons_dir)
    slugs = [_slug_for_file(p) for p in files]
    stamps = {slug: file_stamp(p) for slug, p in zip(slugs, files)}
    by_path = dict(zip(slugs, files))

    cache = _load_cache(output_path, dim) if os.path.exists(output_path) else None
    if cache is None:
        return build_full(lessons_dir, output_path, embed_fn=embed_fn, dim=dim)

    cached = cache["by_slug"]
    cached_embs = cache["embeddings"]

    reused: dict[str, "np.ndarray"] = {}
    to_embed_slugs: list[str] = []
    to_embed_texts: list[str] = []
    to_embed_hashes: list[str] = []

    for slug in slugs:
        mtime, size = stamps[slug]
        hit = cached.get(slug)
        if hit is not None and hit[1] == mtime and hit[2] == size:
            reused[slug] = cached_embs[hit[0]]
            continue
        # Stamp changed (or new slug): read once, compare content hash.
        text = _read_text(by_path[slug])
        chash = _content_hash(text)
        if hit is not None and hit[3] == chash:
            reused[slug] = cached_embs[hit[0]]
            continue
        to_embed_slugs.append(slug)
        to_embed_texts.append(text)
        to_embed_hashes.append(chash)

    removed = [s for s in cached if s not in stamps]
    if not to_embed_slugs and not removed and set(cache["slugs"]) == set(slugs):
        # Same slug set, every stamp hit: nothing to do, skip the rewrite.
        return {
            "output_path": output_path,
            "n_lessons": len(slugs),
            "encoded_count": 0,
            "reused_count": len(slugs),
            "dim": dim,
            "wrote_index": False,
            "full_rebuild": False,
        }

    fresh: dict[str, "np.ndarray"] = {}
    if to_embed_texts:
        new_embs = _run_embed(embed_fn, to_embed_texts, dim)
        for slug, emb in zip(to_embed_slugs, new_embs):
            fresh[slug] = emb

    ordered_slugs = sorted(slugs)
    embeddings = np.zeros((len(ordered_slugs), dim), dtype=np.float32)
    out_hashes: list[str] = []
    out_mtimes: list[int] = []
    out_sizes: list[int] = []
    for i, slug in enumerate(ordered_slugs):
        if slug in fresh:
            embeddings[i] = fresh[slug]
        else:
            embeddings[i] = reused[slug]
        mtime, size = stamps[slug]
        out_mtimes.append(mtime)
        out_sizes.append(size)
        if slug in to_embed_slugs:
            out_hashes.append(to_embed_hashes[to_embed_slugs.index(slug)])
        elif slug in reused and slug not in cached:
            # Unreachable in practice (every reused slug is either a stamp
            # hit with a cache entry or a hash-confirmed re-read); guarded
            # so a future code path cannot emit a wrong hash.
            out_hashes.append(_content_hash(_read_text(by_path[slug])))
        else:
            out_hashes.append(cached[slug][3])

    # Single write for the whole rebuild.
    _write_index_atomic(output_path, ordered_slugs, embeddings, out_mtimes, out_sizes, out_hashes, dim)
    return {
        "output_path": output_path,
        "n_lessons": len(ordered_slugs),
        "encoded_count": len(to_embed_slugs),
        "reused_count": len(reused),
        "dim": dim,
        "wrote_index": True,
        "full_rebuild": False,
    }


def build_or_update_index(
    lessons_dir: str,
    output_path: str,
    embed_fn=None,
    dim: int = DEFAULT_DIM,
    force_rebuild: bool = False,
) -> dict:
    """Alias mirroring the reference builder's entry-point name."""
    if force_rebuild:
        return build_full(lessons_dir, output_path, embed_fn=embed_fn, dim=dim)
    return build_incremental(lessons_dir, output_path, embed_fn=embed_fn, dim=dim)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Incrementally rebuild the semantic index.")
    parser.add_argument("lessons_dir", nargs="?", default=None)
    parser.add_argument("output_path", nargs="?", default=None)
    parser.add_argument("--full", "--force", action="store_true", dest="full",
                        help="Full rebuild: embed every file.")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    args = parser.parse_args(argv)
    root = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT")
    lessons_dir = args.lessons_dir or (os.path.join(root, "memory", "lessons") if root else None)
    output_path = args.output_path or (os.path.join(root, "memory", "attention", "index.npz") if root else None)
    if not lessons_dir or not output_path:
        print("usage: build_index.py [lessons_dir output_path] [--full] [--dim N]",
              file=sys.stderr)
        return 2
    if args.full:
        res = build_full(lessons_dir, output_path, dim=args.dim)
    else:
        res = build_incremental(lessons_dir, output_path, dim=args.dim)
    print(
        f"Index: {res['n_lessons']} lessons "
        f"({res['encoded_count']} encoded, {res['reused_count']} reused, "
        f"wrote={res['wrote_index']}) -> {res['output_path']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
