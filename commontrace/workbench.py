"""The lesson workbench behind the console: what is waiting for review, why a draft would or would not pass, and
the actions that act on it.

Reading is always available through the gateway's token. ACTING (edit, approve, reject) is a separate switch the
operator turns on (`commontrace gateway --allow-approval`), because approving a lesson changes what every agent
is told, and a console left open on a robot's screen should not be able to do that by default. Every action goes
through the same code the command line uses, so the same gates apply: unedited scaffolding, a high-confidence
secret or injection finding, a restatement of an active lesson, and the store's separation-of-duties policy. A
draft edited here is still a `status: review` draft; only `approve` activates it, and `force` is not offered.
"""
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


def _active_texts(rows, exclude: str) -> list[tuple[str, str]]:
    return [(slug, redundancy.comparable_text(fm, body)) for slug, fm, body in rows
            if str(fm.get("status")) == "active" and slug != exclude]


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


def list_lessons(root: str, status: str | None = None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise WorkbenchError(400, "bad_request", f"status must be one of {', '.join(STATUSES)}")
    rows = _lessons(root)
    out = []
    for slug, fm, body in rows:
        if status and str(fm.get("status")) != status:
            continue
        out.append(_summary(slug, fm, body, _active_texts(rows, slug) if fm.get("status") == "review" else []))
    return out


def _find(root: str, slug: str):
    if not isinstance(slug, str) or not lesson_io.SLUG_RE.match(slug):
        raise WorkbenchError(400, "bad_request", "slug must be letters, digits, '_' or '-'")
    want = lesson_io.canonical_slug(slug)
    rows = _lessons(root)
    for row in rows:
        if lesson_io.canonical_slug(row[0]) == want:
            return row, rows
    raise WorkbenchError(404, "not_found", f"no lesson {slug!r}")


def _section(body: str, name: str) -> str:
    m = re.search(rf"^##\s*{re.escape(name)}\s*\n(.*?)(?=\n##\s|\Z)", body, re.S | re.M | re.I)
    return m.group(1).strip() if m else ""


def detail(root: str, slug: str) -> dict:
    (name, fm, body), rows = _find(root, slug)
    out = _summary(name, fm, body, _active_texts(rows, name) if fm.get("status") == "review" else [])
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


def edit(root: str, slug: str, fields: dict, actor: str) -> dict:
    """Change a REVIEW lesson's rule or conditions. Anything else is refused: an active lesson is changed by a
    new draft that is approved, so the experiment sees a new revision rather than a silent rewrite."""
    (name, fm, body), _rows = _find(root, slug)
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be edited here")
    allowed = {"rule", "applies_when", "do_not_apply_when"}
    unknown = set(fields) - allowed
    if unknown or not fields:
        raise WorkbenchError(400, "bad_request", f"give one or more of: {', '.join(sorted(allowed))}")
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
    return detail(root, name)


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


def approve(root: str, slug: str, rationale: str | None, actor: str) -> dict:
    (name, fm, _body), _rows = _find(root, slug)
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be approved")
    ns = argparse.Namespace(slug=name, dest=root, force=False, rationale=(rationale or "").strip() or None,
                            approver=actor)
    code, message = _run(lesson_cmd.run_approve, ns)
    if code != 0:
        raise WorkbenchError(409, "refused", message or "the approval gates refused this lesson")
    return {"slug": name, "status": "active"}


def reject(root: str, slug: str, reason: str) -> dict:
    (name, fm, _body), _rows = _find(root, slug)
    if fm.get("status") != "review":
        raise WorkbenchError(409, "not_in_review", "only a lesson in review can be rejected")
    ns = argparse.Namespace(slug=name, dest=root, reason=_clean(reason, "reason"))
    code, message = _run(lesson_cmd.run_reject, ns)
    if code != 0:
        raise WorkbenchError(409, "refused", message or "could not reject")
    return {"slug": name, "status": "archived"}
