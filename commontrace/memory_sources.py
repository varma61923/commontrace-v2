"""Causal measurement for memory that lives in a plain file, not this store.

`commontrace/measure.py`'s `CausalMemory` wraps a *retrieval call* -- ask a
query, get back ranked items, withhold a random fraction, log the arm. That
fits Mem0/Zep/any store with a `search()` method. It does not fit the memory
an agent platform writes to a plain file and reads back whole: CLAUDE.md,
AGENTS.md, `.cursor/rules/*.mdc`, a Devin Knowledge export. There is no query
and no ranked list -- the entire file is placed in every session's context,
unconditionally. The unit of assignment the spec calls for is different too:
not a lesson, but "one memory item or file" -- here, one `##`-delimited
section of the file.

FileMemorySource bridges the two: it splits the file into sections, and for
one occasion decides which sections to hand to CausalMemory as the eligible
set for a synthetic, session-scoped "retrieval". The holdout draw, the
append-only log, the revision hash, the outcome join and `commontrace
experiment`'s analysis are then the exact same code path every lesson in
this store already goes through -- render() does not add a second causal
implementation, it adds a second way to produce the list CausalMemory reads.

WHAT AN OCCASION ACTUALLY SEES
-------------------------------
`render(occasion_id)` returns the file's text with the withheld sections
removed. The caller is responsible for making the agent's session actually
read THAT text instead of the file on disk for this occasion -- this module
has no opinion on how a given platform is told to do that (a pre-session
copy, a wrapper that intercepts the read, whatever the integration needs).
What it guarantees is that the same occasion_id always gets the same
answer (the underlying draw is deterministic, see holdout_io.assign_and_log)
and that the withheld/blocked sections are named, never silently dropped.

SECTION IDENTITY, AND ITS LIMIT
--------------------------------
An id is derived from a section's heading text, not its position -- a file
reordered by its owner between two occasions must not silently reassign
which section an id refers to. This is not perfect: two sections that share
an exact heading (two "## Notes" blocks) are told apart only by the order
they first appear in, so an edit that reorders two IDENTICALLY TITLED
sections can swap their identity. That failure is caught downstream rather
than prevented here: each section's body text is hashed into the logged
`revision` exactly as commontrace/measure.py does for any external memory,
and a swapped identity changes the revision, which the existing stability
check (commontrace/revision.py, read via commontrace/integrity.py) already
treats as "this id no longer means the same thing" rather than silently
pooling the two.

HARM WITHDRAWAL DOES NOT EDIT THE FILE
----------------------------------------
CLAUDE.md/AGENTS.md is usually not this product's file to rewrite -- another
tool writes it, a person's editor has it open, and its git history belongs
to whoever authored it. So a section found HARMFUL is never removed from
disk. `withdraw()` records its id in `memory/source_blocklist.json`, and
every future `render()` drops that id unconditionally, before the holdout
draw even runs -- the "otherwise it becomes a blocklist enforced at
injection" branch for a source this product does not own.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from commontrace import frontmatter, holdout_io, measure, paths

_HEADING_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

BLOCKLIST_NAME = "source_blocklist.json"


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "section"


@dataclass(frozen=True)
class Section:
    """One `##`-delimited block. `text` is the exact original substring
    (heading line plus body, including any nested `###` subheadings), so
    re-joining a subset of sections in original order reproduces the source
    file byte-for-byte apart from the sections left out."""

    id: str
    heading: str
    text: str


def parse_sections(content: str) -> tuple[str, list[Section]]:
    """Split `content` at its top-level (`##`) headings.

    Returns `(preamble, sections)`. `preamble` is everything before the
    first `##` heading -- typically a title and an introduction -- and is
    never a candidate for holdout: it is not a discrete claim an agent can
    selectively use, and withholding it would usually remove the file's own
    title. A file with no `##` heading at all returns `(content, [])`: it is
    one indivisible block, and treating it as a single "section" of unstated
    identity would need a name to key it on that this function has no way to
    invent honestly.
    """
    matches = list(_HEADING_RE.finditer(content))
    if not matches:
        return content, []
    preamble = content[: matches[0].start()]
    sections: list[Section] = []
    seen: dict[str, int] = {}
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        heading = m.group(1).strip()
        base = _slugify(heading)
        seen[base] = seen.get(base, 0) + 1
        section_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        sections.append(Section(id=section_id, heading=heading, text=content[start:end]))
    return preamble, sections


@dataclass(frozen=True)
class RenderResult:
    text: str
    #: section ids withheld by the random holdout draw on this occasion.
    withheld: list[str]
    #: section ids permanently excluded by withdraw(), before any draw ran.
    blocked: list[str]
    #: section ids left out because the store's harm policy withdrew them on a
    #: measured HURTS verdict (commontrace/measure.py). Follows the evidence,
    #: so it is empty again after a new randomization.
    harmful: list[str] = field(default_factory=list)


def blocklist_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), BLOCKLIST_NAME)


def _source_key(root: str, path: str) -> str:
    """`path`, relative to `root` when it lives inside the store, else
    absolute -- so the blocklist stays meaningful if this store is checked
    out at a different location, provided the source file moves with it."""
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:
        return abs_path  # Windows: different drive, relpath cannot express it
    return abs_path if rel.startswith("..") else rel.replace(os.sep, "/")


def _read_blocklist_raw(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_blocklist(root: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key, ids in _read_blocklist_raw(blocklist_path(root)).items():
        if isinstance(ids, list):
            out[str(key)] = {str(i) for i in ids}
    return out


def blocked_ids(root: str, path: str) -> set[str]:
    return blocked_ids_for_key(root, _source_key(root, path))


def blocked_ids_for_key(root: str, key: str) -> set[str]:
    """Like blocked_ids, for a source identified by a name rather than a
    file path (commontrace/memory_adapters.py)."""
    return load_blocklist(root).get(key, set())


def _write_blocklist_raw(path: str, raw: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def withdraw(root: str, path: str, section_id: str) -> None:
    """Permanently exclude `section_id` of the file at `path` from every
    future render, on every occasion, ahead of the holdout draw. See this
    module's docstring for why this never edits `path` itself."""
    withdraw_key(root, _source_key(root, path), section_id)


def withdraw_key(root: str, key: str, section_id: str) -> None:
    blocklist_file = blocklist_path(root)
    with frontmatter.locked(blocklist_file):
        raw = _read_blocklist_raw(blocklist_file)
        ids = raw.get(key)
        ids = list(ids) if isinstance(ids, list) else []
        if section_id not in ids:
            ids.append(section_id)
        raw[key] = ids
        _write_blocklist_raw(blocklist_file, raw)


def reinstate(root: str, path: str, section_id: str) -> bool:
    """Undo `withdraw`. Returns True if `section_id` was actually blocked."""
    return reinstate_key(root, _source_key(root, path), section_id)


def reinstate_key(root: str, key: str, section_id: str) -> bool:
    blocklist_file = blocklist_path(root)
    with frontmatter.locked(blocklist_file):
        raw = _read_blocklist_raw(blocklist_file)
        ids = raw.get(key)
        if not isinstance(ids, list) or section_id not in ids:
            return False
        ids = [i for i in ids if i != section_id]
        if ids:
            raw[key] = ids
        else:
            raw.pop(key, None)
        _write_blocklist_raw(blocklist_file, raw)
    return True


class FileMemorySource:
    """One CLAUDE.md/AGENTS.md/.cursor-rules-style file, measured through
    the same holdout machinery as any other memory in this product.

    Never writes to `path`. A section withheld for an occasion is withheld
    by producing a different rendered TEXT for that occasion to read, the
    same reasoning commontrace/adapters.py already gives for reading files
    rather than live APIs during import: the source stays inspectable and
    stays exactly what its own owner wrote.
    """

    def __init__(self, path: str, *, root: str | None = None, label: str | None = None) -> None:
        self.path = os.path.abspath(path)
        self._root = paths.resolve_root(root)
        self._label = label or os.path.basename(self.path)

    @property
    def root(self) -> str:
        return self._root

    def _read(self) -> str:
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def sections(self) -> list[Section]:
        """The file's current sections, read fresh from disk every call --
        this source is read-only, so there is no cached copy to go stale."""
        _preamble, sections = parse_sections(self._read())
        return sections

    def render(self, occasion_id: str) -> RenderResult:
        """The text this occasion should actually be given: the preamble,
        plus every eligible section not drawn into the withheld arm, in
        original order. Blocked sections never reach the holdout draw at
        all -- they are not under test, the same way an inactive lesson is
        never a candidate for retrieval."""
        content = self._read()
        preamble, sections = parse_sections(content)
        blocked = blocked_ids(self._root, self.path)
        eligible = [s for s in sections if s.id not in blocked]

        if not eligible:
            return RenderResult(text=preamble, withheld=[], blocked=[s.id for s in sections if s.id in blocked])

        items = [{"id": s.id, "memory": s.text} for s in eligible]
        memory = measure.CausalMemory(
            lambda _query, **_kwargs: items,
            root=self._root,
            scorer=f"file:{self._label}",
        )
        recalled = memory.recall_detailed(self.path, occasion_id=occasion_id)
        delivered_ids = {d["id"] for d in recalled.items}
        rendered = preamble + "".join(item["memory"] for item in items if item["id"] in delivered_ids)
        harmful = [item["id"] for item in items if item["id"] in recalled.withdrawn]
        withheld = [
            item["id"] for item in items
            if item["id"] not in delivered_ids and item["id"] not in recalled.withdrawn
        ]
        return RenderResult(
            text=rendered,
            withheld=withheld,
            blocked=[s.id for s in sections if s.id in blocked],
            harmful=harmful,
        )

    def record_outcome(self, occasion_id: str, *, succeeded: bool) -> bool:
        return holdout_io.record_outcome(self._root, occasion_id, succeeded)
