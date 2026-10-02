"""Discoverable skills: reusable procedures an agent can load on demand.

Three sources, in priority order (first wins on name collision):

1. project — ``<root>/skills/*/SKILL.md`` and ``<root>/skills/*.md``
2. user — ``~/.commontrace/skills/*/SKILL.md`` and ``~/.commontrace/skills/*.md``
3. bundled — the curated ``commontrace/kb_packs/*.jsonl`` packs

Every file-backed skill is a Markdown file with YAML frontmatter::

    ---
    name: my-skill
    description: What it does. Use when ...
    when_to_use: optional trigger hint
    user_invocable: false
    ---

    <instructions>

Only ``name`` and ``description`` are required. ``when_to_use`` and
``user_invocable`` are optional. Files whose frontmatter fails validation
are skipped (rejected), never half-loaded.

The agent loop injects only ``name`` + ``description`` into context;
the full ``SKILL.md`` body loads on demand via :func:`load_body`.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
NAME_MAX_LEN = 64

SOURCE_PROJECT = "project"
SOURCE_USER = "user"
SOURCE_BUNDLED = "bundled"


@dataclass(frozen=True)
class Skill:
    """One reusable procedure, ready to list or load."""

    name: str
    description: str
    when_to_use: str = ""
    user_invocable: bool = False
    source: str = SOURCE_PROJECT
    path: str = ""
    body: str = ""

    def index_line(self) -> str:
        """The one-line ``name: description`` form injected into context."""
        return f"- **{self.name}**: {self.description}"


class SkillError(ValueError):
    """A skill file was readable but not usable."""


def validate_skill_frontmatter(fm: object, path: str = "<skill>") -> list[str]:
    """Human-readable validation errors for a skill frontmatter mapping."""
    errors: list[str] = []
    if not isinstance(fm, dict):
        return [f"{path}: frontmatter must be a YAML mapping"]
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{path}: missing required field 'name'")
    elif not NAME_RE.match(name.strip()) or len(name.strip()) > NAME_MAX_LEN:
        errors.append(
            f"{path}: 'name' must match {NAME_RE.pattern} "
            f"(max {NAME_MAX_LEN} chars), got {name!r}"
        )
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append(f"{path}: missing required field 'description'")
    if "when_to_use" in fm and fm["when_to_use"] is not None:
        if not isinstance(fm["when_to_use"], str):
            errors.append(
                f"{path}: 'when_to_use' must be a string, "
                f"got {type(fm['when_to_use']).__name__}"
            )
    if "user_invocable" in fm and fm["user_invocable"] is not None:
        if not isinstance(fm["user_invocable"], bool):
            errors.append(
                f"{path}: 'user_invocable' must be a boolean, "
                f"got {type(fm['user_invocable']).__name__}"
            )
    return errors


def project_skills_dir(root: str) -> str:
    return os.path.join(os.path.abspath(root), "skills")


def user_skills_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".commontrace", "skills")


def _candidate_files(skills_dir: str) -> list[str]:
    """SKILL.md files under *skills_dir* (``*/SKILL.md`` + flat ``*.md``)."""
    if not os.path.isdir(skills_dir):
        return []
    out: list[str] = []
    try:
        out.extend(sorted(glob.glob(os.path.join(skills_dir, "*", "SKILL.md"))))
    except OSError:
        pass
    try:
        for path in sorted(glob.glob(os.path.join(skills_dir, "*.md"))):
            base = os.path.basename(path)
            if base.startswith(".") or base.endswith(".tmp"):
                continue
            out.append(path)
    except OSError:
        pass
    # Skip hidden dirs / template-ish files.
    kept = []
    for path in out:
        parts = os.path.relpath(path, skills_dir).split(os.sep)
        if any(p.startswith(".") for p in parts):
            continue
        kept.append(path)
    return sorted(set(kept))


def _from_file(path: str, source: str) -> Skill | None:
    """Parse one skill file; None when missing/unreadable/invalid (rejected)."""
    from commontrace import frontmatter

    try:
        fm, body = frontmatter.read(path)
    except Exception:
        return None
    if validate_skill_frontmatter(fm, path):
        return None
    name = str(fm["name"]).strip()
    description = str(fm["description"]).strip()
    raw_when = fm.get("when_to_use")
    when_to_use = str(raw_when).strip() if isinstance(raw_when, str) else ""
    raw_inv = fm.get("user_invocable")
    user_invocable = bool(raw_inv) if isinstance(raw_inv, bool) else False
    return Skill(
        name=name,
        description=description,
        when_to_use=when_to_use,
        user_invocable=user_invocable,
        source=source,
        path=os.path.abspath(path),
        body=body,
    )


def _discover_in_dir(skills_dir: str, source: str) -> list[Skill]:
    found: list[Skill] = []
    for path in _candidate_files(skills_dir):
        skill = _from_file(path, source)
        if skill is not None:
            found.append(skill)
    found.sort(key=lambda s: s.name)
    return found


def _bundled_skills() -> list[Skill]:
    """The curated kb_packs exposed as skills (lowest priority)."""
    try:
        from commontrace import kb_packs
    except Exception:
        return []
    try:
        packs = kb_packs.list_packs()
    except Exception:
        return []
    out: list[Skill] = []
    for pack in packs:
        try:
            pack_path = os.path.join(kb_packs.PACKS_DIR, f"{pack.name}.jsonl")
        except Exception:
            pack_path = ""
        description = (pack.description or "").strip() or (
            f"Curated '{pack.name}' knowledge pack ({pack.count} lessons)."
        )
        if not NAME_RE.match(pack.name):
            continue
        out.append(Skill(
            name=pack.name,
            description=description,
            when_to_use="",
            user_invocable=False,
            source=SOURCE_BUNDLED,
            path=os.path.abspath(pack_path) if pack_path else "",
            body="",
        ))
    out.sort(key=lambda s: s.name)
    return out


def discover(
    root: str,
    *,
    user_dir: str | None = None,
    include_bundled: bool = True,
) -> list[Skill]:
    """All skills visible from *root*, project-overrides-bundled.

    Order is priority order: project, then user, then bundled. The first
    skill with a given name wins; later duplicates are dropped.
    """
    project = _discover_in_dir(project_skills_dir(root), SOURCE_PROJECT)
    udir = os.path.abspath(user_dir) if user_dir is not None else user_skills_dir()
    user = _discover_in_dir(udir, SOURCE_USER)
    bundled = _bundled_skills() if include_bundled else []

    ordered: list[Skill] = []
    seen: set[str] = set()
    for skill in project + user + bundled:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        ordered.append(skill)
    return ordered


def get_skill(root: str, name: str, **kwargs) -> Skill | None:
    """The skill called *name*, or None (project shadows user shadows bundled)."""
    for skill in discover(root, **kwargs):
        if skill.name == name:
            return skill
    return None


def load_body(skill: Skill) -> str:
    """Full SKILL.md instructions for *skill*, read on demand."""
    if skill.path and skill.path.endswith(".md"):
        try:
            from commontrace import frontmatter

            _fm, body = frontmatter.read(skill.path)
            return body
        except Exception:
            pass
    return skill.body


def format_for_context(skills: list[Skill], *, limit: int = 20) -> str:
    """The ``name + description`` list the agent loop injects (no bodies)."""
    lines = [
        "# Available Skills",
        "",
        "Each skill is a reusable procedure. Name and description only — "
        "load the full SKILL.md on demand when the task matches.",
        "",
    ]
    for skill in skills[:limit]:
        lines.append(skill.index_line())
    if len(skills) > limit:
        lines.append(f"- ... and {len(skills) - limit} more")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# mtime-polling watcher (commontrace/watch.py snapshot idiom)
# ---------------------------------------------------------------------------

def _watched_skill_files(root: str, user_dir: str | None) -> list[str]:
    """Absolute skill-file paths watched for mtime/size changes."""
    dirs = [project_skills_dir(root)]
    udir = os.path.abspath(user_dir) if user_dir is not None else user_skills_dir()
    dirs.append(udir)
    out: list[str] = []
    for skills_dir in dirs:
        out.extend(_candidate_files(skills_dir))
    return sorted(set(os.path.abspath(p) for p in out))


def snapshot_skill_files(root: str, user_dir: str | None = None) -> dict[str, list[int]]:
    """abspath -> [mtime_ns, size] for every skill file present on disk."""
    snap: dict[str, list[int]] = {}
    for path in _watched_skill_files(root, user_dir):
        try:
            st = os.stat(path)
        except OSError:
            continue
        snap[os.path.abspath(path)] = [st.st_mtime_ns, st.st_size]
    return snap


class SkillWatcher:
    """Poll for skill changes; reload the cached :func:`discover` list.

    Mirrors ``commontrace/watch.py``: state is a snapshot mapping path ->
    ``[mtime_ns, size]``. :meth:`poll` diffs the live tree against it and
    refreshes the cache when anything changed (new + modified + deleted).
    Bundled kb_packs are static and not part of the snapshot, but they are
    included in the cached list when *include_bundled* is true.
    """

    def __init__(
        self,
        root: str,
        *,
        user_dir: str | None = None,
        include_bundled: bool = True,
    ) -> None:
        self.root = os.path.abspath(root)
        self.user_dir = os.path.abspath(user_dir) if user_dir is not None else user_skills_dir()
        self.include_bundled = include_bundled
        self._snapshot = snapshot_skill_files(self.root, self.user_dir)
        self._skills: list[Skill] = discover(
            self.root, user_dir=self.user_dir, include_bundled=self.include_bundled,
        )

    @property
    def skills(self) -> list[Skill]:
        return list(self._skills)

    def snapshot(self) -> dict[str, list[int]]:
        return dict(self._snapshot)

    def poll(self) -> tuple[bool, list[Skill]]:
        """Re-scan; return ``(changed, skills)`` (refreshes cache if changed)."""
        current = snapshot_skill_files(self.root, self.user_dir)
        if current == self._snapshot:
            return False, list(self._skills)
        self._snapshot = current
        self._skills = discover(
            self.root, user_dir=self.user_dir, include_bundled=self.include_bundled,
        )
        return True, list(self._skills)

    def refresh_if_changed(self) -> bool:
        """Reload the cache when skill files changed; True when reloaded."""
        changed, _ = self.poll()
        return changed

    def get(self, name: str) -> Skill | None:
        for skill in self._skills:
            if skill.name == name:
                return skill
        return None
