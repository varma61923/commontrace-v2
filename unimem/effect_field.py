"""Effect field: carry randomized evidence between memories that say the same thing.

The holdout experiment measures every memory on its own. That is the right unit of
evidence and the wrong unit of learning. A fleet distils the same lesson many times
in different words, and each paraphrase starts from nothing: a harmful idea re-enters
at the explore rate every time it is re-worded, and a proven one is withheld from
half the occasions until its copy proves itself again. Agent-memory research sees
the symptom (negative transfer from look-alike experiences, STRATA 2026;
context-conditioned memory value, BEAM 2026) but values memories by retrieval
success or a learned critic, not by randomized evidence.

The effect field is a Gaussian-process prior over memory content, fitted to the
randomized effects the causal gate already estimates:

    effect_i = base_i + f(x_i) + u_i,
    f ~ GP(0, share * tau^2 * k(x, x')),   u_i ~ N(0, (1 - share) * tau^2)

``base_i`` is the gate's own leave-one-out source/global prior and ``tau^2`` its
between-memory variance. ``k = cos(x, x') ** power`` is positive semidefinite on unit
vectors (Schur product theorem): sparse TF-IDF vectors by default (stdlib, offline,
deterministic) or caller-supplied embeddings. A memory's field prior is the GP's best
linear prediction from its nearest measured neighbours, never from its own data. It
uses the full neighbour covariance, so ten near-duplicates count as about one
witness, not ten. With ``share = 0`` it is exactly the gate's prior.

Transfer is earned, not assumed. ``power`` and ``share`` are chosen by
leave-family-out log predictive density. Copies and memories connected by shared
outcome units form one validation family, with one mean gain. The field is used
only when independent-family gains beat no transfer by a one-sided paired z test
Bonferroni-corrected over the whole grid. Unestimable variation never supplies
infinite evidence. Otherwise it is NOT_VALIDATED or INSUFFICIENT and changes nothing.

A validated field does two things. It becomes each measured memory's prior inside
`causal_gate.fit`. It also forecasts memories with no evidence yet, so a new
paraphrase of a proven-harmful lesson starts mostly withheld (never more than
``1 - floor``) and one of a proven helper starts mostly delivered. Every rate still
comes from a published, hash-chained schedule, so assignment stays randomized and
AIPW and the integrity audit are untouched. `lookalikes` reports the failure mode
the field cannot fix: highly similar memories whose randomized effects disagree in
sign. A similarity kernel always blurs these, so a reviewer should separate them.

What this is not: forecasts are allocation weights. A forecast never makes a memory
HELPS or HURTS, never withdraws or graduates anything, and never enters
`experiment.analyze`, billing or release evidence.
"""

from __future__ import annotations

import hashlib
import heapq
import importlib
import json
import math
import operator
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from numbers import Real
from statistics import NormalDist
from typing import Any

from unimem._lexical import STOPWORDS, WORD_RE, has_cjk, segment_cjk

NEIGHBORS = 12  # measured witnesses per memory; the GP solve is NEIGHBORS**3 per configuration
POWERS = (1, 2, 4)  # kernel sharpness: integer powers keep cos**p positive semidefinite
SHARES = (0.2, 0.4, 0.6, 0.8, 0.95)  # share of between-memory variance the field may explain
ALPHA = 0.05
MIN_MEASURED = 10  # measured memories with a witness, before a field may validate
LOOKALIKE_SIMILARITY = 0.6
MAX_TEXT_CHARS = 8000
JITTER = 1e-12
KERNEL_LEXICAL = "lexical-tfidf-v2"
MAX_BAG_TERMS = 256
MAX_SIMILARITY_CACHE = 65_536
KERNEL_CONTEXT = "context-components-v1"


def validate_z(powers: int = len(POWERS)) -> float:
    """One-sided z a configuration must reach, Bonferroni-corrected over the search grid."""
    if type(powers) is not int or powers <= 0:
        raise ValueError("power count must be a positive integer")
    return NormalDist().inv_cdf(1.0 - ALPHA / (powers * len(SHARES)))


VALIDATE_Z = validate_z()

VALIDATED = "VALIDATED"
NOT_VALIDATED = "NOT_VALIDATED"
INSUFFICIENT = "INSUFFICIENT"

Vector = dict[str, float]
Neighbors = tuple[tuple[str, float], ...]


# -- content kernel ------------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    """Casefolded content terms: Unicode words, CJK bigrams, no stopwords or bare numbers."""
    out: list[str] = []
    for word in WORD_RE.findall(text[:MAX_TEXT_CHARS].casefold()):
        for term in segment_cjk(word):
            if term not in STOPWORDS and not term.isdigit() and (len(term) > 1 or has_cjk(term)):
                out.append(term)
    return out


def term_hash(term: str) -> str:
    """A 48-bit keyed digest of a term: signatures keep counts of these, not words (still erasable data)."""
    return hashlib.blake2b(term.encode("utf-8"), digest_size=6, person=b"unimem-terms-v1").hexdigest()


def term_bag(text: str) -> dict[str, int]:
    """Hashed content-term counts of `text`, at most MAX_BAG_TERMS of the most frequent."""
    counts = Counter(term_hash(t) for t in tokens(text))
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_BAG_TERMS])


def _unit(weights: Mapping[str, float], scale: float = 1.0) -> Vector:
    norm = math.sqrt(math.fsum(w * w for w in weights.values()))
    return {k: scale * w / norm for k, w in sorted(weights.items()) if w} if norm > 0 else {}


def _dense_unit(values: Sequence[float], scale: float) -> tuple[float, ...]:
    try:
        if any(isinstance(x, bool) or not isinstance(x, Real) for x in values):
            raise ValueError("embedding values must be finite numbers")
        vector = tuple(float(x) for x in values)
    except (TypeError, OverflowError):
        raise ValueError("embedding values must be finite numbers") from None
    if not all(math.isfinite(x) for x in vector):
        raise ValueError("embedding values must be finite numbers")
    largest = max(map(abs, vector), default=0.0)
    if not largest:
        return ()
    # Scaling before squaring avoids overflow and underflow for finite vectors.
    vector = tuple(x / largest for x in vector)
    norm = math.sqrt(math.fsum(x * x for x in vector))
    return tuple(scale * x / norm for x in vector)


@dataclass(frozen=True)
class Kernel:
    """Cosine similarity between memories, over hashed content terms and any number of embedding spaces.

    A memory present in several spaces is the concatenation of its unit vectors, each
    scaled by ``1/sqrt(spaces)``, so the similarity of two memories is the sum of their
    per-space cosines divided by ``sqrt(spaces_a * spaces_b)``. Missing spaces
    contribute zero; this is a mean only for matching space memberships. The
    concatenated-vector Gram matrix stays positive semidefinite.
    """

    kind: str
    vectors: Mapping[str, Vector]
    dense: Mapping[str, Mapping[str, tuple[float, ...]]] = field(default_factory=dict)  # model -> memory -> vector

    @classmethod
    def lexical(cls, texts: Mapping[str, str]) -> Kernel:
        """Smoothed TF-IDF over `texts` (memory id -> text)."""
        return cls.combined({memory: term_bag(text) for memory, text in texts.items()})

    @classmethod
    def from_embeddings(cls, vectors: Mapping[str, Sequence[float]], kind: str = "dense") -> Kernel:
        """Caller-supplied embeddings (memory id -> vector) in one space named `kind`."""
        return cls.combined({}, {m: (kind, v) for m, v in vectors.items()})

    @classmethod
    def combined(
        cls,
        bags: Mapping[str, Mapping[str, int]],
        embeddings: Mapping[str, tuple[str, Sequence[float]]] | None = None,
    ) -> Kernel:
        """TF-IDF over hashed term `bags`, joined with `embeddings` (memory -> (model, vector))."""
        units = {m: (model, _dense_unit(vector, 1.0)) for m, (model, vector) in (embeddings or {}).items()}
        units = {m: (model, unit) for m, (model, unit) in units.items() if unit}
        df = Counter(term for bag in bags.values() for term in bag)
        n = len(bags)
        idf = {term: math.log((1.0 + n) / (1.0 + d)) + 1.0 for term, d in df.items()}
        spaces = Counter({m: 1 for m, bag in bags.items() if bag})
        spaces.update(units.keys())
        vectors = {
            m: _unit({t: (1.0 + math.log(c)) * idf[t] for t, c in bag.items() if c > 0}, 1.0 / math.sqrt(spaces[m]))
            for m, bag in bags.items()
            if bag
        }
        dense: dict[str, dict[str, tuple[float, ...]]] = defaultdict(dict)
        for m, (model, unit) in units.items():
            scale = 1.0 / math.sqrt(spaces[m])
            dense[model][m] = tuple(scale * x for x in unit)
        kind = KERNEL_LEXICAL + "".join(f"+{model}" for model in sorted(dense))
        return cls(kind, vectors, dict(dense))

    def similarity(self, a: str, b: str) -> float:
        total = 0.0
        va, vb = self.vectors.get(a), self.vectors.get(b)
        if va and vb:
            if len(va) > len(vb):
                va, vb = vb, va
            total += math.fsum(w * vb.get(t, 0.0) for t, w in va.items())
        for space in self.dense.values():
            da, db = space.get(a), space.get(b)
            if da and db and len(da) == len(db):
                total += math.fsum(x * y for x, y in zip(da, db))
        return max(-1.0, min(1.0, total))

    def has(self, memory: str) -> bool:
        return bool(self.vectors.get(memory)) or any(memory in space for space in self.dense.values())

    def neighbors(
        self,
        queries: Iterable[str],
        pool: Iterable[str],
        k: int = NEIGHBORS,
        *,
        families: Mapping[str, str] | None = None,
    ) -> dict[str, Neighbors]:
        """Each query's `k` most similar pool members (never itself).

        Term similarity goes through an inverted index; embedding similarity is a
        dense product, through numpy when it is installed.
        """
        if type(k) is not int or k < 0:
            raise ValueError("neighbor count must be a non-negative integer")
        members = sorted(set(pool))
        queries = list(dict.fromkeys(queries))
        if not k:
            return {q: () for q in queries}
        out: dict[str, Neighbors] = {}
        for query, scores in self._score_rows(queries, members):
            ranked = heapq.nsmallest(
                k,
                (
                    (m, score)
                    for m, score in scores.items()
                    if score > 1e-9 and m != query and (families is None or families[m] != families[query])
                ),
                key=lambda item: (-item[1], item[0]),
            )
            out[query] = tuple((m, round(min(1.0, score), 12)) for m, score in ranked)
        return out

    def similar_pairs(self, pool: Iterable[str], *, min_similarity: float) -> Iterable[tuple[str, str, float]]:
        """Every threshold-matching pair, independent of the display-neighbor cap.

        Scores are streamed in bounded batches. Only the statistical diagnostic's
        matching pairs need to be retained, rather than a full fleet Gram matrix.
        """
        if not math.isfinite(min_similarity) or not -1 <= min_similarity <= 1:
            raise ValueError("similarity threshold must be finite and between -1 and 1")
        members = sorted(set(pool))
        for query, scores in self._score_rows(members, members):
            candidates = (
                members
                if min_similarity <= 0
                else [m for m, score in scores.items() if score >= min_similarity - 1e-12]
            )
            for other in sorted(candidates):
                if other > query:
                    score = self.similarity(query, other)
                    if score >= min_similarity:
                        yield query, other, score

    def _score_rows(self, queries: Sequence[str], members: Sequence[str]) -> Iterable[tuple[str, dict[str, float]]]:
        postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for memory in members:
            for term, w in self.vectors.get(memory, {}).items():
                postings[term].append((memory, w))
        # Retain scores for a bounded query batch, rather than the entire Q*M fleet.
        for start in range(0, len(queries), 128):
            batch = queries[start : start + 128]
            scores: dict[str, dict[str, float]] = {q: defaultdict(float) for q in batch}
            for q in batch:
                for term, w in self.vectors.get(q, {}).items():
                    for memory, wm in postings.get(term, ()):
                        scores[q][memory] += w * wm
            for space in self.dense.values():
                _dense_scores(space, batch, members, scores)
            for q in batch:
                yield q, scores[q]

    def digest(self) -> str:
        """Commitment to the kernel's inputs, for a schedule's basis."""
        body = {
            "kind": self.kind,
            "vectors": {m: {t: round(w, 9) for t, w in v.items()} for m, v in sorted(self.vectors.items())},
            "dense": {
                model: {m: [round(x, 9) for x in v] for m, v in sorted(space.items())}
                for model, space in sorted(self.dense.items())
            },
        }
        payload = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dense_scores(
    space: Mapping[str, tuple[float, ...]],
    queries: Sequence[str],
    members: Sequence[str],
    scores: dict[str, dict[str, float]],
) -> None:
    qs = [q for q in queries if q in space]
    ms = [m for m in members if m in space]
    if not qs or not ms:
        return
    try:
        np: Any = importlib.import_module("numpy")
    except ImportError:
        for q in qs:
            vq = space[q]
            for m in ms:
                if len(vq) == len(space[m]):
                    scores[q][m] += math.fsum(x * y for x, y in zip(vq, space[m]))
        return
    by_width: dict[int, list[str]] = defaultdict(list)
    for m in ms:
        by_width[len(space[m])].append(m)
    for width, members_of_width in by_width.items():
        matching = [q for q in qs if len(space[q]) == width]
        # Bound the temporary product independently of fleet size. Preserve signed
        # contributions: a negative image cosine can cancel a positive text cosine.
        for start in range(0, len(matching), 128):
            batch = matching[start : start + 128]
            query_matrix = np.asarray([space[q] for q in batch], dtype=float)
            for offset in range(0, len(members_of_width), 512):
                block = members_of_width[offset : offset + 512]
                product = query_matrix @ np.asarray([space[m] for m in block], dtype=float).T
                for i, q in enumerate(batch):
                    for j in np.flatnonzero(product[i]):
                        scores[q][block[j]] += float(product[i, j])


@dataclass(frozen=True)
class CellKernel:
    """Similarity between (memory, context) cells: content similarity times context similarity.

    Content similarity comes from a memory `Kernel` (a memory is identical to itself;
    memories without a content vector match only themselves). Context similarity is
    the share of ``/``-separated components two contexts agree on, padded so that
    short labels match nothing else. A product of positive-semidefinite kernels is
    positive semidefinite, so a skill variant on ``jaw/depot/pick`` can learn from its
    siblings on ``jaw/yard/pick`` while a different skill on ``suction/lab/pick``
    teaches it almost nothing.
    """

    content: Kernel | None
    cells: Mapping[str, tuple[str, str]]  # cell id -> (memory, context)
    width: int = 1
    _cache: dict[frozenset[str], tuple[dict[str, list[str]], dict[str, Neighbors]]] = field(
        default_factory=dict, compare=False, repr=False
    )
    _sims: dict[tuple[str, str], float] = field(default_factory=dict, compare=False, repr=False)
    _shared: dict[tuple[str, str], float] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def build(cls, content: Kernel | None, cells: Mapping[str, tuple[str, str]]) -> CellKernel:
        width = max((len(context.split("/")) for _m, context in cells.values()), default=1)
        return cls(content, dict(cells), width)

    @property
    def kind(self) -> str:
        return (self.content.kind if self.content is not None else "identity") + "*" + KERNEL_CONTEXT

    def has(self, cell: str) -> bool:
        return cell in self.cells

    def _context(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        key = (a, b)
        cached = self._shared.get(key)
        if cached is not None:
            return cached
        pa, pb = a.split("/"), b.split("/")
        value = sum(1 for i in range(min(len(pa), len(pb))) if pa[i] == pb[i]) / self.width
        if len(self._shared) < MAX_SIMILARITY_CACHE:
            self._shared[key] = value
        return value

    def _memory(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        if self.content is None:
            return 0.0
        key = (a, b)
        cached = self._sims.get(key)
        if cached is not None:
            return cached
        value = self.content.similarity(a, b)
        if len(self._sims) < MAX_SIMILARITY_CACHE:
            self._sims[key] = value
        return value

    def similarity(self, a: str, b: str) -> float:
        if a not in self.cells or b not in self.cells:
            return 0.0
        (ma, ca), (mb, cb) = self.cells[a], self.cells[b]
        return self._memory(ma, mb) * self._context(ca, cb)

    def _pool(self, pool: Iterable[str], memories: Iterable[str]) -> tuple[dict[str, list[str]], dict[str, Neighbors]]:
        """The pool's cells by memory, and each memory's content neighbours among the pool's memories."""
        members = frozenset(cell for cell in pool if cell in self.cells)
        if members not in self._cache:
            by_memory: dict[str, list[str]] = defaultdict(list)
            for cell in sorted(members):
                by_memory[self.cells[cell][0]].append(cell)
            roster = sorted(by_memory)
            near = self.content.neighbors(roster, roster, NEIGHBORS) if self.content is not None else {}
            self._cache[members] = (by_memory, near)
        by_memory, near = self._cache[members]
        missing = sorted({m for m in memories if m not in near})
        if missing and self.content is not None:
            near.update(self.content.neighbors(missing, sorted(by_memory), NEIGHBORS))
        return by_memory, near

    def neighbors(
        self,
        queries: Iterable[str],
        pool: Iterable[str],
        k: int = NEIGHBORS,
        *,
        families: Mapping[str, str] | None = None,
    ) -> dict[str, Neighbors]:
        """Each query cell's `k` most similar pool cells: its own memory's other contexts and its
        content neighbours' contexts."""
        if type(k) is not int or k < 0:
            raise ValueError("neighbor count must be a non-negative integer")
        queries = list(queries)
        if not k:
            return {q: () for q in queries}
        by_memory, near = self._pool(pool, (self.cells[q][0] for q in queries if q in self.cells))
        out: dict[str, Neighbors] = {}
        for q in queries:
            if q not in self.cells:
                out[q] = ()
                continue
            memory, context = self.cells[q]
            nearest = near.get(memory, ())
            limit = max(NEIGHBORS, len(nearest))
            visited: set[str] = set()
            ranked: list[tuple[str, float]] = []
            while True:

                def scored() -> Iterable[tuple[str, float]]:
                    yield from ranked
                    for other, weight in ((memory, 1.0), *nearest):
                        if other in visited:
                            continue
                        visited.add(other)
                        for cell in by_memory.get(other, ()):
                            if cell != q and (families is None or families[cell] != families[q]):
                                s = weight * self._context(context, self.cells[cell][1])
                                if s > 1e-9:
                                    yield cell, round(min(1.0, s), 12)

                ranked = heapq.nsmallest(k, scored(), key=lambda item: (-item[1], item[0]))
                # Content cosine upper-bounds every cell's product similarity.
                # Expand until the remaining memories cannot beat the kth cell;
                # equal scores must still be visited for deterministic ID ties.
                if (
                    self.content is None
                    or len(nearest) < limit
                    or limit >= len(by_memory)
                    or (len(ranked) == k and ranked[-1][1] > nearest[-1][1])
                ):
                    break
                limit = min(len(by_memory), max(limit * 2, k))
                # Keep expansions local: retaining every full roster per memory
                # would turn the bounded pool cache into quadratic storage.
                nearest = self.content.neighbors([memory], sorted(by_memory), limit)[memory]
            out[q] = tuple(ranked)
        return out


# -- Gaussian-process transfer -------------------------------------------------------------------


def _cholesky(a: list[list[float]]) -> list[list[float]] | None:
    n = len(a)
    low = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - math.fsum(map(operator.mul, low[i][:j], low[j][:j]))
            if i == j:
                if s <= 0.0:
                    return None
                low[i][i] = math.sqrt(s)
            else:
                low[i][j] = s / low[j][j]
    return low


def _forward(low: list[list[float]], b: Sequence[float]) -> list[float]:
    x: list[float] = []
    for i, row in enumerate(low):
        x.append((b[i] - math.fsum(map(operator.mul, row[:i], x))) / row[i])
    return x


@dataclass(frozen=True)
class _Neighborhood:
    ids: tuple[str, ...]
    to_query: tuple[float, ...]  # similarity of each witness to the memory
    among: tuple[tuple[float, ...], ...]  # similarity between witnesses


def _transfer(
    hood: _Neighborhood,
    power: int,
    share: float,
    scale: float,
    scales: Mapping[str, float],
    residual: Mapping[str, float],
    noise: Mapping[str, float],
) -> tuple[float, float]:
    """(shift of the prior mean, reduction of the prior variance) from the witnesses' residuals.

    Item i's deviation has prior sd `scales[i]` (`scale` for the query); a `share` of its
    variance is the field, with covariance ``share * s_i * s_j * k_ij ** power``.
    """
    if share <= 0.0 or not hood.ids:
        return 0.0, 0.0
    sd = [scales[m] for m in hood.ids]
    n = len(hood.ids)
    a = [
        [
            sd[i] ** 2 + noise[hood.ids[i]] + JITTER if i == j else share * sd[i] * sd[j] * hood.among[i][j] ** power
            for j in range(n)
        ]
        for i in range(n)
    ]
    low = _cholesky(a)
    if low is None:
        return 0.0, 0.0
    lc = _forward(low, [share * scale * sd[j] * s**power for j, s in enumerate(hood.to_query)])
    ly = _forward(low, [residual[m] for m in hood.ids])
    return math.fsum(x * y for x, y in zip(lc, ly)), min(share * scale * scale, math.fsum(x * x for x in lc))


def _transfer_engine(
    hoods: Mapping[str, _Neighborhood],
    scales: Mapping[str, float],
    residual: Mapping[str, float],
    noise: Mapping[str, float],
) -> Callable[[int, float], dict[str, tuple[float, float]]]:
    """Prepare bounded batches once for the search grid; preserve the stdlib oracle."""

    def scalar(power: int, share: float) -> dict[str, tuple[float, float]]:
        return {q: _transfer(hood, power, share, scales[q], scales, residual, noise) for q, hood in hoods.items()}

    if len(hoods) < 32:
        return scalar
    try:
        np: Any = importlib.import_module("numpy")
    except ImportError:
        return scalar
    by_size: dict[int, list[str]] = defaultdict(list)
    for q, hood in hoods.items():
        if hood.ids:
            by_size[len(hood.ids)].append(q)
    batches = []
    for size, names in by_size.items():
        batch_size = max(1, min(256, 65_536 // (size * size)))
        for start in range(0, len(names), batch_size):
            qs = names[start : start + batch_size]
            sd = np.asarray([[scales[m] for m in hoods[q].ids] for q in qs], dtype=float)
            diagonal = sd * sd + np.asarray([[noise[m] for m in hoods[q].ids] for q in qs]) + JITTER
            among = np.asarray([hoods[q].among for q in qs], dtype=float)
            to_query = np.asarray([hoods[q].to_query for q in qs], dtype=float)
            ys = np.asarray([[residual[m] for m in hoods[q].ids] for q in qs], dtype=float)
            scale = np.asarray([scales[q] for q in qs], dtype=float)
            batches.append((qs, sd, diagonal, among, to_query, ys, scale))

    def solve(power: int, share: float) -> dict[str, tuple[float, float]]:
        output = {q: (0.0, 0.0) for q in hoods}
        if share <= 0:
            return output
        for qs, sd, diagonal, among, to_query, ys, scale in batches:
            covariance = share * sd[:, :, None] * sd[:, None, :] * among**power
            index = np.arange(sd.shape[1])
            covariance[:, index, index] = diagonal
            rhs = np.stack((share * scale[:, None] * sd * to_query**power, ys), axis=-1)
            try:
                low = np.linalg.cholesky(covariance)
                solved = np.linalg.solve(low, rhs)
            except np.linalg.LinAlgError:
                # A single ill-conditioned neighborhood must not disable the
                # other queries or change the existing failed-factor behavior.
                output.update({q: _transfer(hoods[q], power, share, scales[q], scales, residual, noise) for q in qs})
                continue
            shifts = np.sum(solved[:, :, 0] * solved[:, :, 1], axis=1)
            reductions = np.minimum(share * scale * scale, np.sum(solved[:, :, 0] ** 2, axis=1))
            output.update((q, (float(shifts[i]), float(reductions[i]))) for i, q in enumerate(qs))
        return output

    return solve


def _log_normal(x: float, mean: float, variance: float) -> float:
    return -0.5 * (math.log(2.0 * math.pi * variance) + (x - mean) ** 2 / variance)


@dataclass(frozen=True)
class ValidationCase:
    """A held-out query's training-only nuisance fit and witness pool."""

    neighbors: Neighbors
    base: tuple[float, float]
    reference: tuple[float, float]
    scales: Mapping[str, float]
    residuals: Mapping[str, float]
    noise: Mapping[str, float]


@dataclass
class Field:
    """A fitted effect field. Its prior equals the base prior unless the field validated."""

    verdict: str
    kernel: str
    power: int = POWERS[0]
    share: float = 0.0
    tau2: float = 0.0
    measured: int = 0
    lpd_gain: float | None = None  # mean leave-one-out log predictive gain per memory with a witness
    z: float | None = None
    threshold: float = VALIDATE_Z
    validation_units: int = 0
    _hoods: dict[str, _Neighborhood] = field(default_factory=dict, repr=False)
    _residual: dict[str, float] = field(default_factory=dict, repr=False)
    _noise: dict[str, float] = field(default_factory=dict, repr=False)
    _scales: dict[str, float] = field(default_factory=dict, repr=False)
    _predictions: dict[str, tuple[float, float]] = field(default_factory=dict, repr=False)
    _prediction_key: tuple[int, float, float] | None = field(default=None, repr=False)
    _cases: Mapping[str, ValidationCase] = field(default_factory=dict, repr=False)

    def _scale(self, memory: str) -> float:
        return self._scales.get(memory, math.sqrt(self.tau2))

    @property
    def validated(self) -> bool:
        return self.verdict == VALIDATED

    def informs(self, memory: str) -> bool:
        """Does the validated field have a measured witness for `memory`?"""
        return self.validated and bool(self._hoods.get(memory, _EMPTY).ids)

    def witnesses(self, memory: str) -> Neighbors:
        hood = self._hoods.get(memory, _EMPTY)
        return tuple((identity, round(similarity, 12)) for identity, similarity in zip(hood.ids, hood.to_query))

    def prior(self, memory: str, base_mean: float, base_var: float) -> tuple[float, float]:
        """`memory`'s prior (mean, variance): the base prior moved by its witnesses' evidence."""
        if not self.validated:
            return base_mean, base_var
        cached = self._predictions.get(memory) if self._prediction_key == (self.power, self.share, self.tau2) else None
        case = self._cases.get(memory)
        shift, reduction = (
            cached
            if cached is not None
            else _transfer(
                self._hoods.get(memory, _EMPTY),
                self.power,
                self.share,
                case.scales[memory] if case else self._scale(memory),
                case.scales if case else self._scales,
                case.residuals if case else self._residual,
                case.noise if case else self._noise,
            )
        )
        return base_mean + shift, max(base_var - reduction, JITTER)

    def summary(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "kernel": self.kernel,
            "power": self.power,
            "share": self.share,
            "measured": self.measured,
            "validation_units": self.validation_units,
            "lpd_gain": None if self.lpd_gain is None else round(self.lpd_gain, 6),
            "z": None if self.z is None or not math.isfinite(self.z) else round(self.z, 4),
            "threshold": round(self.threshold, 4),
        }


_EMPTY = _Neighborhood((), (), ())


@dataclass
class _KernelGram:
    index: dict[str, int]
    values: Any
    margin: float
    np: Any
    lexical: Any
    terms: dict[str, int]

    def neighbors(
        self,
        kernel: Kernel,
        queries: Sequence[str],
        members: Sequence[str],
        k: int,
        *,
        families: Mapping[str, str] | None = None,
    ) -> dict[str, Neighbors]:
        """Use a conservative shortlist, then the original scalar ranking/ties."""
        result = {}
        columns = self.np.asarray([self.index[m] for m in members], dtype=int)
        positions = {m: i for i, m in enumerate(members)}
        family_columns: dict[str, list[int]] = defaultdict(list)
        if families is not None:
            for m, i in positions.items():
                family_columns[families[m]].append(i)
        for q in queries:
            row = self.values[self.index[q], columns].copy()
            if q in positions:
                row[positions[q]] = -self.np.inf
            if families is not None:
                row[family_columns.get(families[q], [])] = -self.np.inf
            count = min(k, len(members))
            cutoff = float(self.np.partition(row, len(row) - count)[len(row) - count]) if count else self.np.inf
            selected = self.np.flatnonzero(row >= max(1e-9 - self.margin, cutoff - self.margin))
            # add.accumulate preserves the public inverted-index left-to-right
            # additions. Pairwise sum/fsum would reorder last-bit ties.
            terms = self.np.asarray([self.terms[t] for t in kernel.vectors.get(q, {})], dtype=int)
            if len(terms) and len(selected):
                products = self.lexical[columns[selected, None], terms[None, :]] * self.lexical[self.index[q], terms]
                scores = self.np.add.accumulate(products, axis=1)[:, -1]
                exact = [(members[i], float(s)) for i, s in zip(selected, scores)]
            else:
                exact = []
            result[q] = tuple(
                (m, round(min(1.0, s), 12))
                for m, s in heapq.nsmallest(
                    k, ((m, s) for m, s in exact if s > 1e-9), key=lambda item: (-item[1], item[0])
                )
            )
        return result


def _kernel_gram(kernel: Kernel | CellKernel, names: Sequence[str]) -> _KernelGram | None:
    """Request-local Gram matrix under explicit dimension and allocation budgets."""
    if not isinstance(kernel, Kernel) or not 32 <= len(names) <= 1024:
        return None
    terms = sorted({term for m in names for term in kernel.vectors.get(m, {})})
    spaces: dict[tuple[str, int], list[str]] = defaultdict(list)
    for model, vectors in kernel.dense.items():
        for m in names:
            if m in vectors:
                spaces[model, len(vectors[m])].append(m)
    width = len(terms) + sum(size for _model, size in spaces)
    if len(names) * width > 2_000_000:
        return None
    try:
        np: Any = importlib.import_module("numpy")
    except ImportError:
        return None
    index = {m: i for i, m in enumerate(names)}
    gram = np.zeros((len(names), len(names)), dtype=float)
    columns = {t: j for j, t in enumerate(terms)}
    lexical = None
    if terms:
        matrix = np.zeros((len(names), len(terms)), dtype=float)
        for m, i in index.items():
            for t, w in kernel.vectors.get(m, {}).items():
                matrix[i, columns[t]] = w
        gram += matrix @ matrix.T
        lexical = matrix
    for (model, _size), members in spaces.items():
        matrix = np.asarray([kernel.dense[model][m] for m in members], dtype=float)
        selected = np.asarray([index[m] for m in members], dtype=int)
        gram[np.ix_(selected, selected)] += matrix @ matrix.T
    return _KernelGram(
        index, np.clip(gram, -1.0, 1.0), 64 * (width + 1) * float(np.finfo(float).eps), np, lexical, columns
    )


def _hood(
    kernel: Kernel | CellKernel,
    neighbors: Neighbors,
    cache: dict[tuple[str, str], float] | None = None,
    gram: _KernelGram | None = None,
    *,
    query: str | None = None,
) -> _Neighborhood:
    ids = tuple(m for m, _s in neighbors)
    if gram is not None and ids:
        indices = [gram.index[m] for m in ids]
        values = gram.values[gram.np.ix_(indices, indices)]
        gram.np.fill_diagonal(values, 1.0)
        to_query = (
            tuple(float(gram.values[gram.index[query], index]) for index in indices)
            if query is not None
            else tuple(s for _, s in neighbors)
        )
        return _Neighborhood(ids, to_query, tuple(map(tuple, values.tolist())))
    to_query = tuple(
        kernel.similarity(query, memory) if query is not None else similarity for memory, similarity in neighbors
    )
    memo = {} if cache is None else cache

    def similarity(a: str, b: str) -> float:
        if a == b:
            return 1.0
        key = (a, b) if a < b else (b, a)
        if key not in memo:
            value = kernel.similarity(a, b)
            if len(memo) >= MAX_SIMILARITY_CACHE:
                return value
            memo[key] = value
        return memo[key]

    among = tuple(tuple(similarity(a, b) for b in ids) for a in ids)
    return _Neighborhood(ids, to_query, among)


def _validation_families(
    names: Sequence[str], kernel: Kernel | CellKernel, units: Mapping[str, Iterable[str]] | None
) -> dict[str, str]:
    """Connected evidence units and identical content profiles cannot be replicated.

    A shared occasion joins its memories transitively. Exact content copies are
    one family even when identifiers differ. Work is linear in memberships and
    vector dimensions, without materializing a pairwise dependence matrix.
    """
    parent = {name: name for name in names}

    def root(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    owners: dict[tuple[Any, ...], str] = {}
    for name in names:
        memory, context = kernel.cells[name] if isinstance(kernel, CellKernel) else (name, "")
        content = kernel.content if isinstance(kernel, CellKernel) else kernel
        profile: tuple[Any, ...] = (
            (memory,)
            if content is None
            else (
                tuple((t, round(w, 12)) for t, w in sorted(content.vectors.get(memory, {}).items())),
                tuple(
                    (model, tuple(round(x, 12) for x in space[memory]))
                    for model, space in sorted(content.dense.items())
                    if memory in space
                ),
            )
        )
        tokens: list[tuple[Any, ...]] = [("content", context, profile)]
        if units is not None:
            if name not in units:
                raise ValueError("validation units are required for every measured memory")
            tokens.extend(("occasion", unit) for unit in units[name])
        for token in tokens:
            previous = owners.setdefault(token, name)
            a, b = root(name), root(previous)
            if a != b:
                parent[max(a, b)] = min(a, b)
    return {name: root(name) for name in names}


def fit(
    estimates: Mapping[str, tuple[float, float]],
    bases: Mapping[str, tuple[float, float]],
    tau2: float,
    kernel: Kernel | CellKernel,
    *,
    candidates: Iterable[str] = (),
    k: int = NEIGHBORS,
    powers: Sequence[int] = POWERS,
    baseline: Mapping[str, tuple[float, float]] | None = None,
    scales: Mapping[str, float] | None = None,
    targets: Iterable[str] | None = None,
    units: Mapping[str, Iterable[str]] | None = None,
    validation_cases: Mapping[str, ValidationCase] | None = None,
) -> Field:
    """Fit and validate the field over measured `estimates` (memory -> (effect, variance)).

    `bases` holds the gate's leave-one-out prior (mean, variance) for every measured
    memory and every candidate; `tau2` is the gate's between-memory variance. The
    validation threshold is Bonferroni-corrected over ``len(powers) * len(SHARES)``.
    `baseline` is the prior used when the field does not validate, if it differs
    from `bases`; the field must beat that prior, not merely its own base. `scales`
    gives an item its own prior sd of deviation (default ``sqrt(tau2)`` for all).
    `targets` restricts which measured items are scored and given priors (default: all).
    `units` maps measured IDs to shared occasion/dependence tokens. Content copies
    and shared units form connected families; validation excludes the query's
    entire family. Forecasting still uses the full measured witness pool.
    `validation_cases` supplies training-only nuisance fits for cross-fitted
    context predictions. Those cases also supply the deployed priors, so a
    context never re-enters its own witnesses or variance estimate.
    """
    if type(k) is not int or k < 0:
        raise ValueError("neighbor count must be a non-negative integer")
    if not powers or any(type(power) is not int or power <= 0 for power in powers):
        raise ValueError("kernel powers must be a nonempty sequence of positive integers")
    reference = bases if baseline is None else baseline
    threshold = validate_z(len(powers))
    measured = sorted(m for m in estimates if m in bases and kernel.has(m))
    families = _validation_families(measured, kernel, units)
    wanted = set(measured) if targets is None else set(targets) & set(measured)
    queries = sorted(wanted | {c for c in candidates if c in bases and kernel.has(c)})
    similarities: dict[tuple[str, str], float] = {}
    gram = (
        _kernel_gram(kernel, sorted(set(queries) | set(measured)))
        if len({families[m] for m in wanted}) >= MIN_MEASURED and tau2 > 0
        else None
    )
    if validation_cases is not None:
        if set(queries) - validation_cases.keys():
            raise ValueError("training-only validation cases are required for every query")
        near = {q: validation_cases[q].neighbors for q in queries}
        for q, neighbors in near.items():
            if any(m not in families or (q in families and families[m] == families[q]) for m, _ in neighbors):
                raise ValueError("validation witnesses must exclude the query's complete family")
    else:
        near = (
            gram.neighbors(kernel, queries, measured, k)
            if gram is not None and isinstance(kernel, Kernel) and not kernel.dense
            else kernel.neighbors(queries, measured, k)
        )
    hoods = {q: _hood(kernel, nb, similarities, gram, query=q) for q, nb in near.items()}
    residual = {m: estimates[m][0] - bases[m][0] for m in measured}
    noise = {m: estimates[m][1] for m in measured}
    default = math.sqrt(max(tau2, 0.0))
    scale = {q: float((scales or {}).get(q, default)) for q in {*queries, *measured}}
    out = Field(
        INSUFFICIENT,
        kernel.kind,
        tau2=tau2,
        threshold=threshold,
        _hoods=hoods,
        _residual=residual,
        _noise=noise,
        _scales=scale,
        _cases=validation_cases or {},
    )
    if validation_cases is not None or len(set(families.values())) == len(measured):
        validation_hoods = {m: hoods[m] for m in wanted}
    else:
        # Filter families in one bounded score pass. Constructing a separate
        # full pool/index for every family would add quadratic Python work.
        validation_near = (
            gram.neighbors(kernel, sorted(wanted), measured, k, families=families)
            if gram is not None and isinstance(kernel, Kernel) and not kernel.dense
            else kernel.neighbors(sorted(wanted), measured, k, families=families)
        )
        validation_hoods = {m: _hood(kernel, nb, similarities, gram, query=m) for m, nb in validation_near.items()}
    scored = [m for m in sorted(wanted) if validation_hoods[m].ids]
    out.measured = len(scored)
    groups: dict[str, list[int]] = defaultdict(list)
    for i, memory in enumerate(scored):
        groups[families[memory]].append(i)
    out.validation_units = len(groups)
    if len(groups) < MIN_MEASURED or tau2 <= 0.0 or min(scale.values(), default=0.0) <= 0.0:
        return out
    engines = []
    if validation_cases is None:
        engines.append(_transfer_engine({q: validation_hoods[q] for q in scored}, scale, residual, noise))
    else:
        batches: dict[int, list[str]] = defaultdict(list)
        for q in queries:
            batches[id(validation_cases[q].scales)].append(q)
        for members in batches.values():
            case = validation_cases[members[0]]
            engines.append(_transfer_engine({q: hoods[q] for q in members}, case.scales, case.residuals, case.noise))

    def predictions(power: int, share: float) -> dict[str, tuple[float, float]]:
        return {q: value for engine in engines for q, value in engine(power, share).items()}

    references = {m: validation_cases[m].reference if validation_cases else reference[m] for m in scored}
    baseline_lpd = {
        m: _log_normal(estimates[m][0], references[m][0], references[m][1] + estimates[m][1]) for m in scored
    }

    def gains(power: int, share: float) -> list[float]:
        row = []
        fitted = predictions(power, share)
        for m in scored:
            effect, variance = estimates[m]
            mean, var = bases[m]
            shift, reduction = fitted[m]
            row.append(_log_normal(effect, mean + shift, max(var - reduction, JITTER) + variance) - baseline_lpd[m])
        return [math.fsum(row[i] for i in members) / len(members) for members in groups.values()]

    best: tuple[float, int, float, list[float]] | None = None
    for power in powers:
        for share in SHARES:
            row = gains(power, share)
            total = math.fsum(row)
            if best is None or total > best[0] + 1e-12:
                best = (total, power, share, row)
    assert best is not None
    _total, power, share, row = best
    n = len(row)
    mean = math.fsum(row) / n
    sd = math.sqrt(math.fsum((d - mean) ** 2 for d in row) / (n - 1))
    # Constant gains do not estimate uncertainty; numerical roundoff must not
    # turn that failure into arbitrarily strong evidence.
    z = mean / (sd / math.sqrt(n)) if sd > 1e-12 * max(1.0, abs(mean)) else 0.0
    out.power, out.share, out.lpd_gain, out.z = power, share, mean, z
    out.verdict = VALIDATED if mean > 0 and z >= threshold else NOT_VALIDATED
    if not out.validated:
        out.share = 0.0
    else:
        out._predictions = (
            predictions(power, share)
            if validation_cases is not None
            else _transfer_engine(hoods, scale, residual, noise)(power, share)
        )
        out._prediction_key = (power, share, tau2)
    return out


# -- look-alikes with opposite effects -----------------------------------------------------------


@dataclass(frozen=True)
class Lookalike:
    a: str
    b: str
    similarity: float
    effect_a: float
    effect_b: float
    z: float
    q_value: float


def lookalikes(
    estimates: Mapping[str, tuple[float, float]],
    kernel: Kernel,
    *,
    min_similarity: float = LOOKALIKE_SIMILARITY,
    alpha: float = ALPHA,
    practical: float = 0.10,
) -> list[Lookalike]:
    """Similar memory pairs whose randomized effects differ in sign, at false-discovery rate `alpha`."""
    from unimem.causal_gate import _benjamini_hochberg

    ids = sorted(m for m in estimates if kernel.has(m))
    tested: list[tuple[tuple[str, str], float, float, float]] = []
    for a, b, s in kernel.similar_pairs(ids, min_similarity=min_similarity):
        (ea, va), (eb, vb) = estimates[a], estimates[b]
        z = (ea - eb) / math.sqrt(va + vb) if va + vb > 0 else 0.0
        tested.append(((a, b), s, z, math.erfc(abs(z) / math.sqrt(2.0))))
    q_values = _benjamini_hochberg([p for *_rest, p in tested])
    out = []
    for ((a, b), s, z, _p), q in zip(tested, q_values):
        ea, eb = estimates[a][0], estimates[b][0]
        if q < alpha and ea * eb < 0 and abs(ea - eb) >= practical:
            out.append(Lookalike(a, b, round(s, 6), ea, eb, z, q))
    return out


# -- the store -----------------------------------------------------------------------------------

_TEXT_FIELDS = ("name", "description", "applies_when", "do_not_apply_when")


def lesson_texts(root: str) -> tuple[dict[str, str], set[str]]:
    """Every readable lesson's text by slug, and the slugs whose status is active."""
    import glob
    import os

    from unimem import frontmatter, lesson_io, paths

    texts: dict[str, str] = {}
    active: set[str] = set()
    for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
        name = os.path.basename(path)
        if name == "lesson_template.md":
            continue
        try:
            fm, body = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - an unreadable lesson has no text to transfer
            continue
        fm = fm if isinstance(fm, Mapping) else {}
        slug = lesson_io.canonical_slug(name)
        parts = [str(fm[key]) for key in _TEXT_FIELDS if isinstance(fm.get(key), str)]
        texts[slug] = "\n".join([*parts, body or ""])
        if str(fm.get("status") or "active") == "active":
            active.add(slug)
    return texts, active
