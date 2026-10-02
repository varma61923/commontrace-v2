"""Persisted corpus index for CommonTrace lexical retrieval.

`retrieval._build_index` reconstructs postings/doc-frequencies from the
per-lesson token streams on every call. Inside one process that work is
memoized, but every CLI invocation is a new process, so a 10k-lesson store
pays ~0.2s of index rebuild per query. This module persists the *built*
index to `.cache/corpus_<scorer>.bin` in a compact binary layout and loads
it back in tens of milliseconds.

Validity is exact, not heuristic: the file stores the ordered
(path, mtime_ns, size) fingerprint it was built from, and a load only
succeeds when the current corpus fingerprint matches entry-for-entry. Any
mismatch (edit, add, delete, reorder, scorer change, version change)
falls back to a rebuild. All floats are float64, so a loaded index scores
bit-identically to a freshly built one.

Version history:
- v1: postings + doc frequencies + n_terms + length factors + tie breaks.
- v2: same layout, with the doc-length array (`n_terms`, unique-term
  count per doc) promoted to a load-bearing contract: the BM25 scorer
  (`bm25-v1`) normalizes every term by `n_terms[i] / avg_field_len`, so a
  persisted index without an exact doc-length array is unusable for it.
  v1 blobs are rejected (callers rebuild + re-save as v2).
"""
from __future__ import annotations

import os
import struct
import tempfile

_MAGIC = b"CTCI"
_VERSION = 2

_HEADER = struct.Struct("<4sBB")
_SCORER_PREFIX = struct.Struct("<B")
_META = struct.Struct("<Idd")
_COUNT = struct.Struct("<I")
_PATH_STAMP = struct.Struct("<Hqq")
_TERM_HEAD = struct.Struct("<HII")
_DOC = struct.Struct("<I")
_F64 = struct.Struct("<d")
_I32 = struct.Struct("<i")


def bin_path(cache_dir: str, scorer: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", ".") else "_" for c in scorer)
    return os.path.join(cache_dir, f"corpus_{safe}.bin")


def _fingerprint_entries(fingerprint) -> list[tuple[str, int, int]]:
    return [(str(p), int(m), int(s)) for p, (m, s) in fingerprint]


def save(cache_dir: str, scorer: str, fingerprint, index) -> bool:
    """Persist a built `_CorpusIndex`. Best-effort: False on any failure."""
    try:
        entries = _fingerprint_entries(fingerprint)
        scorer_b = scorer.encode("utf-8")
        if len(scorer_b) > 255:
            return False
        chunks: list[bytes] = [
            _HEADER.pack(_MAGIC, _VERSION, len(scorer_b)),
            scorer_b,
            _META.pack(len(entries), index.avg_field_len, index.max_idf),
            _COUNT.pack(len(entries)),
        ]
        for path, mtime_ns, size in entries:
            pb = path.encode("utf-8")
            chunks.append(_PATH_STAMP.pack(len(pb), mtime_ns, size))
            chunks.append(pb)
        vocab = sorted(index.postings)
        chunks.append(_COUNT.pack(len(vocab)))
        for term in vocab:
            docs, wsums, bests = index.postings[term]
            tb = term.encode("utf-8")
            chunks.append(_TERM_HEAD.pack(len(tb), index.doc_freq[term], len(docs)))
            chunks.append(tb)
            chunks.append(struct.pack(f"<{len(docs)}I", *docs))
            chunks.append(struct.pack(f"<{len(docs)}d", *wsums))
            chunks.append(struct.pack(f"<{len(docs)}d", *bests))
        n = len(entries)
        chunks.append(struct.pack(f"<{n}I", *index.n_terms))
        chunks.append(struct.pack(f"<{n}d", *index.length_factors))
        chunks.append(struct.pack(
            f"<{n}i", *(t[0] for t in index.tie_breaks)))
        chunks.append(struct.pack(
            f"<{n}i", *(t[1] for t in index.tie_breaks)))
        blob = b"".join(chunks)
        os.makedirs(cache_dir, exist_ok=True)
        target = bin_path(cache_dir, scorer)
        fd, tmp = tempfile.mkstemp(
            dir=cache_dir, prefix=os.path.basename(target) + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError, TypeError, struct.error, AttributeError):
        return False


def load(cache_dir: str, scorer: str, lessons, fingerprint):
    """Load a persisted index, or None when it is missing/invalid/stale.

    `lessons` is the current ordered (path, fm) list; `fingerprint` is the
    ordered ((path, (mtime_ns, size)), ...) tuple from the term cache.
    Returns a `retrieval._CorpusIndex` on an exact fingerprint match.
    """
    from commontrace.retrieval import _CorpusIndex

    try:
        with open(bin_path(cache_dir, scorer), "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    try:
        off = 0
        magic, ver, scorer_len = _HEADER.unpack_from(blob, off)
        off += _HEADER.size
        if magic != _MAGIC or ver != _VERSION:
            # Wrong magic, or a v1 blob without the doc-length contract:
            # reject so the caller rebuilds from the term streams.
            return None
        stored_scorer = blob[off:off + scorer_len].decode("utf-8")
        off += scorer_len
        if stored_scorer != scorer:
            return None
        (n_docs, avg, max_idf) = _META.unpack_from(blob, off)
        off += _META.size
        if n_docs != len(lessons) or n_docs != len(fingerprint):
            return None
        (n_paths,) = _COUNT.unpack_from(blob, off)
        off += _COUNT.size
        if n_paths != n_docs:
            return None
        for i in range(n_docs):
            (plen, mtime_ns, size) = _PATH_STAMP.unpack_from(blob, off)
            off += _PATH_STAMP.size
            path = blob[off:off + plen].decode("utf-8")
            off += plen
            fpath, (fm, fs) = fingerprint[i]
            if path != fpath or mtime_ns != int(fm) or size != int(fs):
                return None
            if path != lessons[i][0]:
                return None
        (vocab_size,) = _COUNT.unpack_from(blob, off)
        off += _COUNT.size
        postings: dict = {}
        doc_freq: dict = {}
        for _ in range(vocab_size):
            (tlen, df, npost) = _TERM_HEAD.unpack_from(blob, off)
            off += _TERM_HEAD.size
            term = blob[off:off + tlen].decode("utf-8")
            off += tlen
            docs = struct.unpack_from(f"<{npost}I", blob, off)
            off += 4 * npost
            wsums = struct.unpack_from(f"<{npost}d", blob, off)
            off += 8 * npost
            bests = struct.unpack_from(f"<{npost}d", blob, off)
            off += 8 * npost
            postings[term] = (docs, wsums, bests)
            doc_freq[term] = df
        n_terms = list(struct.unpack_from(f"<{n_docs}I", blob, off))
        off += 4 * n_docs
        length_factors = tuple(struct.unpack_from(f"<{n_docs}d", blob, off))
        off += 8 * n_docs
        tie_imp = struct.unpack_from(f"<{n_docs}i", blob, off)
        off += 4 * n_docs
        tie_uses = struct.unpack_from(f"<{n_docs}i", blob, off)
        off += 4 * n_docs
        if off != len(blob):
            return None
        return _CorpusIndex(
            n_terms=n_terms,
            postings=postings,
            doc_freq=doc_freq,
            n_docs=n_docs,
            avg_field_len=avg,
            max_idf=max_idf,
            length_factors=length_factors,
            tie_breaks=tuple(zip(tie_imp, tie_uses)),
        )
    except (struct.error, UnicodeDecodeError, ValueError, IndexError):
        return None
