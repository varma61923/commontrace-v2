"""Curated substrate lesson packs: `commontrace kb install <pack>` seeds a
store with reviewed, source-cited knowledge about failures every fleet
rediscovers independently -- webhook redelivery, migration locks, SIGTERM
during a deploy -- without that store having to hit each one first.

WHERE THE CONTENT COMES FROM
------------------------------
Every pack is a subset of `commons/seed/substrate-v1.jsonl`, the
operator-curated Knowledge Base corpus this repository already ships and
already measures (`commons/eval/`), grouped by tag. Nothing in a pack was
written for this feature; a pack is a selection, not new content. Each
record keeps the `source` citation it was curated with.

The pack files live INSIDE the `commontrace` package (`kb_packs/*.jsonl`,
declared in pyproject.toml's package-data) rather than read from
`commons/`, which exists only in a repository checkout: a command that
works from a clone and fails after `pip install` is the gap
pyproject.toml's own package-data comments already describe for
`reference/*.py`.

WHAT INSTALLING DOES -- AND DELIBERATELY DOES NOT
----------------------------------------------------
Each record becomes a lesson at `status: review`, never `active`. Generic
substrate knowledge is still a claim about the reader's own stack, and the
only judgement a pack cannot make is WHERE in that stack the rule does
NOT apply -- so `do_not_apply_when` and `## Counter-examples` are left as
`TODO:` scaffolding, which `commontrace lesson approve` refuses until a
person fills them in (commontrace/templates.py). Inventing a
counter-condition for a fleet this module has never seen would be exactly
the fabricated text that gate exists to stop.

An already-installed lesson is never overwritten: a reviewer's edits to
it are the valuable part, and re-running `kb install` after a pack update
must not erase them. It is reported as skipped instead.

Each installed lesson records `kb_pack: {name, version}`, where `version`
is a hash of the pack file's own bytes -- reproducible, and changes only
when the content does.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from commontrace import lesson_io, paths, templates

PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_packs")

#: One line per pack, shown by `kb list`. A pack with no entry here still
#: installs; it just lists with no description.
DESCRIPTIONS = {
    "databases": "Migrations, locks, indexes, ORMs, collation -- failures at the database layer.",
    "security": "Secrets, auth, injection, path traversal, hardening defaults.",
    "kubernetes-deployment": "Shutdown, probes, zero-downtime migrations, container image hygiene.",
    "concurrency-resilience": "Retries, backoff, idempotency, races, cascading failure.",
}

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")


class UnknownPack(ValueError):
    pass


@dataclass(frozen=True)
class PackInfo:
    name: str
    count: int
    version: str
    description: str


@dataclass
class InstallResult:
    pack: str
    version: str
    written: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)


def _pack_path(name: str) -> str:
    """The file for `name`, refusing anything that is not a plain pack
    name -- a pack name reaches a filesystem path, so `../x` must be
    rejected here rather than resolved."""
    if not _NAME_RE.match(name or ""):
        raise UnknownPack(f"not a valid pack name: {name!r}")
    path = os.path.join(PACKS_DIR, f"{name}.jsonl")
    if not os.path.isfile(path):
        raise UnknownPack(f"no such pack: {name!r} (see `commontrace kb list`)")
    return path


def _version(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def _read(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def list_packs() -> list[PackInfo]:
    out = []
    for filename in sorted(os.listdir(PACKS_DIR)):
        if not filename.endswith(".jsonl"):
            continue
        name = filename[: -len(".jsonl")]
        path = os.path.join(PACKS_DIR, filename)
        out.append(PackInfo(
            name=name, count=len(_read(path)), version=_version(path),
            description=DESCRIPTIONS.get(name, ""),
        ))
    return out


def _slug(pack: str, title: str) -> str:
    stem = _SLUG_CHARS_RE.sub("_", title.lower()).strip("_")[:60].rstrip("_")
    return f"lesson_kb_{pack.replace('-', '_')}_{stem or 'entry'}"


def _lesson(record: dict, *, pack: str, version: str, slug: str, agent_type: str) -> tuple[dict, str]:
    title = str(record.get("title") or "").strip()
    situation = str(record.get("context_text") or "").strip()
    rule = str(record.get("solution_text") or "").strip()
    source = str(record.get("source") or "").strip()
    tags = [str(t) for t in record.get("tags") or []]

    fm = templates.lesson_frontmatter(
        slug=slug,
        description=title,
        agent_type=agent_type,
        domain=pack,
        tags=tags,
        applies_when=situation,
        do_not_apply_when=(
            "TODO: where this does NOT apply in your own stack -- the pack cannot know"
        ),
        importance=3,
        importance_rationale=f"Installed from the curated '{pack}' pack; calibrate for this fleet.",
        status="review",
    )
    fm["kb_pack"] = {"name": pack, "version": version}
    body = (
        f"## Rule\n{rule}\n\n"
        f"## Why\n{situation}\n\nSource: {source or '(none recorded)'}\n\n"
        f"## How to apply\nWhen {situation[:1].lower() + situation[1:] if situation else 'this situation arises'}.\n\n"
        "## Counter-examples\nTODO: cases in this fleet's stack where the rule does NOT apply.\n"
    )
    return fm, body


def install_pack(root: str, name: str, *, agent_type: str | None = None) -> InstallResult:
    path = _pack_path(name)
    version = _version(path)
    records = _read(path)
    store_type = paths.store_agent_type(root)
    result = InstallResult(pack=name, version=version)

    lessons_dir = paths.lessons_dir(root)
    os.makedirs(lessons_dir, exist_ok=True)
    seen: set[str] = set()
    for record in records:
        slug = _slug(name, str(record.get("title") or ""))
        # Two titles that collapse to the same slug get distinct files
        # rather than the second silently skipping as "already installed".
        base, n = slug, 2
        while slug in seen:
            slug, n = f"{base}_{n}", n + 1
        seen.add(slug)

        out_path = os.path.join(lessons_dir, f"{slug}.md")
        if os.path.exists(out_path):
            result.skipped_existing.append(slug)
            continue
        fm, body = _lesson(
            record, pack=name, version=version, slug=slug,
            # Explicit choice first, then what the record was curated for,
            # then whatever this store was initialized as.
            agent_type=str(agent_type or record.get("agent_type") or store_type),
        )
        lesson_io.write_lesson(
            out_path, fm, body, root=root, actor="kb-install",
            reason=f"installed from kb pack {name}@{version}",
        )
        result.written.append(slug)
    return result
