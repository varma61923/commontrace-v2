"""The parsed lesson corpus, cached across queries and rebuilt incrementally.

WHY THIS EXISTS. Every retrieval re-read and re-parsed the entire lesson
store: `query_cmd._iter_active_lessons` globs `lesson_*.md` and runs
`frontmatter.read` (open + regex + `yaml.load`) on every file, on every query,
and `mcp_server` -- a long-lived process -- did it per `retrieve` call too.
Measured with `commontrace/reference/measure_local_latency.py --no-cache`:

    lessons     load      rank      total     load share
        100    116.0 ms    2.0 ms   118.0 ms      98.3%
      1,600   1815.4 ms   33.3 ms  1849.7 ms      98.1%
      6,400   7912.4 ms  193.5 ms  8115.0 ms      97.5%

fitted alpha 1.02 -- LINEAR-OR-WORSE in `hub/SCALING.md`'s own verdict
language, on the tier `protocol/PROTOCOL.md` §5 says every agent without a Hub
runs exclusively. `STRATEGY.md` §13.2's link 3 asks whether serving cost grows
with corpus size; for the local tier the answer was "linearly, and parsing is
98% of it". A lesson store only grows, so retrieval got slower exactly as the
fleet learned more.

With this cache (and the paired tokenization cache below), same benchmark,
same machine, real code path (`--sizes ... `, cache warm from a prior query,
which is the steady state a real fleet runs in):

    lessons     load      rank      total     load share    speedup
        100      1.2 ms    1.1 ms     2.3 ms      53.3%        51x
      1,600     22.7 ms   22.7 ms    48.6 ms      46.7%        38x
      6,400    129.0 ms  134.2 ms   249.1 ms      51.8%        33x

`alpha` for the cached path is still ~1.1 -- and that is expected, not a
regression: detecting staleness needs one `os.stat` per lesson (~15 ms at
10k), and ranking needs to at least look at every candidate once. Both are
inherently O(N). What moved is the CONSTANT per lesson -- a `yaml.load` call
replaced by a stat comparison for every file that has not changed -- not the
exponent. Note the shape of that: making the ranker faster alone would only
ever have touched the 2% `load_share` left standing in the original table.

WHAT IS CACHED, AND WHY ONLY THAT. A projection of each lesson's frontmatter
containing exactly the fields the retrieval path reads -- not the whole
frontmatter. Two reasons, both load-bearing:

  - Real lesson files carry `last_hit: 2026-07-01`, which PyYAML materializes
    as a `datetime.date`. A cache that round-tripped arbitrary frontmatter
    through JSON would either crash on it or silently change its type. A
    bounded projection of known-safe fields cannot.
  - A cache that answers questions the retrieval path never asks is a cache
    whose staleness nobody can reason about. Callers needing full frontmatter
    (lesson editing, `doctor`, distillation) still parse the file.

STALENESS IS DETECTED, NEVER ASSUMED -- WITHIN STAT GRANULARITY. Each entry
records the file's `(mtime_ns, size)`. Every load stats every lesson file --
~15 ms at 10k, against the ~36 s of YAML it replaces -- and reparses exactly
the files whose stat changed, plus any that are new. Deleted files leave. So
an edit costs one parse, not N, and the answer reflects the store as of the
last stat-visible change. A rewrite that preserves both mtime_ns and size
(an equal-length content swap within one timestamp tick, an
mtime-preserving copy, a skewed or restored clock) is invisible to this
check by construction; when that is suspected, call `invalidate(root)` (or
delete `.cache/lessons.json`) to force a full reparse. This matches
`commontrace/reference/query.py`'s `check_staleness` granularity for the
semantic index: `mcp_server`'s "cannot go stale" holds for every change the
filesystem reports, not for one it hides.

IT NEVER RAISES. A corrupt, unreadable or unwritable cache falls back to
parsing everything, which is exactly today's behaviour -- the same posture
`retrieval_io.load_config` takes, and for the same reason: refusing to serve a
lesson because a cache file is malformed trades a working fleet for a tidy
error. Nothing here is a source of truth; the lesson files are.
"""

from __future__ import annotations

import glob
import json
import os
import tempfile

from commontrace import frontmatter, paths

CACHE_NAME = "lessons.json"
CACHE_DIR = ".cache"

# Bumped when the projection below changes shape, so an older cache is
# discarded rather than misread. A cache whose format silently drifted would
# be worse than none: it would answer confidently and wrongly.
FORMAT_VERSION = 1

# Exactly the frontmatter keys the retrieval path reads:
#   - `_lesson_text_weighted`: description, applies_when, tags, domain
#   - `RankedLesson`:          name, description
#   - the sort key:            importance, uses
#   - `_iter_active_lessons`:  status, agent_type
# Adding a key here requires bumping FORMAT_VERSION.
PROJECTED_FIELDS = (
    "name", "description", "applies_when", "tags",
    "domain", "importance", "uses", "status", "agent_type",
)


def cache_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CACHE_DIR, CACHE_NAME)


def _json_safe(value):
    """Keep a value if JSON round-trips it unchanged, else stringify it.

    Stringifying is safe for every field in PROJECTED_FIELDS: the text fields
    are read through `str(... or "")` anyway, and `retrieval._rank_int`
    already coerces a non-integer `importance`/`uses` to 0 whether it arrives
    as a date, None, or the string "high". So a stringified value ranks
    identically to the value it replaced.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def field_terms(fm: dict) -> list[list[str]]:
    """The tokenized weighted fields, in `_lesson_text_weighted`'s own order.

    Cached because tokenization is the other half of the per-query cost the
    parse cache does not remove: profiling `rank_lessons` over 6,400 lessons
    put `_tokenize` plus its regex `findall` at 44% of ranking time, re-derived
    identically on every query from files that had not changed.

    Stored as sorted lists purely so the JSON is stable on disk and diffable;
    `rank_lessons` rebuilds a `set` from each, and a set's contents -- not the
    order they arrived in -- is all the scoring reads.
    """
    from commontrace.retrieval import _lesson_text_weighted, _tokenize

    return [sorted(set(_tokenize(text))) for text, _weight in _lesson_text_weighted(fm)]


def project(fm: dict) -> dict:
    """The subset of a lesson's frontmatter that retrieval actually reads.

    Absent keys stay absent rather than becoming None: `fm.get("importance", 0)`
    and `fm.get("description") or ""` treat the two differently, so preserving
    the distinction is what keeps a cached lesson rank identically to a freshly
    parsed one.
    """
    return {k: _json_safe(fm[k]) for k in PROJECTED_FIELDS if k in fm}


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _lesson_paths(root: str) -> list[str]:
    """Same enumeration `_iter_active_lessons` performs, including its sort --
    `rank_lessons` sorts stably, so ties between equally-scoring lessons are
    broken by corpus order and that order has to be reproduced exactly."""
    ldir = paths.lessons_dir(root)
    return [
        p for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md")))
        if os.path.basename(p) != "lesson_template.md"
    ]


def _read_cache(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("format_version") != FORMAT_VERSION:
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def _write_cache(path: str, entries: dict) -> None:
    """Atomic, and best-effort: a store on read-only media still retrieves.

    Unique tmp name per call, then `os.replace` -- the same pattern
    `commontrace/reference/build_index.py:_write_index` uses, and for the same
    reason: two processes querying the same store concurrently must not be able
    to interleave partial writes over each other.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=CACHE_NAME + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"format_version": FORMAT_VERSION, "entries": entries}, fh)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError, TypeError):
        pass  # a cache that cannot be written is not an error, only slower


def _valid_terms(terms: object) -> bool:
    """Cached `terms` must be list-of-4 token lists, else reparse.

    A corrupt cache previously passed through (missing key -> KeyError in
    load_active_with_terms, or a 4-char string passing the len==4 ranker
    check and misranking silently), violating this module's IT NEVER RAISES
    contract. Validate shape here so any corruption falls back to parsing.
    """
    if not isinstance(terms, list) or len(terms) != 4:
        return False
    return all(isinstance(field, list) and all(isinstance(t, str) for t in field)
               for field in terms)


def _stamps_differ(cached: dict, entries: dict) -> bool:
    """Whether the on-disk cache differs, without comparing token lists.

    `terms` are a pure function of the projected `fm` (field_terms), so
    equal stamps + equal projections imply equal terms. The previous deep
    `entries != cached` re-compared every token list on every query -- the
    steady-state hot path -- for information the stamps already carry.
    """
    if set(cached) != set(entries):
        return True
    for path, entry in entries.items():
        prior = cached.get(path)
        if not isinstance(prior, dict):
            return True
        if (prior.get("mtime_ns"), prior.get("size")) != (entry.get("mtime_ns"), entry.get("size")):
            return True
        if prior.get("fm") != entry.get("fm"):
            return True
    return False


def _load_entries(root: str, reader=None) -> tuple[dict[str, dict], list[str]]:
    """(entries, ordered_paths): path -> {"mtime_ns", "size", "fm", "terms"}.

    The single read/reparse/rewrite pass every public function in this module
    is a thin view over -- there is exactly one place that stats a file,
    decides whether to reparse it, and rewrites the on-disk cache, so
    `load_projected`, `load_active` and `load_active_with_terms` can never
    disagree about what is fresh.

    Returns the corpus order snapshot alongside the entries so callers never
    re-glob: a second enumeration between the load and the emission could
    observe a concurrent create/delete and reorder ties out from under
    `rank_lessons`'s stable sort (or drop/duplicate a ranked lesson).

    `reader(path) -> (fm, body) | None` lets the caller decide how a malformed
    file is reported (`commands/_format.read_or_warn` warns and returns None).
    Keeping that policy out of here is deliberate: a cache should not own the
    question of what a user is told about their own broken file.

    A file that fails to parse is deliberately NOT cached as a negative entry.
    It costs a parse attempt on every query, which is exactly what it costs
    today -- and that is the point: the warning keeps firing until the file is
    fixed, rather than being silenced by a cache the user cannot see.
    """
    if reader is None:
        def reader(p):  # noqa: E306
            try:
                return frontmatter.read(p)
            except Exception:
                return None

    try:
        lesson_paths = _lesson_paths(root)
    except OSError:
        return {}, []

    cpath = cache_path(root)
    cached = _read_cache(cpath)

    entries: dict = {}
    repaired = False
    for path in lesson_paths:
        stamp = _stat(path)
        if stamp is None:
            continue  # vanished between glob and stat
        prior = cached.get(path)
        stamp_and_fm_match = (
            isinstance(prior, dict)
            and prior.get("mtime_ns") == stamp[0]
            and prior.get("size") == stamp[1]
            and isinstance(prior.get("fm"), dict)
        )
        if stamp_and_fm_match and _valid_terms(prior.get("terms")):
            entries[path] = prior
            continue
        if stamp_and_fm_match:
            # Stamps and projection agree with what's cached, but `terms`
            # itself is corrupt -- recompute it from the still-valid `fm`
            # rather than paying a full reparse, and mark the cache dirty so
            # the fix is actually written back. `_stamps_differ` alone would
            # never see this case: stamps and `fm` are exactly what it
            # compares, and neither one moved.
            entries[path] = {**prior, "terms": field_terms(prior["fm"])}
            repaired = True
            continue

        # New or modified: the only path that pays a YAML parse.
        result = reader(path)
        if result is None:
            continue
        fm = result[0]
        if not isinstance(fm, dict):
            continue
        projected = project(fm)
        entries[path] = {
            "mtime_ns": stamp[0], "size": stamp[1],
            "fm": projected, "terms": field_terms(projected),
        }

    # Write only when the cache would actually change, so a steady-state query
    # is pure reads -- no tmpfile, no rename, no write amplification per query.
    if repaired or _stamps_differ(cached, entries):
        _write_cache(cpath, entries)
    return entries, [p for p in lesson_paths if p in entries]


def load_projected(root: str, reader=None) -> list[tuple[str, dict]]:
    """Every lesson in the store as (path, projected frontmatter).

    Unfiltered -- callers apply their own `status`/`agent_type` predicates, so
    one cache serves every caller rather than one cache per filter combination.
    """
    entries, ordered = _load_entries(root, reader=reader)
    # The snapshot order from `_load_entries`, not a second glob: re-enumerating
    # here could observe a concurrent create/delete and reorder ties out from
    # under `rank_lessons`'s stable sort (or drop/duplicate a ranked lesson).
    return [(p, entries[p]["fm"]) for p in ordered if p in entries]


def load_active(root: str, agent_type: str | None = None,
                reader=None) -> list[tuple[str, dict]]:
    """Active lessons, filtered exactly as `_iter_active_lessons` filters them."""
    out = []
    for path, fm in load_projected(root, reader=reader):
        if fm.get("status") != "active":
            continue
        if agent_type and fm.get("agent_type") != agent_type:
            continue
        out.append((path, fm))
    return out


def load_active_with_terms(
    root: str, agent_type: str | None = None, reader=None,
) -> tuple[list[tuple[str, dict]], dict[str, list[list[str]]]]:
    """Active lessons, plus their already-tokenized fields keyed by path --
    pass the second value straight through as `rank_lessons(..., term_cache=)`
    to skip re-tokenizing text that has not changed since the last query."""
    entries, ordered = _load_entries(root, reader=reader)
    lessons: list[tuple[str, dict]] = []
    term_cache: dict[str, list[list[str]]] = {}
    for path in ordered:
        entry = entries.get(path)
        if entry is None:
            continue
        fm = entry["fm"]
        if fm.get("status") != "active":
            continue
        if agent_type and fm.get("agent_type") != agent_type:
            continue
        lessons.append((path, fm))
        term_cache[path] = entry["terms"]
    return lessons, term_cache


def invalidate(root: str) -> None:
    """Drop the cache. Not needed for correctness -- staleness is detected by
    stat, not by callers remembering to call this -- but `doctor` and the test
    suite want a way to force the slow path deterministically."""
    try:
        os.unlink(cache_path(root))
    except OSError:
        pass
