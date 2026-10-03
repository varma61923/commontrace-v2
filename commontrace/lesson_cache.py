"""The parsed lesson corpus, cached across queries and rebuilt incrementally."""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import glob
import json
import os
import tempfile

from commontrace import frontmatter, paths, ttl

CACHE_NAME = "lessons.json"
CACHE_DIR = ".cache"

FORMAT_VERSION = 4

PROJECTED_FIELDS = (
    "name", "description", "applies_when", "tags",
    "domain", "importance", "uses", "status", "agent_type",
    "core", "scopes", "valid_from", "valid_until", "expires",
)


def cache_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CACHE_DIR, CACHE_NAME)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def field_terms(fm: dict) -> list[list[str]]:
    """The tokenized weighted fields, in `_lesson_text_weighted`'s own order."""
    from commontrace.retrieval import _lesson_text_weighted, _tokenize

    return [sorted(set(_tokenize(text))) for text, _weight in _lesson_text_weighted(fm)]


def project(fm: dict) -> dict:
    """The subset of a lesson's frontmatter that retrieval actually reads."""
    return {k: _json_safe(fm[k]) for k in PROJECTED_FIELDS if k in fm}


def parse_moment(value: str | datetime.date | datetime.datetime) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        parsed = value
    elif isinstance(value, datetime.date):
        parsed = datetime.datetime.combine(value, datetime.time.min)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("a date/time is required")
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"could not parse {value!r} as a date/time; use YYYY-MM-DD or ISO 8601"
            ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def filter_eligible(
    lessons: list[tuple[str, dict]], *, scope: str = "", as_of: str | datetime.datetime | None = None,
    show_expired: bool = False,
) -> list[tuple[str, dict]]:
    moment = parse_moment(as_of) if as_of else datetime.datetime.now(datetime.timezone.utc)
    requested_scope = str(scope or "").strip()
    eligible = []
    for path, fm in lessons:
        raw_scopes = fm.get("scopes")
        scopes = (
            {str(item).strip() for item in raw_scopes if str(item).strip()}
            if isinstance(raw_scopes, (list, tuple, set))
            else {str(raw_scopes).strip()} if raw_scopes else set()
        )
        if requested_scope and scopes and requested_scope not in scopes:
            continue
        try:
            valid_from = parse_moment(fm["valid_from"]) if fm.get("valid_from") else None
            valid_until = parse_moment(fm["valid_until"]) if fm.get("valid_until") else None
        except ValueError:
            continue
        if valid_from is not None and moment < valid_from:
            continue
        if valid_until is not None and moment >= valid_until:
            continue
        if not show_expired and ttl.lesson_is_expired(fm, moment):
            continue
        eligible.append((path, fm))
    return eligible


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


_SCAN_SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("commontrace_scan_scope", default=None)


@contextlib.contextmanager
def one_scan():
    """List each store's lessons directory at most once inside this block."""
    token = _SCAN_SCOPE.set({})
    try:
        yield
    finally:
        _SCAN_SCOPE.reset(token)


def listing(root: str) -> tuple:
    """((path, mtime_ns, size), ...) for every lesson file, sorted by path (see `_listing`)."""
    return _listing(root)


def mtime_seconds(mtime_ns: int) -> float:
    sec, nsec = divmod(mtime_ns, 1_000_000_000)
    return sec + nsec * 1e-9


def _listing(root: str) -> tuple:
    ldir = paths.lessons_dir(root)
    scope = _SCAN_SCOPE.get()
    if scope is not None and ldir in scope:
        return scope[ldir]
    prefix = ldir if ldir.endswith(os.sep) else ldir + os.sep
    out = []
    with os.scandir(ldir) as entries:
        for entry in entries:
            name = entry.name
            if not (name.startswith("lesson_") and name.endswith(".md")) or name == "lesson_template.md":
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            out.append((prefix + name, st.st_mtime_ns, st.st_size))
    out.sort()
    result = tuple(out)
    if scope is not None:
        scope[ldir] = result
    return result


_FAST: dict[str, tuple[tuple, tuple | None, dict, list]] = {}


def _lesson_paths(root: str) -> list[str]:
    ldir = paths.lessons_dir(root)
    return [
        p for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md")))
        if os.path.basename(p) != "lesson_template.md"
    ]


_MEMO: dict[str, tuple[tuple, dict, frozenset]] = {}


def _file_identity(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


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


def _write_cache(path: str, entries: dict) -> tuple | None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=CACHE_NAME + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"format_version": FORMAT_VERSION, "entries": entries}, fh)
                fh.flush()
                st = os.fstat(fh.fileno())
            os.replace(tmp, path)
            return (st.st_ino, st.st_mtime_ns, st.st_size)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError, TypeError):
        pass
    return None


def _valid_terms(terms: object) -> bool:
    if not isinstance(terms, list) or len(terms) != 4:
        return False
    return all(isinstance(field, list) and all(isinstance(t, str) for t in field)
               for field in terms)


def _stamps_differ(cached: dict, entries: dict) -> bool:
    if set(cached) != set(entries):
        return True
    for path, entry in entries.items():
        prior = cached.get(path)
        if prior is entry:
            continue
        if not isinstance(prior, dict):
            return True
        if (prior.get("mtime_ns"), prior.get("size")) != (entry.get("mtime_ns"), entry.get("size")):
            return True
        if prior.get("fm") != entry.get("fm"):
            return True
    return False


def _load_entries(root: str, reader=None) -> tuple[dict[str, dict], list[str]]:
    entries, ordered, _listing_key = _load_entries_keyed(root, reader)
    return entries, ordered


def _load_entries_keyed(root: str, reader) -> tuple[dict[str, dict], list[str], tuple]:
    if reader is None:
        def reader(p):  # noqa: E306
            try:
                return frontmatter.read(p)
            except Exception:
                return None

    try:
        listing = _listing(root)
    except OSError:
        return {}, [], ()

    cpath = cache_path(root)
    identity = _file_identity(cpath)
    fast = _FAST.get(cpath)
    if fast is not None and identity is not None and fast[1] == identity and fast[0] == listing:
        return fast[2], fast[3], fast[0]
    lesson_paths = [p for p, _m, _s in listing]
    stamps = {p: (m, size) for p, m, size in listing}
    memo = _MEMO.get(cpath)
    if memo is not None and identity is not None and memo[0] == identity:
        cached, validated = memo[1], memo[2]
    else:
        cached, validated = _read_cache(cpath), frozenset()

    entries: dict = {}
    repaired = False
    for path in lesson_paths:
        stamp = stamps[path]
        prior = cached.get(path)
        stamp_and_fm_match = (
            isinstance(prior, dict)
            and prior.get("mtime_ns") == stamp[0]
            and prior.get("size") == stamp[1]
            and isinstance(prior.get("fm"), dict)
        )
        if stamp_and_fm_match and (path in validated or _valid_terms(prior.get("terms"))):
            entries[path] = prior
            continue
        if stamp_and_fm_match:
            entries[path] = {**prior, "terms": field_terms(prior["fm"])}
            repaired = True
            continue

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

    if repaired or _stamps_differ(cached, entries):
        written = _write_cache(cpath, entries)
        if written is not None:
            _MEMO[cpath] = (written, entries, frozenset(entries))
        else:
            _MEMO.pop(cpath, None)
        identity = written
    elif identity is not None:
        _MEMO[cpath] = (identity, cached, validated | frozenset(entries))
    ordered = [p for p in lesson_paths if p in entries]
    if identity is not None:
        _FAST[cpath] = (listing, identity, entries, ordered)
    else:
        _FAST.pop(cpath, None)
    return entries, ordered, listing


def load_projected(root: str, reader=None) -> list[tuple[str, dict]]:
    """Every lesson in the store as (path, projected frontmatter)."""
    entries, ordered = _load_entries(root, reader=reader)
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


class TermCache(dict):
    """path -> tokenized fields, plus each lesson file's (mtime_ns, size)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stamps: dict[str, tuple[int, int]] = {}
        self.lessons: list | None = None
        self.fingerprint: tuple | None = None
        self.fingerprint_hash: int = 0
        self.bin_dir: str | None = None


_SNAPSHOTS: dict[tuple, tuple[tuple, list, TermCache]] = {}


def load_active_with_terms(
    root: str, agent_type: str | None = None, reader=None,
) -> tuple[list[tuple[str, dict]], dict[str, list[list[str]]]]:
    entries, ordered, snap_key = _load_entries_keyed(root, reader)
    memo_key = (os.path.abspath(root), agent_type)
    prior = _SNAPSHOTS.get(memo_key)
    if prior is not None and prior[0] == snap_key:
        return prior[1], prior[2]
    lessons: list[tuple[str, dict]] = []
    term_cache = TermCache()
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
        term_cache.stamps[path] = (entry["mtime_ns"], entry["size"])
    term_cache.lessons = lessons
    term_cache.fingerprint = tuple((p, term_cache.stamps[p]) for p, _fm in lessons)
    term_cache.fingerprint_hash = hash(term_cache.fingerprint)
    term_cache.bin_dir = os.path.join(paths.memory_dir(root), CACHE_DIR)
    _SNAPSHOTS[memo_key] = (snap_key, lessons, term_cache)
    return lessons, term_cache


def invalidate(root: str) -> None:
    try:
        os.unlink(cache_path(root))
    except OSError:
        pass
