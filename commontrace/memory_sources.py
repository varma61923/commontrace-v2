"""Causal measurement for memory that lives in a plain file, not this store."""
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
    id: str
    heading: str
    text: str


def parse_sections(content: str) -> tuple[str, list[Section]]:
    """Split `content` at its top-level (`##`) headings."""
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
    withheld: list[str]
    blocked: list[str]
    harmful: list[str] = field(default_factory=list)


def blocklist_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), BLOCKLIST_NAME)


def _source_key(root: str, path: str) -> str:
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:
        return abs_path
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
    return load_blocklist(root).get(key, set())


def _write_blocklist_raw(path: str, raw: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def withdraw(root: str, path: str, section_id: str) -> None:
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
        _preamble, sections = parse_sections(self._read())
        return sections

    def render(self, occasion_id: str) -> RenderResult:
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
