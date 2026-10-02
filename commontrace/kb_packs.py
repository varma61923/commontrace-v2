from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from commontrace import lesson_io, paths, templates

PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_packs")

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
            agent_type=str(agent_type or record.get("agent_type") or store_type),
        )
        lesson_io.write_lesson(
            out_path, fm, body, root=root, actor="kb-install",
            reason=f"installed from kb pack {name}@{version}",
        )
        result.written.append(slug)
    return result
