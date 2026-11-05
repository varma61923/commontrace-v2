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

Transfer is earned, not assumed. ``power`` and ``share`` are chosen by leave-one-out
log predictive density over the measured memories. The field is used only when the
best configuration beats no transfer by a one-sided paired z test at a level
Bonferroni-corrected over the whole grid. Otherwise it is NOT_VALIDATED and changes
nothing.

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
from collections.abc import Iterable, Mapping, Sequence
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
KERNEL_CONTEXT = "context-components-v1"


def validate_z(powers: int = len(POWERS)) -> float:
    """One-sided z a configuration must reach, Bonferroni-corrected over the search grid."""
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
    scaled by ``1/sqrt(spaces)``, so the similarity of two memories is the mean of their
    per-space cosines over the spaces they share, and the Gram matrix stays positive
    semidefinite.
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

    def neighbors(self, queries: Iterable[str], pool: Iterable[str], k: int = NEIGHBORS) -> dict[str, Neighbors]:
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
        postings: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for memory in members:
            for term, w in self.vectors.get(memory, {}).items():
                postings[term].append((memory, w))
        out: dict[str, Neighbors] = {}
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
                ranked = heapq.nsmallest(
                    k,
                    ((m, s) for m, s in scores[q].items() if s > 1e-9 and m != q),
                    key=lambda item: (-item[1], item[0]),
                )
                out[q] = tuple((m, round(min(1.0, s), 12)) for m, s in ranked)
        return out

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
        if (a, b) not in self._shared:
            pa, pb = a.split("/"), b.split("/")
            self._shared[a, b] = sum(1 for i in range(min(len(pa), len(pb))) if pa[i] == pb[i]) / self.width
        return self._shared[a, b]

    def _memory(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        if self.content is None:
            return 0.0
        if (a, b) not in self._sims:
            self._sims[a, b] = self.content.similarity(a, b)
        return self._sims[a, b]

    def similarity(self, a: str, b: str) -> float:
        (ma, ca), (mb, cb) = self.cells[a], self.cells[b]
        return self._memory(ma, mb) * self._context(ca, cb)

    def _pool(self, pool: Iterable[str], memories: Iterable[str]) -> tuple[dict[str, list[str]], dict[str, Neighbors]]:
        """The pool's cells by memory, and each memory's content neighbours among the pool's memories."""
        members = frozenset(pool)
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

    def neighbors(self, queries: Iterable[str], pool: Iterable[str], k: int = NEIGHBORS) -> dict[str, Neighbors]:
        """Each query cell's `k` most similar pool cells: its own memory's other contexts and its
        content neighbours' contexts."""
        queries = list(queries)
        by_memory, near = self._pool(pool, (self.cells[q][0] for q in queries))
        out: dict[str, Neighbors] = {}
        for q in queries:
            memory, context = self.cells[q]
            scored = []
            for other, weight in ((memory, 1.0), *near.get(memory, ())):
                for cell in by_memory.get(other, ()):
                    if cell != q:
                        s = weight * self._context(context, self.cells[cell][1])
                        if s > 1e-9:
                            scored.append((cell, round(min(1.0, s), 12)))
            out[q] = tuple(sorted(scored, key=lambda item: (-item[1], item[0]))[:k])
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


def _log_normal(x: float, mean: float, variance: float) -> float:
    return -0.5 * (math.log(2.0 * math.pi * variance) + (x - mean) ** 2 / variance)


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
    _hoods: dict[str, _Neighborhood] = field(default_factory=dict, repr=False)
    _residual: dict[str, float] = field(default_factory=dict, repr=False)
    _noise: dict[str, float] = field(default_factory=dict, repr=False)
    _scales: dict[str, float] = field(default_factory=dict, repr=False)

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
        return tuple(zip(hood.ids, hood.to_query))

    def prior(self, memory: str, base_mean: float, base_var: float) -> tuple[float, float]:
        """`memory`'s prior (mean, variance): the base prior moved by its witnesses' evidence."""
        if not self.validated:
            return base_mean, base_var
        shift, reduction = _transfer(
            self._hoods.get(memory, _EMPTY),
            self.power,
            self.share,
            self._scale(memory),
            self._scales,
            self._residual,
            self._noise,
        )
        return base_mean + shift, max(base_var - reduction, JITTER)

    def summary(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "kernel": self.kernel,
            "power": self.power,
            "share": self.share,
            "measured": self.measured,
            "lpd_gain": None if self.lpd_gain is None else round(self.lpd_gain, 6),
            "z": None if self.z is None or not math.isfinite(self.z) else round(self.z, 4),
            "threshold": round(self.threshold, 4),
        }


_EMPTY = _Neighborhood((), (), ())


def _hood(kernel: Kernel | CellKernel, neighbors: Neighbors) -> _Neighborhood:
    ids = tuple(m for m, _s in neighbors)
    among = tuple(tuple(1.0 if a == b else kernel.similarity(a, b) for b in ids) for a in ids)
    return _Neighborhood(ids, tuple(s for _m, s in neighbors), among)


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
) -> Field:
    """Fit and validate the field over measured `estimates` (memory -> (effect, variance)).

    `bases` holds the gate's leave-one-out prior (mean, variance) for every measured
    memory and every candidate; `tau2` is the gate's between-memory variance. The
    validation threshold is Bonferroni-corrected over ``len(powers) * len(SHARES)``.
    `baseline` is the prior used when the field does not validate, if it differs
    from `bases`; the field must beat that prior, not merely its own base. `scales`
    gives an item its own prior sd of deviation (default ``sqrt(tau2)`` for all).
    `targets` restricts which measured items are scored and given priors (default: all);
    every measured item still serves as a witness.
    """
    reference = bases if baseline is None else baseline
    threshold = validate_z(len(powers))
    measured = sorted(m for m in estimates if m in bases and kernel.has(m))
    wanted = set(measured) if targets is None else set(targets) & set(measured)
    queries = sorted(wanted | {c for c in candidates if c in bases and kernel.has(c)})
    hoods = {q: _hood(kernel, nb) for q, nb in kernel.neighbors(queries, measured, k).items()}
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
    )
    scored = [m for m in sorted(wanted) if hoods[m].ids]
    out.measured = len(scored)
    if len(scored) < MIN_MEASURED or tau2 <= 0.0 or min(scale.values(), default=0.0) <= 0.0:
        return out

    def gains(power: int, share: float) -> list[float]:
        row = []
        for m in scored:
            effect, variance = estimates[m]
            mean, var = bases[m]
            shift, reduction = _transfer(hoods[m], power, share, scale[m], scale, residual, noise)
            row.append(
                _log_normal(effect, mean + shift, max(var - reduction, JITTER) + variance)
                - _log_normal(effect, reference[m][0], reference[m][1] + variance)
            )
        return row

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
    z = mean / (sd / math.sqrt(n)) if sd > 0 else (math.inf if mean > 0 else 0.0)
    out.power, out.share, out.lpd_gain, out.z = power, share, mean, z
    out.verdict = VALIDATED if mean > 0 and z >= threshold else NOT_VALIDATED
    if not out.validated:
        out.share = 0.0
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
    pairs: dict[tuple[str, str], float] = {}
    for a, row in kernel.neighbors(ids, ids).items():
        for b, s in row:
            if s >= min_similarity:
                pairs[(min(a, b), max(a, b))] = s
    tested: list[tuple[tuple[str, str], float, float, float]] = []
    for (a, b), s in sorted(pairs.items()):
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
