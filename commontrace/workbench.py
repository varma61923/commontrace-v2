from __future__ import annotations

import argparse
import contextlib
import io
import re

from commontrace import approval, draft_quality, frontmatter, lesson_io, paths, redundancy
from commontrace.commands import lesson_cmd

MAX_BODY_CHARS = 20_000
MAX_FIELD_CHARS = 2_000
STATUSES = ("review", "active", "archived", "draft")


class WorkbenchError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _lessons(root: str) -> list[tuple[str, dict, str]]:
    import os

    ldir = paths.lessons_dir(root)
    out = []
    if not os.path.isdir(ldir):
        return out
    for name in sorted(os.listdir(ldir)):
        if not (name.startswith("lesson_") and name.endswith(".md")) or name == "lesson_template.md":
            continue
        try:
            fm, body = frontmatter.read(os.path.join(ldir, name))
        except Exception:  # noqa: BLE001 - an unreadable lesson is skipped, not fatal to the queue
            continue
        out.append((str(fm.get("name") or name.removesuffix(".md")), fm, body))
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
    return [(slug, redundancy.comparable_text(fm, body)) for slug, fm, body in rows
            if str(fm.get("status")) == "active" and slug != exclude]


def _active_texts_once(rows) -> list[tuple[str, str]]:
    """Active texts, computed once per call (file reads happen once in `_lessons`)."""
    comparable = {slug: redundancy.comparable_text(fm, body) for slug, fm, body in rows}
    return [(slug, comparable[slug]) for slug, fm, _body in rows if str(fm.get("status")) == "active"]


def _checks(slug: str, fm: dict, body: str, active: list[tuple[str, str]]) -> dict:
    failed = draft_quality.gate_failures(fm, body, active)
    near = redundancy.closest(redundancy.comparable_text(fm, body), active, threshold=0.3)
    return {"failed": failed, "passes": not failed,
            "nearest_active": {"slug": near.a, "similarity": round(near.similarity, 3)} if near else None}


def _summary(slug: str, fm: dict, body: str, active) -> dict:
    row = {"slug": slug, "status": str(fm.get("status", "")), "description": str(fm.get("description", ""))[:300],
           "domain": str(fm.get("domain", "")), "importance": fm.get("importance"),
           "drafted_by_model": isinstance(fm.get("llm_draft"), dict),
           "source_traces": len(fm.get("source_traces") or []), "revises": fm.get("revises") or None}
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
        1 for _slug, fm, _body in _lessons(root)
        if (not status or str(fm.get("status")) == status) and _scope_allowed(fm, scope)
    )


def list_lessons(root: str, status: str | None = None, limit: int | None = None,
                 offset: int = 0, scope: str = "") -> list[dict]:
    if status is not None and status not in STATUSES:
        raise WorkbenchError(400, "bad_request", f"status must be one of {', '.join(STATUSES)}")
    parsed_limit, parsed_offset = _parse_pagination(limit, offset)
    rows = _lessons(root)
    scoped_rows = [row for row in rows if _scope_allowed(row[1], scope)]
    # File reads happen once here; the active set is computed once and reused per item.
    active_all = _active_texts_once(scoped_rows)
    filtered = [(slug, fm, body) for slug, fm, body in scoped_rows
                if not status or str(fm.get("status")) == status]
    page = filtered if parsed_limit is None and not parsed_offset else filtered[
        parsed_offset:None if parsed_limit is None else parsed_offset + parsed_limit]
    out = []
    for slug, fm, body in page:
        if str(fm.get("status")) == "review":
            out.append(_summary(slug, fm, body, active_all))
        else:
            out.append(_summary(slug, fm, body, []))
    return out


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


def edit(root: str, slug: str, fields: dict, actor: str, scope: str = "") -> dict:
    (name, fm, body), _rows = _find(root, slug, scope)
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be edited here")
    allowed = {"rule", "applies_when", "do_not_apply_when", "description"}
    unknown = set(fields) - allowed
    if unknown or not fields:
        raise WorkbenchError(400, "bad_request", f"give one or more of: {', '.join(sorted(allowed))}")
    if "description" in fields:
        fm["description"] = _clean(fields["description"], "description")
    if "applies_when" in fields:
        fm["applies_when"] = _clean(fields["applies_when"], "applies_when")
        body = _replace_section(body, "How to apply", fm["applies_when"])
    if "do_not_apply_when" in fields:
        fm["do_not_apply_when"] = _clean(fields["do_not_apply_when"], "do_not_apply_when")
        body = _replace_section(body, "Counter-examples", fm["do_not_apply_when"])
    if "rule" in fields:
        body = _replace_section(body, "Rule", _clean(fields["rule"], "rule"))
    path = lesson_io.lesson_path(root, name)
    with frontmatter.locked(path):
        lesson_io.write_lesson(path, fm, body, root=root, actor=actor, reason="edited in the console")
    return detail(root, name, scope)


def _replace_section(body: str, name: str, text: str) -> str:
    pattern = re.compile(rf"(^##\s*{re.escape(name)}\s*\n)(.*?)(?=\n##\s|\Z)", re.S | re.M | re.I)
    if pattern.search(body):
        return pattern.sub(lambda m: m.group(1) + text + "\n", body, count=1)
    return body.rstrip("\n") + f"\n\n## {name}\n{text}\n"


def _run(fn, ns: argparse.Namespace) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = fn(ns)
    return code, err.getvalue().strip()


def approve(root: str, slug: str, rationale: str | None, actor: str, scope: str = "") -> dict:
    (name, fm, _body), _rows = _find(root, slug, scope)
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be approved")
    ns = argparse.Namespace(slug=name, dest=root, force=False, rationale=(rationale or "").strip() or None,
                            approver=actor, scope=scope)
    code, message = _run(lesson_cmd.run_approve, ns)
    if code != 0:
        raise WorkbenchError(409, "refused", message or "the approval gates refused this lesson")
    return {"slug": name, "status": "active"}


def reject(root: str, slug: str, reason: str, scope: str = "") -> dict:
    (name, fm, _body), _rows = _find(root, slug, scope)
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be rejected")
    ns = argparse.Namespace(slug=name, dest=root, reason=_clean(reason, "reason"))
    code, message = _run(lesson_cmd.run_reject, ns)
    if code != 0:
        raise WorkbenchError(409, "refused", message or "could not reject")
    return {"slug": name, "status": "archived"}
