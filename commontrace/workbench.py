from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import threading
from collections import OrderedDict

from commontrace import approval, draft_quality, frontmatter, lesson_admission, lesson_io, paths, redundancy
from commontrace.commands import lesson_cmd

MAX_BODY_CHARS = 20_000
MAX_FIELD_CHARS = 2_000
STATUSES = ("review", "active", "archived", "draft")

# Full frontmatter and bodies are needed for review gates and provenance. The
# retrieval cache intentionally projects those fields away, so keep this cache
# independent and validate every entry against the source on each request.
LESSON_CACHE_ENTRIES = 4096
LESSON_CACHE_BYTES = 16 * 1024 * 1024
_LESSON_CACHE: OrderedDict[str, tuple[tuple, dict, str, int]] = OrderedDict()
_LESSON_CACHE_BYTES = 0
_LESSON_CACHE_LOCK = threading.Lock()
REVIEW_CACHE_ENTRIES = 512
_REVIEW_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_REVIEW_CACHE_LOCK = threading.Lock()


class _ActiveTexts(list):
    """One content fingerprint for the scoped active corpus used by the gates."""

    def __init__(self, rows):
        super().__init__(rows)
        self.fingerprint = hashlib.sha256(json.dumps(self, ensure_ascii=False).encode("utf-8")).digest()


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _cached_lesson(path: str, stamp: tuple, *, isolated: bool = True) -> tuple[dict, str]:
    global _LESSON_CACHE_BYTES

    with _LESSON_CACHE_LOCK:
        cached = _LESSON_CACHE.get(path)
        if cached is not None and cached[0] == stamp:
            _LESSON_CACHE.move_to_end(path)
        else:
            if cached is not None:
                _LESSON_CACHE_BYTES -= _LESSON_CACHE.pop(path)[3]
            cached = None
    if cached is not None:
        # Editing a lesson changes its frontmatter in place. Never expose the
        # shared dictionary, including nested extraction/provenance fields.
        return (copy.deepcopy(cached[1]) if isolated else cached[1]), cached[2]

    fm, body = frontmatter.read(path)
    try:
        if _identity(os.stat(path)) != stamp:
            return fm, body  # A concurrent writer changed it while we read.
        size = len(body.encode("utf-8")) + len(json.dumps(fm, default=str).encode("utf-8"))
    except (OSError, ValueError, TypeError):
        return fm, body
    if size > LESSON_CACHE_BYTES or LESSON_CACHE_ENTRIES < 1:
        return fm, body
    # Parsing happens outside the lock; unrelated warm requests are not blocked
    # behind disk IO or YAML decoding. Revalidate before publishing the result.
    stored = copy.deepcopy(fm)
    with _LESSON_CACHE_LOCK:
        if _identity(os.stat(path)) != stamp:
            return fm, body
        prior = _LESSON_CACHE.pop(path, None)
        if prior is not None:
            _LESSON_CACHE_BYTES -= prior[3]
        _LESSON_CACHE[path] = (stamp, stored, body, size)
        _LESSON_CACHE_BYTES += size
        while len(_LESSON_CACHE) > LESSON_CACHE_ENTRIES or _LESSON_CACHE_BYTES > LESSON_CACHE_BYTES:
            _LESSON_CACHE_BYTES -= _LESSON_CACHE.popitem(last=False)[1][3]
    return fm, body


class WorkbenchError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _lessons(root: str, *, isolated: bool = True) -> list[tuple[str, dict, str]]:
    ldir = paths.lessons_dir(root)
    out = []
    try:
        with os.scandir(ldir) as entries:
            files = []
            for entry in entries:
                if not (entry.name.startswith("lesson_") and entry.name.endswith(".md")):
                    continue
                if entry.name == "lesson_template.md":
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    files.append((entry.name, entry.path, _identity(entry.stat())))
                except OSError:
                    continue
            files.sort()
    except OSError:
        return out
    for name, path, stamp in files:
        try:
            lesson_admission.validate_path(root, path)
            fm, body = _cached_lesson(os.path.abspath(path), stamp, isolated=isolated)
        except Exception:  # noqa: BLE001 - an unreadable lesson is skipped, not fatal to the queue
            continue
        slug = str(fm.get("name") or name.removesuffix(".md"))
        # Routing must identify the file read, rather than another lesson named
        # by untrusted metadata. Approval resolves this same canonical filename.
        if not lesson_io.SLUG_RE.fullmatch(slug) or lesson_io.canonical_slug(slug) != lesson_io.canonical_slug(name):
            continue
        out.append((slug, fm, body))
    return out


def _scope_allowed(fm: dict, scope: str) -> bool:
    if not scope:
        return True
    raw = fm.get("scopes")
    if not raw:
        return True
    scopes = {str(item).strip() for item in raw} if isinstance(raw, (list, tuple, set)) else {str(raw).strip()}
    return scope in scopes


def _active_texts(rows, exclude: str) -> list[tuple[str, str]]:
    return _ActiveTexts((slug, redundancy.comparable_text(fm, body)) for slug, fm, body in rows
                        if str(fm.get("status")) == "active" and slug != exclude)


def _active_texts_once(rows) -> list[tuple[str, str]]:
    """Active texts, computed once per call (file reads happen once in `_lessons`)."""
    return _ActiveTexts((slug, redundancy.comparable_text(fm, body)) for slug, fm, body in rows
                        if str(fm.get("status")) == "active")


def _checks(slug: str, fm: dict, body: str, active: list[tuple[str, str]]) -> dict:
    # Only reusable gate diagnostics are memoized, never authorization or an
    # approval decision. CLI approval still runs every production gate itself.
    key = None
    if isinstance(active, _ActiveTexts):
        key = (slug, hashlib.sha256(repr(fm).encode("utf-8")).digest(),
               hashlib.sha256(body.encode("utf-8")).digest(), active.fingerprint)
        with _REVIEW_CACHE_LOCK:
            cached = _REVIEW_CACHE.get(key)
            if cached is not None:
                _REVIEW_CACHE.move_to_end(key)
                return copy.deepcopy(cached)
    failed = draft_quality.gate_failures(fm, body, active)
    near = redundancy.closest(redundancy.comparable_text(fm, body), active, threshold=0.3)
    result = {"failed": failed, "passes": not failed,
              "nearest_active": {"slug": near.a, "similarity": round(near.similarity, 3)} if near else None}
    if key is not None:
        with _REVIEW_CACHE_LOCK:
            _REVIEW_CACHE[key] = copy.deepcopy(result)
            _REVIEW_CACHE.move_to_end(key)
            while len(_REVIEW_CACHE) > REVIEW_CACHE_ENTRIES:
                _REVIEW_CACHE.popitem(last=False)
    return result


def _summary(slug: str, fm: dict, body: str, active) -> dict:
    row = {"slug": slug, "status": str(fm.get("status", "")), "description": str(fm.get("description", ""))[:300],
           "domain": str(fm.get("domain", "")), "importance": copy.deepcopy(fm.get("importance")),
           "drafted_by_model": isinstance(fm.get("llm_draft"), dict),
           "source_traces": len(fm.get("source_traces") or []), "revises": copy.deepcopy(fm.get("revises")) or None,
           "revision": lesson_admission.digest_of(fm, body)}
    if row["status"] == "review":
        row["checks"] = _checks(slug, fm, body, active)
    return row


def _parse_pagination(limit, offset) -> tuple[int | None, int]:
    if limit is None:
        parsed_limit = None
    elif isinstance(limit, bool) or not isinstance(limit, int):
        raise WorkbenchError(400, "bad_request", "limit must be a positive integer")
    elif not 1 <= limit <= 1000:
        raise WorkbenchError(400, "bad_request", "limit must be between 1 and 1000")
    else:
        parsed_limit = limit
    if offset is None:
        return parsed_limit, 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise WorkbenchError(400, "bad_request", "offset must be a non-negative integer")
    return parsed_limit, offset


def count_lessons(root: str, status: str | None = None, scope: str = "") -> int:
    """How many lessons match `status`, without running any gates (cheap, no redundancy checks)."""
    if status is not None and status not in STATUSES:
        raise WorkbenchError(400, "bad_request", f"status must be one of {', '.join(STATUSES)}")
    return sum(
        1 for _slug, fm, _body in _lessons(root, isolated=False)
        if (not status or str(fm.get("status")) == status) and _scope_allowed(fm, scope)
    )


def list_lessons(root: str, status: str | None = None, limit: int | None = None,
                 offset: int = 0, scope: str = "") -> list[dict]:
    return lesson_page(root, status, limit, offset, scope)[0]


def lesson_page(root: str, status: str | None = None, limit: int | None = None,
                offset: int = 0, scope: str = "") -> tuple[list[dict], int]:
    """Return a page and its count from the same scoped source listing."""
    if status is not None and status not in STATUSES:
        raise WorkbenchError(400, "bad_request", f"status must be one of {', '.join(STATUSES)}")
    parsed_limit, parsed_offset = _parse_pagination(limit, offset)
    # These dictionaries remain internal read-only views; _summary copies every
    # mutable value it returns. Detail and edit use isolated metadata instead.
    rows = _lessons(root, isolated=False)
    scoped_rows = [row for row in rows if _scope_allowed(row[1], scope)]
    filtered = [(slug, fm, body) for slug, fm, body in scoped_rows
                if not status or str(fm.get("status")) == status]
    page = filtered if parsed_limit is None and not parsed_offset else filtered[
        parsed_offset:None if parsed_limit is None else parsed_offset + parsed_limit]
    active_all = _active_texts_once(scoped_rows) if any(fm.get("status") == "review" for _, fm, _ in page) else []
    out = []
    for slug, fm, body in page:
        if str(fm.get("status")) == "review":
            out.append(_summary(slug, fm, body, active_all))
        else:
            out.append(_summary(slug, fm, body, []))
    return out, len(filtered)


def _find(root: str, slug: str, scope: str = ""):
    if not isinstance(slug, str) or not lesson_io.SLUG_RE.match(slug):
        raise WorkbenchError(400, "bad_request", "slug must be letters, digits, '_' or '-'")
    want = lesson_io.canonical_slug(slug)
    rows = _lessons(root)
    for row in rows:
        if lesson_io.canonical_slug(row[0]) == want and _scope_allowed(row[1], scope):
            return row, rows
    raise WorkbenchError(404, "not_found", f"no lesson {slug!r}")


def _section(body: str, name: str) -> str:
    m = re.search(rf"^##\s*{re.escape(name)}\s*\n(.*?)(?=\n##\s|\Z)", body, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def detail(root: str, slug: str, scope: str = "") -> dict:
    (name, fm, body), rows = _find(root, slug, scope)
    scoped_rows = [row for row in rows if _scope_allowed(row[1], scope)]
    out = _summary(name, fm, body, _active_texts(scoped_rows, name) if fm.get("status") == "review" else [])
    out.update({
        "applies_when": str(fm.get("applies_when", "")), "do_not_apply_when": str(fm.get("do_not_apply_when", "")),
        "rule": _section(body, "Rule")[:MAX_FIELD_CHARS], "body": body[:MAX_BODY_CHARS],
        "body_truncated": len(body) > MAX_BODY_CHARS,
        "provenance": fm.get("llm_draft") if isinstance(fm.get("llm_draft"), dict) else None,
        "history": [{"at": r.get("at"), "from": r.get("from"), "to": r.get("to"), "actor": r.get("actor"),
                     "reason": r.get("reason")} for r in lesson_io.history(root, name)][-50:],
    })
    try:
        approval.check(approval.load_policy(root), slug=name, approver="console",
                       authors=approval.authors_of(root, name))
        out["separation_of_duties"] = None
    except (approval.ApprovalDenied, approval.PolicyError) as exc:
        out["separation_of_duties"] = str(exc)
    return out


def _clean(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchError(400, "bad_request", f"{field} must be a non-empty string")
    if len(value) > MAX_FIELD_CHARS:
        raise WorkbenchError(400, "bad_request", f"{field} is longer than {MAX_FIELD_CHARS} characters")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise WorkbenchError(400, "bad_request", f"{field} contains control characters")
    return value.strip()


def _expected_revision(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None):
        raise WorkbenchError(400, "bad_request", "expected_revision must be a lowercase SHA256 digest")
    return value


def _review_path(root: str, path: str) -> None:
    try:
        lesson_admission.validate_path(root, path)
    except lesson_admission.AdmissionError:
        raise WorkbenchError(404, "not_found", "lesson is unavailable") from None


def _review_precondition(root: str, slug: str, scope: str, expected_revision: str | None,
                         path: str, fm: dict, body: str) -> None:
    """Check authoritative bytes while holding their write lock, before gates/signing."""
    _review_path(root, path)
    name = str(fm.get("name") or os.path.basename(path).removesuffix(".md"))
    if lesson_io.canonical_slug(name) != lesson_io.canonical_slug(slug) or not _scope_allowed(fm, scope):
        raise WorkbenchError(404, "not_found", "lesson is unavailable")
    if expected_revision is not None and lesson_admission.digest_of(fm, body) != expected_revision:
        raise WorkbenchError(409, "stale_review", "lesson changed since it was reviewed; reload before acting")
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be changed here")


def edit(root: str, slug: str, fields: dict, actor: str, scope: str = "",
         expected_revision: str | None = None) -> dict:
    expected_revision = _expected_revision(expected_revision)
    (name, _fm, _body), _rows = _find(root, slug, scope)
    allowed = {"rule", "applies_when", "do_not_apply_when", "description"}
    unknown = set(fields) - allowed
    if unknown or not fields:
        raise WorkbenchError(400, "bad_request", f"give one or more of: {', '.join(sorted(allowed))}")
    clean = {key: _clean(value, key) for key, value in fields.items()}
    path = lesson_io.lesson_path(root, name)
    if path is None:
        raise WorkbenchError(404, "not_found", "lesson is unavailable")
    with frontmatter.locked(path):
        _review_path(root, path)
        fm, body = frontmatter.read(path)
        _review_precondition(root, name, scope, expected_revision, path, fm, body)
        for field in ("description", "applies_when", "do_not_apply_when"):
            if field in clean:
                fm[field] = clean[field]
        for field, section in (("rule", "Rule"), ("applies_when", "How to apply"),
                               ("do_not_apply_when", "Counter-examples")):
            if field in clean:
                body = _replace_section(body, section, clean[field])
        lesson_io.write_lesson(path, fm, body, root=root, actor=actor, reason="edited in the console")
    return detail(root, name, scope)


def _replace_section(body: str, name: str, text: str) -> str:
    pattern = re.compile(rf"(^##\s*{re.escape(name)}\s*\n)(.*?)(?=\n##\s|\Z)", re.S | re.M | re.I)
    if pattern.search(body):
        return pattern.sub(lambda m: m.group(1) + text + "\n", body, count=1)
    return body.rstrip("\n") + f"\n\n## {name}\n{text}\n"


def _run(fn, ns: argparse.Namespace) -> tuple[int, str]:
    from commontrace.ui_commands import _COMMAND_LOCK

    err = io.StringIO()
    # Both console entry points redirect process-global streams. Share their
    # lock so simultaneous commands cannot disclose output to another request.
    with _COMMAND_LOCK, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = fn(ns)
    return code, err.getvalue().strip()


def approve(root: str, slug: str, rationale: str | None, actor: str, scope: str = "",
            expected_revision: str | None = None) -> dict:
    expected_revision = _expected_revision(expected_revision)
    if rationale is not None and not isinstance(rationale, str):
        raise WorkbenchError(400, "bad_request", "rationale must be a string")
    (name, _fm, _body), _rows = _find(root, slug, scope)
    def precondition(path: str, current: dict, body: str) -> None:
        _review_precondition(root, name, scope, expected_revision, path, current, body)
        if len(body) > MAX_BODY_CHARS:
            raise WorkbenchError(409, "review_truncated",
                                 "lesson exceeds the console review limit; review it in the terminal")

    ns = argparse.Namespace(slug=name, dest=root, force=False, rationale=(rationale or "").strip() or None,
                            approver=actor, scope=scope,
                            review_precondition=precondition)
    code, message = _run(lesson_cmd.run_approve, ns)
    if code != 0:
        raise WorkbenchError(409, "refused", message or "the approval gates refused this lesson")
    return {"slug": name, "status": "active"}


def reject(root: str, slug: str, reason: str, scope: str = "", expected_revision: str | None = None) -> dict:
    expected_revision = _expected_revision(expected_revision)
    (name, _fm, _body), _rows = _find(root, slug, scope)
    ns = argparse.Namespace(slug=name, dest=root, reason=_clean(reason, "reason"),
                            review_precondition=lambda path, current, body: _review_precondition(
                                root, name, scope, expected_revision, path, current, body))
    code, message = _run(lesson_cmd.run_reject, ns)
    if code != 0:
        raise WorkbenchError(409, "refused", message or "could not reject")
    return {"slug": name, "status": "archived"}
