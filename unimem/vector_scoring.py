"""Bounded exact cosine top-k for little-endian float32 vector storage.

On audited CPython 3.12–3.14 builds the extended-precision dot product screens rows that
cannot enter the heap. Competitive rows always use the established ``fsum``
score, preserving returned scores and lexicographic ties. Older interpreters
use the same exact scorer without screening. No vector/text cache is retained.
"""

from __future__ import annotations

import heapq
import importlib
import itertools
import math
import struct
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

_Dot = Callable[[Sequence[float], Sequence[float]], float]


def _native_dot() -> _Dot | None:
    precision = sys.float_info
    if (
        sys.implementation.name != "cpython"
        or not (3, 12) <= sys.version_info[:2] <= (3, 14)
        or (precision.radix, precision.mant_dig, precision.max_exp, precision.min_exp, precision.rounds)
        != (2, 53, 1024, -1021, 1)
    ):
        return None
    return cast(_Dot | None, getattr(math, "sumprod", None))


_SUMPROD = _native_dot()


@dataclass(slots=True)
class _Candidate:
    key: str
    score: float

    def __lt__(self, other: _Candidate) -> bool:
        # The root is the worst retained row, including the reverse key tie.
        return self.score < other.score or (self.score == other.score and self.key > other.key)


def cosine_topk(
    rows: Iterable[tuple[str, bytes]], query: tuple[float, ...], *, dimension: int, top_k: int
) -> list[tuple[str, float]]:
    """Scan bounded caller-supplied pages; retain O(top_k + dimension) values.

    Callers validate limits and quantize the query to finite, nonzero float32.
    Row validation still runs even when that row cannot enter the result.

    Float32 products are exact, normal binary64 values (or zero); n<=2000
    prevents overflow. CPython 3.12–3.14's tl_fma reduces to SumKVert K=3
    because product residuals are zero. On ordinary IEEE nearest-rounding
    builds its dot error is <2*u*A, where u=epsilon/2 and A=sum(abs(a*b))
    (Ogita, Rump and Oishi, Proposition 4.10, doi:10.1137/030601818).
    Cauchy-Schwarz, hypot's <1-ulp norm error, the fsum double-round allowance
    and both divisions bound screening/reference cosine deviation by <10*u;
    adding the margin costs <2*u. Even n=1 gives a margin of 256*u. Strict
    rejection preserves improvements and equal-score key ties. Nonstandard
    startup float formats/rounding and unaudited interpreters use fsum only;
    applications must preserve ordinary nearest rounding during execution.
    """
    if top_k == 0:
        return []
    iterator = iter(rows)
    first = list(itertools.islice(iterator, 256))
    rows = itertools.chain(first, iterator)
    query_norm = math.hypot(*query)
    if len(first) == 256 and dimension >= 32 and _SUMPROD is not None and len(query) == dimension:
        try:
            np = importlib.import_module("numpy")
        except ImportError:
            pass
        else:
            return _blocked_topk(rows, query, query_norm, dimension, top_k, np)
    margin = 64 * (dimension + 1) * sys.float_info.epsilon
    unpack = struct.Struct(f"<{dimension}f").unpack
    heap: list[_Candidate] = []
    dot = _SUMPROD
    memo: OrderedDict[bytes, float] = OrderedDict()
    for key, blob in rows:
        if len(blob) != dimension * 4:
            raise ValueError("stored vector dimension is corrupt")
        score = memo.get(blob)
        if score is None:
            stored = unpack(blob)
            norm = math.hypot(*stored) * query_norm
            if not norm or not math.isfinite(norm):
                raise ValueError("stored vector is corrupt")
            if dot is not None and len(heap) == top_k:
                estimate = dot(stored, query) / norm
                if math.isfinite(estimate) and estimate + margin < heap[0].score:
                    continue
            score = math.fsum(a * b for a, b in zip(stored, query)) / norm
            memo[blob] = score
            if len(memo) > 128:
                memo.popitem(last=False)
        candidate = _Candidate(key, min(1.0, max(-1.0, score)))
        if len(heap) < top_k:
            heapq.heappush(heap, candidate)
        elif heap[0] < candidate:
            heapq.heapreplace(heap, candidate)
    return [(row.key, row.score) for row in sorted(heap, key=lambda row: (-row.score, row.key))]


def _blocked_topk(
    rows: Iterable[tuple[str, bytes]],
    query: tuple[float, ...],
    query_norm: float,
    dimension: int,
    top_k: int,
    np: Any,
) -> list[tuple[str, float]]:
    """BLAS screens bounded float64 blocks; every retained score uses the exact oracle.

    Float32 inputs have exact binary64 products and squares. Standard dot/norm
    forward-error bounds are O(dimension * epsilon); the existing 64*(n+1)*eps
    allowance conservatively covers both and the final divisions. Clipping is
    nonexpansive. Never prune a boundary/tie, and validate even pruned rows.
    The byte memo is query-local and holds at most 128 vectors (<=1 MiB).
    """
    iterator = iter(rows)
    heap: list[_Candidate] = []
    memo: OrderedDict[bytes, float] = OrderedDict()
    unpack = struct.Struct(f"<{dimension}f").unpack
    target = np.asarray(query, dtype=np.float64)
    margin = 64 * (dimension + 1) * sys.float_info.epsilon

    def retain(blob: bytes, keys: Sequence[str]) -> None:
        score = memo.get(blob)
        if score is None:
            stored = unpack(blob)
            norm = math.hypot(*stored) * query_norm
            score = min(1.0, max(-1.0, math.fsum(a * b for a, b in zip(stored, query)) / norm))
            memo[blob] = score
            if len(memo) > 128:
                memo.popitem(last=False)
        for key in keys:
            candidate = _Candidate(key, score)
            if len(heap) < top_k:
                heapq.heappush(heap, candidate)
            elif heap[0] < candidate:
                heapq.heapreplace(heap, candidate)

    while block := list(itertools.islice(iterator, 512)):
        unique: dict[bytes, list[str]] = {}
        for key, blob in block:
            if len(blob) != dimension * 4:
                raise ValueError("stored vector dimension is corrupt")
            unique.setdefault(blob, []).append(key)
        pending = []
        for blob, keys in unique.items():
            if blob in memo:
                retain(blob, keys)
            else:
                pending.append(blob)
        if not pending:
            continue
        packed = np.frombuffer(b"".join(pending), dtype="<f4").reshape(-1, dimension)
        # Reject signaling NaNs before conversion can emit a floating-point
        # warning; corrupted storage must keep the same ValueError contract.
        if not np.all(np.isfinite(packed)):
            raise ValueError("stored vector is corrupt")
        matrix = packed.astype(np.float64)
        norms = np.sqrt(np.sum(matrix * matrix, axis=1)) * query_norm
        if not np.all(np.isfinite(norms) & (norms > 0)):
            raise ValueError("stored vector is corrupt")
        estimates = np.clip((matrix @ target) / norms, -1.0, 1.0)
        for index in np.argsort(-estimates, kind="stable"):
            if len(heap) == top_k and float(estimates[index]) + margin < heap[0].score:
                break
            blob = pending[index]
            retain(blob, unique[blob])
    return [(row.key, row.score) for row in sorted(heap, key=lambda row: (-row.score, row.key))]
