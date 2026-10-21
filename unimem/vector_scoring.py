"""Bounded exact cosine top-k for little-endian float32 vector storage.

On audited CPython 3.12–3.14 builds the extended-precision dot product screens rows that
cannot enter the heap. Competitive rows always use the established ``fsum``
score, preserving returned scores and lexicographic ties. Older interpreters
use the same exact scorer without screening. No vector/text cache is retained.
"""
from __future__ import annotations

import heapq
import math
import struct
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import cast

_Dot = Callable[[Sequence[float], Sequence[float]], float]

def _native_dot() -> _Dot | None:
    precision = sys.float_info
    if sys.implementation.name != "cpython" or not (3, 12) <= sys.version_info[:2] <= (3, 14) \
            or (precision.radix, precision.mant_dig, precision.max_exp, precision.min_exp, precision.rounds) \
            != (2, 53, 1024, -1021, 1):
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


def cosine_topk(rows: Iterable[tuple[str, bytes]], query: tuple[float, ...], *,
                dimension: int, top_k: int) -> list[tuple[str, float]]:
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
    query_norm = math.hypot(*query)
    margin = 64 * (dimension + 1) * sys.float_info.epsilon
    unpack = struct.Struct(f"<{dimension}f").unpack
    heap: list[_Candidate] = []
    dot = _SUMPROD
    for key, blob in rows:
        if len(blob) != dimension * 4:
            raise ValueError("stored vector dimension is corrupt")
        stored = unpack(blob)
        norm = math.hypot(*stored) * query_norm
        if not norm or not math.isfinite(norm):
            raise ValueError("stored vector is corrupt")
        if dot is not None and len(heap) == top_k:
            estimate = dot(stored, query) / norm
            if math.isfinite(estimate) and estimate + margin < heap[0].score:
                continue
        score = math.fsum(a * b for a, b in zip(stored, query)) / norm
        candidate = _Candidate(key, min(1.0, max(-1.0, score)))
        if len(heap) < top_k:
            heapq.heappush(heap, candidate)
        elif heap[0] < candidate:
            heapq.heapreplace(heap, candidate)
    return [(row.key, row.score) for row in sorted(heap, key=lambda row: (-row.score, row.key))]
