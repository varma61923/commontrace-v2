"""Generation-bound immutable sparse fact retrieval with fresh proof validation.

Fact-file identity, not TTL, selects every snapshot. Retention is bounded to
64 MiB/eight generations; scoped BM25 metadata statistics retain at most 8 MiB.
Oversized filter strings are evaluated without retaining their statistics keys.
Posting/term sets use sorted immutable tuples and singleton values rather than
per-document hash tables. Payloads are individually compressed with a fixed
public-schema dictionary; snapshot-local value pools are discarded after build.
Cold builders necessarily read the source corpus once. Proof eligibility is
never cached: only ranked candidates and their dependency ancestry are checked.
Retention budgets exclude active builders/request views. Cold indexing uses
O(N) time and memory; banks exceeding the budget are served without retention.
Decoded request payloads can exceed their retained compressed size; the budget
does not impose a new size or eligibility limit on legacy fact rows.
Coherent-read attempts are capped at three, rather than looping under mutation.
BM25 statistics include metadata-eligible facts whose proofs may be ineligible;
private facts outside the requested scope never affect those statistics.
Detected changes invalidate the prior retained generation immediately. Active
request views can retain old objects until released; this is not physical erasure.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import sys
import threading
import zlib
from bisect import bisect_left
from collections import Counter, OrderedDict
from collections.abc import Hashable, Iterable, Iterator, Mapping, Set
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal, TypeVar, cast

from commontrace import _jsonl, lesson_cache, retrieval
from commontrace._lexical import STOPWORDS, WORD_RE
from commontrace._stem import stem
from commontrace.runtime_cache import RuntimeCache

if TYPE_CHECKING:
    from commontrace.hierarchical import AtomicFact

Scorer = Literal['overlap-v1', 'bm25-v1']
_OVERLAP = re.compile(r'[a-z0-9]+')
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_RETRIES = 3
K = TypeVar('K')
V = TypeVar('V')
S = TypeVar('S')
# Fixed public schema vocabulary only: no corpus or tenant text enters a
# process-wide compression dictionary or intern table.
_PAYLOAD_DICTIONARY = (
    b'{"id": "", "statement": "", "category": "general", "scopes": [], "confidence": 0.8, '
    b'"confirmations": 1, "valid_from": "", "valid_until": null, '
    b'"expires_at": null, "forgotten": false, "source_traces": [], "status": "active", '
    b'"superseded_by": null, "revision": "", "created_at": "", '
    b'"updated_at": "", "stability": "stable", "evidence": [], '
    b'"evidence_bound": false, "min_support": 1, "evidence_revision": ""}'
)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class _FrozenMap(Mapping[K, V], Generic[K, V]):
    """Privately owned immutable dict with its actual table allocation known."""

    _data: Mapping[K, V]
    _table_bytes: int

    def __init__(self, values: Mapping[K, V]) -> None:
        table = dict(values)
        object.__setattr__(self, '_data', MappingProxyType(table))
        object.__setattr__(self, '_table_bytes', sys.getsizeof(table))

    def __getitem__(self, key: K) -> V:
        return self._data[key]

    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


@dataclass(frozen=True, slots=True, eq=False)
class _CompactSet(Set[str]):
    """Sorted immutable strings without a separate per-set hash table."""

    values: tuple[str, ...] | str

    def __iter__(self) -> Iterator[str]:
        return iter((self.values,)) if isinstance(self.values, str) else iter(self.values)

    def __len__(self) -> int:
        return 1 if isinstance(self.values, str) else len(self.values)

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        if isinstance(self.values, str):
            return value == self.values
        position = bisect_left(self.values, value)
        return position < len(self.values) and self.values[position] == value

    @classmethod
    def _from_iterable(cls, values: Iterable[S]) -> frozenset[S]:
        return frozenset(values)


def _compact_set(values: Iterable[str]) -> _CompactSet:
    """Freeze a unique term/ID iterable; callers supply sets or unique tuples."""
    ordered = tuple(sorted(values))
    return _CompactSet(ordered[0] if len(ordered) == 1 else ordered)


@dataclass(frozen=True, slots=True)
class _Frequencies:
    """Sorted terms and lossless arbitrary-size unsigned varint frequencies."""

    terms: tuple[str, ...]
    counts: bytes

    def __iter__(self) -> Iterator[tuple[str, int]]:
        position = 0
        for term in self.terms:
            count, shift = 0, 0
            while True:
                octet = self.counts[position]
                position += 1
                count |= (octet & 127) << shift
                if octet < 128:
                    break
                shift += 7
            yield term, count

    def get(self, term: str) -> int:
        """One term's count, 0 when absent. Without a multi-byte count (the common
        case) the count is read in place instead of decoding every earlier term."""
        position = bisect_left(self.terms, term)
        if position == len(self.terms) or self.terms[position] != term:
            return 0
        if len(self.counts) == len(self.terms):
            return self.counts[position]
        return next(count for name, count in self if name == term)


def _frequencies(counts: Counter[str]) -> _Frequencies:
    terms, packed = tuple(sorted(counts)), bytearray()
    for term in terms:
        count = counts[term]
        while count >= 128:
            packed.append((count & 127) | 128)
            count >>= 7
        packed.append(count)
    return _Frequencies(terms, bytes(packed))


class FactSnapshotChanged(RuntimeError):
    """The authoritative file changed during loading, ranking or quotation."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    link: tuple[int, int, int, int, int]
    real_path: str


def file_identity(path: str) -> FileIdentity | None:
    try:
        leaf, target = os.lstat(path), os.stat(path)
    except FileNotFoundError:
        return None
    return FileIdentity(target.st_dev, target.st_ino, target.st_size, target.st_mtime_ns, target.st_ctime_ns,
                        (leaf.st_dev, leaf.st_ino, leaf.st_size, leaf.st_mtime_ns, leaf.st_ctime_ns),
                        os.path.realpath(path))


def _scorer(value: str) -> Scorer:
    if not isinstance(value, str) or value not in ('overlap-v1', 'bm25-v1'):
        raise ValueError(f'unknown fact scorer {value!r}')
    return cast(Scorer, value)


def _terms(text: str, scorer: Scorer) -> frozenset[str]:
    if scorer == 'overlap-v1':
        return frozenset(_OVERLAP.findall(text.lower()))
    return frozenset(_bm25_tokens(text))


def _bm25_tokens(text: str) -> list[str]:
    terms = retrieval._tokenize(text)
    # Preserve scalar values and short identifiers: dropping "7" makes quorum
    # 7 and quorum 9 indistinguishable, while one-character CJK words also count.
    terms.extend(term for term in WORD_RE.findall(text.lower()) if len(term) == 1 and term not in STOPWORDS)
    return [stem(term) for term in terms]


def matched_terms(query: str, statement: str, scorer: str = 'overlap-v1') -> list[str]:
    selected = _scorer(scorer)
    return sorted(_terms(query, selected) & _terms(statement, selected))


@dataclass(frozen=True, slots=True)
class _Record:
    id: str
    compressed_payload: bytes
    payload_size: int
    payload_digest: bytes
    category: str
    scopes: tuple[str, ...]
    confidence: float
    forgotten: bool
    status: str
    stability: str
    valid_from: datetime
    valid_until: datetime | None
    expires_at: datetime | None
    bound: bool
    overlap: _CompactSet
    bm25: _Frequencies
    length: int
    source_digest: bytes
    stable_time: bool

    @property
    def payload(self) -> str:
        try:
            decoder = zlib.decompressobj(zdict=_PAYLOAD_DICTIONARY)
            raw = decoder.decompress(self.compressed_payload, self.payload_size + 1)
        except zlib.error as error:
            raise ValueError('stored fact payload is corrupt') from error
        if len(raw) != self.payload_size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('stored fact payload is corrupt')
        if hashlib.sha256(raw).digest() != self.payload_digest:
            raise ValueError('stored fact payload checksum mismatch')
        return raw.decode('utf-8')

    def copy(self) -> AtomicFact:
        from commontrace.hierarchical import _coerce_fact
        return _coerce_fact(json.loads(self.payload))


@dataclass(frozen=True, slots=True)
class _Snapshot:
    path: str
    generation: FileIdentity | None
    records: Mapping[str, _Record]
    overlap: Mapping[str, _CompactSet]
    bm25: Mapping[str, _CompactSet]
    confidence_order: tuple[str, ...]
    boundaries: tuple[datetime, ...]
    stable_time: bool

    def ensure_current(self) -> None:
        current = file_identity(self.path)
        if current != self.generation:
            _remember(self.path, current)
            raise FactSnapshotChanged('fact source changed; retry the governed read')


def _snapshot_bytes(_key: Hashable, snapshot: _Snapshot) -> int:
    if not snapshot.stable_time:
        # Legacy rows deriving valid_from from the wall clock must be coerced
        # afresh on each request, preserving migration-by-read semantics.
        return MAX_SNAPSHOT_BYTES + 1
    return _retained_bytes((_key, snapshot, _PAYLOAD_DICTIONARY))


def _retained_bytes(value: object) -> int:
    """Account immutable Python objects, including Unicode string allocation."""
    size, seen, pending = 1024, set(), [value]
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        size += sys.getsizeof(item)
        if isinstance(item, _FrozenMap):
            size += item._table_bytes + sys.getsizeof(item._data) + sys.getsizeof(item._table_bytes)
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, Mapping):
            # A mapping proxy hides the dict allocation; include a conservative
            # table-capacity allowance as well as the actual keys/values.
            if not isinstance(item, dict):
                size += 128 * len(item)
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, frozenset)):
            pending.extend(item)
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
    return size


_CACHE: RuntimeCache[_Snapshot] = RuntimeCache(max_entries=8, max_bytes=MAX_SNAPSHOT_BYTES, ttl=300,
                                             weigh=_snapshot_bytes)
_GENERATION_LOCK = threading.Lock()
_CURRENT: OrderedDict[str, FileIdentity | None] = OrderedDict()
_STAT_KEYS: OrderedDict[Hashable, str] = OrderedDict()


def _remember(path: str, generation: FileIdentity | None) -> None:
    with _GENERATION_LOCK:
        if path in _CURRENT and _CURRENT[path] != generation:
            _CACHE.invalidate((path, _CURRENT[path]))
            for key in [key for key, owner in _STAT_KEYS.items() if owner == path]:
                _STATISTICS.invalidate(key)
                del _STAT_KEYS[key]
        _CURRENT[path] = generation
        _CURRENT.move_to_end(path)
        while len(_CURRENT) > _CACHE.max_entries:
            old_path, old_generation = _CURRENT.popitem(last=False)
            _CACHE.invalidate((old_path, old_generation))
            for key in [key for key, owner in _STAT_KEYS.items() if owner == old_path]:
                _STATISTICS.invalidate(key)
                del _STAT_KEYS[key]


def _after_fork() -> None:
    global _GENERATION_LOCK, _CURRENT, _STAT_KEYS
    _GENERATION_LOCK, _CURRENT, _STAT_KEYS = threading.Lock(), OrderedDict(), OrderedDict()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def _build(path: str, identity: FileIdentity | None) -> _Snapshot:
    if file_identity(path) != identity:
        raise FactSnapshotChanged('fact source changed before snapshot read')
    records: dict[str, _Record] = {}
    stable_time = True
    for row in _jsonl.read_rows(path):
        try:
            record = _record(row, _line_digest(json.dumps(row, ensure_ascii=False)))
        except (TypeError, ValueError):
            continue
        records[record.id] = record
        stable_time = stable_time and record.stable_time
    records = _pool_records(records)
    overlap: dict[str, set[str]] = {}
    bm25: dict[str, set[str]] = {}
    boundaries: set[datetime] = set()
    for record in records.values():
        for term in record.overlap:
            overlap.setdefault(term, set()).add(record.id)
        for term in record.bm25.terms:
            bm25.setdefault(term, set()).add(record.id)
        boundaries.update(time for time in (record.valid_from, record.valid_until, record.expires_at)
                          if time is not None)
    snapshot = _Snapshot(path, identity, _FrozenMap(records),
                         _FrozenMap({term: _compact_set(ids) for term, ids in overlap.items()}),
                         _FrozenMap({term: _compact_set(ids) for term, ids in bm25.items()}),
                         tuple(sorted(records, key=lambda key: (-records[key].confidence, key))),
                         tuple(sorted(boundaries)), stable_time)
    snapshot.ensure_current()
    return snapshot


def _line_digest(line: str) -> bytes:
    return hashlib.sha256(line.encode('utf-8')).digest()


def _record(row: Mapping[str, object], digest: bytes, previous: _Record | None = None) -> _Record:
    from commontrace.hierarchical import _coerce_fact

    fact = _coerce_fact(dict(row))
    try:
        lesson_cache.parse_moment(row.get('valid_from') or row.get('created_at') or '')  # type: ignore[arg-type]
        stable_time = True
    except (ValueError, TypeError, AttributeError):
        stable_time = False
    payload = json.dumps(fact.to_dict(), ensure_ascii=False)
    # Metadata-only writes retain lexical materialization as well as the cold
    # parser's normalized statement semantics.
    same_statement = previous is not None and json.loads(previous.payload)['statement'] == fact.statement
    if same_statement:
        assert previous is not None
        overlap, bm25, length = previous.overlap, previous.bm25, previous.length
    else:
        counts = Counter(_bm25_tokens(fact.statement))
        overlap = _compact_set(_terms(fact.statement, 'overlap-v1'))
        bm25, length = _frequencies(counts), sum(counts.values())
    raw = payload.encode('utf-8')
    encoder = zlib.compressobj(level=1, zdict=_PAYLOAD_DICTIONARY)
    packed = encoder.compress(raw) + encoder.flush()
    return _Record(fact.id, packed, len(raw), hashlib.sha256(raw).digest(),
                   fact.category, tuple(fact.scopes), fact.confidence, fact.forgotten,
                   fact.status, fact.stability, lesson_cache.parse_moment(fact.valid_from),
                   lesson_cache.parse_moment(fact.valid_until) if fact.valid_until else None,
                   lesson_cache.parse_moment(fact.expires_at) if fact.expires_at else None, fact.evidence_bound,
                   overlap, bm25, length, digest, stable_time)


def _pool_records(records: dict[str, _Record], previous: Mapping[str, _Record] | None = None) -> dict[str, _Record]:
    """Share equal immutable values within this snapshot; discard the pools.

    No process-wide intern table retains sensitive text after invalidation.
    Already pooled COW records are preserved when all identities still match.
    """
    strings: dict[str, str] = {}
    scopes: dict[tuple[str, ...], tuple[str, ...]] = {}
    moments: dict[datetime, datetime] = {}
    numbers: dict[float, float] = {}
    buffers: dict[bytes, bytes] = {}
    def text(value: str) -> str:
        return strings.setdefault(value, value)
    def moment(value: datetime | None) -> datetime | None:
        return moments.setdefault(value, value) if value is not None else None
    if previous is not None:
        for record in previous.values():
            for value in (record.category, record.status, record.stability, *record.scopes,
                          *record.overlap, *record.bm25.terms):
                text(value)
            scopes.setdefault(record.scopes, record.scopes)
            numbers.setdefault(record.confidence, record.confidence)
            moment(record.valid_from)
            moment(record.valid_until)
            moment(record.expires_at)
            buffers.setdefault(record.bm25.counts, record.bm25.counts)
    for key, record in records.items():
        terms = tuple(text(term) for term in record.overlap)
        bm25_terms = tuple(text(term) for term in record.bm25.terms)
        row_scopes = tuple(text(scope) for scope in record.scopes)
        category, status, stability = text(record.category), text(record.status), text(record.stability)
        row_scopes = record.scopes if all(left is right for left, right in zip(row_scopes, record.scopes)) \
            else row_scopes
        pooled_scopes = scopes.setdefault(row_scopes, row_scopes)
        confidence = numbers.setdefault(record.confidence, record.confidence)
        first, until, expiry = moment(record.valid_from), moment(record.valid_until), moment(record.expires_at)
        assert first is not None
        packed = buffers.setdefault(record.bm25.counts, record.bm25.counts)
        overlap = record.overlap if all(left is right for left, right in zip(terms, record.overlap)) \
            else _compact_set(terms)
        frequencies = record.bm25 if packed is record.bm25.counts and all(
            left is right for left, right in zip(bm25_terms, record.bm25.terms)) else _Frequencies(bm25_terms, packed)
        if category is record.category and status is record.status and stability is record.stability \
                and pooled_scopes is record.scopes and confidence is record.confidence \
                and first is record.valid_from and until is record.valid_until and expiry is record.expires_at \
                and overlap is record.overlap and frequencies is record.bm25:
            continue
        records[key] = replace(record, category=category, status=status, stability=stability,
                               scopes=pooled_scopes, confidence=confidence, valid_from=first,
                               valid_until=until, expires_at=expiry, overlap=overlap, bm25=frequencies)
    return records


def capture_for_write(root: str) -> _Snapshot | None:
    """Capture only an already retained exact generation; never cold-build.

    The canonical writer holds its file lock across capture and replacement.
    Complete committed rows, not a prior mutable read, prove the candidate.
    Returned immutable state is an optimization, not authority.
    """
    from commontrace.hierarchical import _facts_file
    path = os.path.abspath(_facts_file(root))
    identity = file_identity(path)
    _remember(path, identity)
    snapshot = _CACHE.peek((path, identity))
    if snapshot is None or not snapshot.stable_time:
        return None
    try:
        snapshot.ensure_current()
    except FactSnapshotChanged:
        return None
    return snapshot


def _committed_identity(path: str, rows: tuple[str, ...], digest: str) -> FileIdentity | None:
    expected = hashlib.sha256()
    for row in rows:
        expected.update((row + '\n').encode('utf-8'))
    if expected.hexdigest() != digest:
        return None
    identity = file_identity(path)
    if identity is None:
        return None
    actual = hashlib.sha256()
    try:
        with open(path, 'rb') as source:
            while chunk := source.read(65536):
                actual.update(chunk)
    except FileNotFoundError:
        return None
    return identity if actual.hexdigest() == digest and file_identity(path) == identity else None


def _patch_postings(previous: Mapping[str, _CompactSet], before: Mapping[str, _Record],
                    after: Mapping[str, _Record], *, bm25: bool) -> Mapping[str, _CompactSet]:
    postings = dict(previous)
    removed: dict[str, set[str]] = {}
    added: dict[str, set[str]] = {}
    def terms(record: _Record) -> frozenset[str]:
        return frozenset(record.bm25.terms if bm25 else record.overlap)
    for key in before.keys() | after.keys():
        old, new = before.get(key), after.get(key)
        if old is new or (old is not None and new is not None and
                          (old.bm25 is new.bm25 if bm25 else old.overlap is new.overlap)):
            continue
        old_terms, new_terms = terms(old) if old else frozenset(), terms(new) if new else frozenset()
        for term in old_terms - new_terms:
            removed.setdefault(term, set()).add(key)
        for term in new_terms - old_terms:
            added.setdefault(term, set()).add(key)
    for term in removed.keys() | added.keys():
        ids = (set(previous.get(term, _CompactSet(()))) - removed.get(term, set())) | added.get(term, set())
        if ids:
            postings[term] = _compact_set(ids)
        else:
            postings.pop(term, None)
    return _FrozenMap(postings)


def publish_committed(root: str, base: _Snapshot | None, rows: tuple[str, ...], digest: str) -> bool:
    """Publish copy-on-write reuse only for a digest-verified canonical commit.

    Unchanged rows avoid parsing/tokenization; metadata-only changes reuse
    lexical terms. Complete serialized rows bind deletions and arbitrary edits.
    Source hashing, map copies, ordering and retention weighing remain O(N).
    External writers, cold/expired bases and unstable legacy timestamps fall
    back to the authoritative coherent reader. No mutable caller object escapes.
    """
    from commontrace.hierarchical import _facts_file
    path = os.path.abspath(_facts_file(root))
    _remember(path, file_identity(path))
    if base is None or base.path != path or not base.stable_time:
        return False
    # Each envelope element must be one JSONL record. Literal line breaks can
    # encode two disk rows while json.loads treats the element as malformed.
    if any('\r' in line or '\n' in line for line in rows):
        return False
    identity = _committed_identity(path, rows, digest)
    if identity is None:
        _remember(path, file_identity(path))
        return False
    reuse: dict[bytes, _Record] = {}
    for previous in base.records.values():
        reuse[previous.source_digest] = previous
        reuse[previous.payload_digest] = previous
    records: dict[str, _Record] = {}
    for line in rows:
        checksum = _line_digest(line)
        record = reuse.get(checksum)
        if record is not None:
            if record.source_digest != checksum:
                record = replace(record, source_digest=checksum)
        else:
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    continue
                prior = base.records.get(str(raw.get('id') or ''))
                record = _record(raw, checksum, prior)
            except (TypeError, ValueError):
                continue
        if not record.stable_time:
            return False
        records[record.id] = record
    records = _pool_records(records, base.records)
    snapshot = _Snapshot(path, identity, _FrozenMap(records),
                         _patch_postings(base.overlap, base.records, records, bm25=False),
                         _patch_postings(base.bm25, base.records, records, bm25=True),
                         tuple(sorted(records, key=lambda key: (-records[key].confidence, key))),
                         tuple(sorted({moment for record in records.values() for moment in
                                       (record.valid_from, record.valid_until, record.expires_at)
                                       if moment is not None})), True)
    try:
        snapshot.ensure_current()
        # The loader checks again even if a competing cold reader owns the
        # flight. Generation invalidation fences retained publication.
        def load() -> _Snapshot:
            snapshot.ensure_current()
            return snapshot
        published = _CACHE.get_or_load((path, identity), load)
        published.ensure_current()
        return True
    except FactSnapshotChanged:
        return False


class FactView(Mapping[str, 'AtomicFact']):
    """Request-local lazy copies over an immutable generation-bound snapshot.

    Access verifies current source identity. Call ``ensure_current`` after
    assembling a context as well: a changed generation is never knowingly used.
    Copies obtained from a view may be edited without changing another request.
    """

    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot
        self._copies: dict[str, AtomicFact] = {}

    @property
    def generation(self) -> FileIdentity | None:
        return self._snapshot.generation

    def ensure_current(self) -> None:
        self._snapshot.ensure_current()

    def __getitem__(self, key: str) -> AtomicFact:
        self.ensure_current()
        if key not in self._copies:
            self._copies[key] = self._snapshot.records[key].copy()
        self.ensure_current()
        return self._copies[key]

    def __iter__(self) -> Iterator[str]:
        self.ensure_current()
        return iter(self._snapshot.records)

    def __len__(self) -> int:
        self.ensure_current()
        return len(self._snapshot.records)


def snapshot_facts(root: str) -> FactView:
    from commontrace import fact_store
    from commontrace.hierarchical import _facts_file

    if fact_store.enabled(root):
        # The SQLite view implements the same public Mapping/ensure_current
        # contract without materializing the corpus or retaining connections.
        return cast(FactView, fact_store.read_view(root))

    path = os.path.abspath(_facts_file(root))
    for _attempt in range(MAX_RETRIES):
        identity = file_identity(path)
        _remember(path, identity)
        try:
            snapshot = _CACHE.get_or_load((path, identity), lambda: _build(path, identity))
            snapshot.ensure_current()
            return FactView(snapshot)
        except FactSnapshotChanged:
            continue
    raise FactSnapshotChanged('fact source kept changing during coherent read')


@dataclass(frozen=True, slots=True)
class _Filter:
    scope: str
    category: str
    as_of: str | None
    include_forgotten: bool
    show_expired: bool
    stability: str

    def allows(self, record: _Record, moment: datetime) -> bool:
        return (self.include_forgotten or not record.forgotten) \
            and (not self.stability or self.stability == record.stability) \
            and (bool(self.as_of) or record.status == 'active') \
            and (not self.category or self.category == record.category) \
            and (not self.scope or not record.scopes or self.scope in record.scopes) \
            and record.valid_from <= moment and (record.valid_until is None or record.valid_until > moment) \
            and (self.show_expired or record.expires_at is None or record.expires_at > moment)


@dataclass(frozen=True, slots=True)
class _Statistics:
    document_count: int
    frequencies: Mapping[str, int]
    average_length: float


def _statistics_bytes(_key: Hashable, stats: _Statistics) -> int:
    return _retained_bytes((_key, stats))


_STATISTICS: RuntimeCache[_Statistics] = RuntimeCache(max_entries=64, max_bytes=8 * 1024 * 1024, ttl=300,
                                                    weigh=_statistics_bytes)


def clear_cache() -> None:
    """Release retained snapshots/statistics; existing request views remain owned."""
    with _GENERATION_LOCK:
        _CURRENT.clear()
        _STAT_KEYS.clear()
        _CACHE.clear()
        _STATISTICS.clear()


def cache_info() -> dict[str, dict[str, int]]:
    return {'snapshots': _CACHE.stats(), 'statistics': _STATISTICS.stats()}


def _statistics(snapshot: _Snapshot, filters: _Filter, moment: datetime) -> _Statistics:
    from bisect import bisect_right
    boundary = bisect_right(snapshot.boundaries, moment)
    def build() -> _Statistics:
        snapshot.ensure_current()
        document_count = 0
        frequencies: Counter[str] = Counter()
        total = 0
        for record in snapshot.records.values():
            if filters.allows(record, moment):
                document_count += 1
                total += record.length
                frequencies.update(record.bm25.terms)
        statistics = _Statistics(document_count, _FrozenMap(frequencies),
                                 total / document_count if document_count else 0)
        snapshot.ensure_current()
        return statistics
    key = (snapshot.path, snapshot.generation, filters, boundary)
    if not snapshot.stable_time or any(len(value) > 256 for value in
                                       (filters.scope, filters.category, filters.as_of or '', filters.stability)):
        return build()
    snapshot.ensure_current()
    with _GENERATION_LOCK:
        if snapshot.path in _CURRENT and _CURRENT[snapshot.path] != snapshot.generation:
            raise FactSnapshotChanged('fact source changed before scoped statistics')
        _STAT_KEYS[key] = snapshot.path
        _STAT_KEYS.move_to_end(key)
        while len(_STAT_KEYS) > _STATISTICS.max_entries:
            old_key, _path = _STAT_KEYS.popitem(last=False)
            _STATISTICS.invalidate(old_key)
    return _STATISTICS.get_or_load(key, build)


def search(root: str, query: str, *, scope: str = '', category: str = '', as_of: str | None = None,
           limit: int = 10, include_forgotten: bool = False, show_expired: bool = False,
           stability: str = '', scorer: str = 'overlap-v1') -> list[tuple[AtomicFact, float]]:
    from commontrace import fact_store
    from commontrace.fact_evidence import EvidenceResolver
    from commontrace.hierarchical import STABILITY_TIERS, _now

    if fact_store.enabled(root):
        return fact_store.search(root, query, scope=scope, category=category, as_of=as_of, limit=limit,
                                 include_forgotten=include_forgotten, show_expired=show_expired,
                                 stability=stability, scorer=scorer)
    selected = _scorer(scorer)
    if stability and stability not in STABILITY_TIERS:
        raise ValueError(f'unknown stability tier {stability!r}')
    historical = lesson_cache.parse_moment(as_of) if as_of else None
    limit = max(0, int(limit))
    if not limit:
        return []
    filters = _Filter(scope, category, as_of, include_forgotten, show_expired, stability)
    query_terms = _terms(query, selected)
    if selected == 'bm25-v1' and not query_terms:
        return []
    for _attempt in range(MAX_RETRIES):
        try:
            view = snapshot_facts(root)
            snapshot = view._snapshot
            # Reuse the canonical lifecycle clock, including legacy clock
            # injection; the ISO round trip preserves its full precision.
            moment = historical or lesson_cache.parse_moment(_now())
            postings = snapshot.overlap if selected == 'overlap-v1' else snapshot.bm25
            ranked: Iterable[tuple[str, float]]
            if query_terms:
                ids = set().union(*(postings.get(term, frozenset()) for term in query_terms))
                scores: list[tuple[float, str]] = []
                stats = _statistics(snapshot, filters, moment) if selected == 'bm25-v1' else None
                idf = [(term, retrieval._idf(stats.document_count, stats.frequencies[term]))
                       for term in sorted(query_terms) if term in stats.frequencies] if stats is not None else []
                for key in ids:
                    record = snapshot.records[key]
                    if not filters.allows(record, moment):
                        continue
                    if stats is None:
                        overlap = sum(term in record.overlap for term in query_terms)
                        raw = overlap / (len(query_terms) + len(record.overlap) - overlap)
                    else:
                        # same terms in the same (sorted) order as iterating the record, so
                        # scores are bit-identical; only the lookups and IDFs are cheaper
                        raw = 0.0
                        for term, weight in idf:
                            count = record.bm25.get(term)
                            if count:
                                raw += retrieval._bm25_term(count, weight, record.length, stats.average_length)
                        raw = raw / (1 + raw)
                    scores.append((-round(raw * 0.7 + record.confidence * 0.3, 4), key))
                heapq.heapify(scores)
                def ordered() -> Iterator[tuple[str, float]]:
                    while scores:
                        negative_score, key = heapq.heappop(scores)
                        yield key, -negative_score
                ranked = ordered()
            else:
                ranked = ((key, snapshot.records[key].confidence) for key in snapshot.confidence_order
                          if filters.allows(snapshot.records[key], moment))
            resolver = EvidenceResolver(root, view, as_of=as_of)
            result: list[tuple[AtomicFact, float]] = []
            for key, score in ranked:
                record = snapshot.records[key]
                if record.bound and not resolver.assess(key).eligible:
                    continue
                result.append((view[key], score))
                if len(result) == limit:
                    break
            view.ensure_current()
            return result
        except FactSnapshotChanged:
            continue
    raise FactSnapshotChanged('fact source kept changing during governed ranking')
