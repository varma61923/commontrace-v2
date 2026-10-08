"""Browser decisions apply only to the authoritative revision the operator saw."""
from __future__ import annotations

import concurrent.futures
import os
import threading

import pytest

from commontrace import frontmatter, gateway, holdout_io, lesson_admission, lesson_io, paths, workbench
from commontrace.cli import main
from tests.test_workbench import TOKEN, _lesson, call


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    holdout_io.configure(str(tmp_path), rate=0.5, salt="review-revision")
    return str(tmp_path)


@pytest.fixture
def acting(root):
    return gateway.Gateway(root, token=TOKEN, allow_approval=True)


@pytest.mark.parametrize("action,fields", [
    ("edit", {"rule": "Use a stable request identifier."}),
    ("approve", {"rationale": "reviewed"}),
    ("reject", {"reason": "too broad"}),
])
def test_stale_browser_decisions_do_not_mutate_or_sign(root, acting, action, fields):
    _lesson(root, "lesson_good")
    revision = call(acting, "GET", "/v1/lesson?slug=lesson_good")[1]["revision"]
    path = lesson_io.lesson_path(root, "lesson_good")
    fm, body = frontmatter.read(path)
    body += "\nAdditional evidence not reviewed by the operator.\n"
    frontmatter.write(path, fm, body)
    before = open(path, encoding="utf-8").read()
    status, payload = call(acting, "POST", f"/v1/lesson/{action}",
                           {"slug": "lesson_good", "expected_revision": revision, **fields})
    assert status == 409 and payload["error"]["code"] == "stale_review"
    assert open(path, encoding="utf-8").read() == before
    assert not os.path.exists(os.path.join(paths.memory_dir(root), "lesson_admissions.db"))


def test_summary_and_detail_bind_complete_provenance_and_body(root, acting):
    _lesson(root, "lesson_good")
    detail = call(acting, "GET", "/v1/lesson?slug=lesson_good")[1]
    summary = call(acting, "GET", "/v1/lessons?status=review")[1]["lessons"][0]
    fm, body = frontmatter.read(lesson_io.lesson_path(root, "lesson_good"))
    assert detail["revision"] == summary["revision"] == lesson_admission.digest_of(fm, body)
    fm["llm_draft"] = {"model": "changed-model"}
    frontmatter.write(lesson_io.lesson_path(root, "lesson_good"), fm, body)
    assert call(acting, "GET", "/v1/lesson?slug=lesson_good")[1]["revision"] != detail["revision"]


def test_current_review_approval_issues_a_valid_receipt(root, acting):
    _lesson(root, "lesson_good")
    revision = call(acting, "GET", "/v1/lesson?slug=lesson_good")[1]["revision"]
    assert call(acting, "POST", "/v1/lesson/approve", {"slug": "lesson_good", "expected_revision": revision})[0] == 200
    path = lesson_io.lesson_path(root, "lesson_good")
    fm, body = frontmatter.read(path)
    assert lesson_admission.eligible(root, path, fm, body)


@pytest.mark.parametrize("revision", ["", "x" * 64, "A" * 64, "a" * 63, 1, {}, []])
@pytest.mark.parametrize("action", ["edit", "approve", "reject"])
def test_revision_schema_is_strict(root, acting, revision, action):
    _lesson(root, "lesson_good")
    fields = {"edit": {"rule": "changed"}, "approve": {}, "reject": {"reason": "broad"}}[action]
    assert call(acting, "POST", f"/v1/lesson/{action}",
                {"slug": "lesson_good", "expected_revision": revision, **fields})[0] == 400


def test_two_concurrent_edits_have_one_winner(root):
    _lesson(root, "lesson_good")
    revision = workbench.detail(root, "lesson_good")["revision"]
    barrier = threading.Barrier(2)

    def change(rule: str) -> str:
        barrier.wait(timeout=5)
        try:
            workbench.edit(root, "lesson_good", {"rule": rule}, "console", expected_revision=revision)
            return "committed"
        except workbench.WorkbenchError as exc:
            return exc.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(change, ["Use idempotency key A.", "Use idempotency key B."]))
    assert sorted(outcomes) == ["committed", "stale_review"]


@pytest.mark.parametrize("action", ["edit", "approve", "reject"])
@pytest.mark.parametrize("changed", ["scope", "status"])
def test_state_and_scope_are_rechecked_after_lookup(root, monkeypatch, action, changed):
    _lesson(root, "lesson_good")
    find = workbench._find
    path = lesson_io.lesson_path(root, "lesson_good")

    def raced_find(*args, **kwargs):
        result = find(*args, **kwargs)
        fm, body = frontmatter.read(path)
        fm["scopes" if changed == "scope" else "status"] = ["container:other"] if changed == "scope" else "active"
        frontmatter.write(path, fm, body)
        return result

    monkeypatch.setattr(workbench, "_find", raced_find)
    with pytest.raises(workbench.WorkbenchError) as caught:
        if action == "edit":
            workbench.edit(root, "lesson_good", {"rule": "new rule"}, "console", "container:one")
        elif action == "approve":
            workbench.approve(root, "lesson_good", None, "console", "container:one")
        else:
            workbench.reject(root, "lesson_good", "broad", "container:one")
    assert caught.value.code == ("not_found" if changed == "scope" else "not_in_review")
    fm, body = frontmatter.read(path)
    assert "new rule" not in body and "approval_receipt" not in fm


def test_legacy_edit_preserves_changes_made_before_lock(root, monkeypatch):
    _lesson(root, "lesson_good")
    find = workbench._find
    path = lesson_io.lesson_path(root, "lesson_good")

    def raced_find(*args, **kwargs):
        result = find(*args, **kwargs)
        fm, body = frontmatter.read(path)
        fm["description"] = "A concurrent description."
        frontmatter.write(path, fm, body + "\nConcurrent evidence.\n")
        return result

    monkeypatch.setattr(workbench, "_find", raced_find)
    workbench.edit(root, "lesson_good", {"rule": "Send a stable retry key."}, "console")
    fm, body = frontmatter.read(path)
    assert fm["description"] == "A concurrent description." and "Concurrent evidence." in body


def test_full_revision_does_not_authorize_an_unviewable_tail(root, acting):
    _lesson(root, "lesson_good")
    path = lesson_io.lesson_path(root, "lesson_good")
    fm, body = frontmatter.read(path)
    frontmatter.write(path, fm, body + "a" * workbench.MAX_BODY_CHARS)
    view = call(acting, "GET", "/v1/lesson?slug=lesson_good")[1]
    assert view["body_truncated"]
    status, payload = call(acting, "POST", "/v1/lesson/approve", {"slug": "lesson_good", "expected_revision": view["revision"]})
    assert status == 409 and payload["error"]["code"] == "review_truncated"


def test_renamed_metadata_and_symlink_sources_are_not_routable(root, acting, tmp_path):
    _lesson(root, "lesson_good")
    path = lesson_io.lesson_path(root, "lesson_good")
    fm, body = frontmatter.read(path)
    fm["name"] = "lesson_alias"
    frontmatter.write(path, fm, body)
    external = tmp_path / "outside.md"
    fm["name"] = "lesson_symlink"
    frontmatter.write(str(external), fm, body)
    os.symlink(external, os.path.join(paths.lessons_dir(root), "lesson_symlink.md"))
    assert call(acting, "GET", "/v1/lessons")[1]["lessons"] == []
    for slug in ("lesson_good", "lesson_alias", "lesson_symlink"):
        assert call(acting, "GET", f"/v1/lesson?slug={slug}")[0] == 404
