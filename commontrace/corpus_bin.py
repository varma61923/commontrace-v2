"""Persisted corpus index for CommonTrace lexical retrieval."""
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


def _tag(scorer: str) -> bytes:
    from commontrace import __version__

    return f"{scorer}|{__version__}".encode("utf-8")


def _fingerprint_entries(fingerprint) -> list[tuple[str, int, int]]:
    return [(str(p), int(m), int(s)) for p, (m, s) in fingerprint]


def save(cache_dir: str, scorer: str, fingerprint, index) -> bool:
    """Persist a built `_CorpusIndex`. Best-effort: False on any failure."""
    try:
        entries = _fingerprint_entries(fingerprint)
        scorer_b = _tag(scorer)
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
    """Load a persisted index, or None when it is missing/invalid/stale."""
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
            return None
        stored_tag = blob[off:off + scorer_len]
        off += scorer_len
        if stored_tag != _tag(scorer):
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
