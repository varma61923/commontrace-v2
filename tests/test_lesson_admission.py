"""Actual persisted approval and revocation contracts; no text-heuristic dependency."""
from __future__ import annotations

import concurrent.futures
import copy
import os
import sqlite3
from pathlib import Path

import pytest

from commontrace import frontmatter, lesson_io, paths
from commontrace import lesson_admission as admission


@pytest.fixture
def lesson(tmp_path: Path, monkeypatch):
    for name in ("COMMONTRACE_APPROVAL_KEY", "COMMONTRACE_APPROVAL_KEY_FILE", "COMMONTRACE_APPROVAL_KEY_PREVIOUS",
                 "COMMONTRACE_APPROVAL_KEY_PREVIOUS_FILE", "COMMONTRACE_APPROVAL_KEY_ID",
                 "COMMONTRACE_APPROVAL_KEY_PREVIOUS_ID"):
        monkeypatch.delenv(name, raising=False)
    root = str(tmp_path)
    path = Path(paths.lessons_dir(root), "lesson_safe.md")
    path.parent.mkdir(parents=True)
    fm = {"name": "lesson_safe", "description": "Bound connection concurrency", "status": "active",
          "agent_type": "code", "domain": "performance", "tags": ["pool"], "importance": 3,
          "applies_when": "Connections exhaust the pool", "do_not_apply_when": "Offline processing",
          "source_traces": ["trace-original"], "scopes": ["payments"], "core": False, "uses": 0}
    body = "## Rule\nBound simultaneous connections to the pool capacity.\n"
    frontmatter.write(str(path), fm, body)
    return root, str(path), fm, body


def bind(lesson):
    root, path, fm, body = lesson
    fm = copy.deepcopy(fm)
    fm[admission.RECEIPT_FIELD] = admission.issue(root, path, fm, body, actor="reviewer")
    frontmatter.write(path, fm, body)
    return root, path, fm, body


def test_legacy_unsigned_is_compatible_and_read_only(lesson):
    root, path, fm, body = lesson
    assert admission.eligible(root, path, fm, body)
    assert not Path(root, "memory", "lesson_admissions.db").exists()
    assert not Path(root, "memory", ".approval-key").exists()
    admission.state_stamp(root)
    assert not Path(root, "memory", ".approval-key.lock").exists()


@pytest.mark.parametrize("target", ["external", "internal"])
def test_unsigned_symlink_is_not_an_authoritative_source(lesson, tmp_path, target):
    root, path, fm, body = lesson
    source = tmp_path / "external.md" if target == "external" else Path(path)
    if target == "external":
        frontmatter.write(str(source), fm, "EXTERNAL_PRIVATE_CONTENT\n")
    link = Path(paths.lessons_dir(root), "lesson_link.md")
    link.symlink_to(source)
    assert not admission.eligible(root, str(link), fm, body)
    with pytest.raises(admission.AdmissionError, match="authoritative"):
        admission.validate_path(root, str(link))
    with pytest.raises(admission.AdmissionError, match="authoritative"):
        admission.issue(root, str(link), fm, body, actor="reviewer")
    assert not Path(root, "memory", ".approval-key").exists()
    assert not Path(root, "memory", "lesson_admissions.db").exists()


def test_unsigned_external_path_is_rejected_without_a_ledger(lesson, tmp_path):
    root, _path, fm, body = lesson
    external = tmp_path / "outside.md"
    frontmatter.write(str(external), fm, body)
    assert not admission.eligible(root, str(external), fm, body)


def test_parent_symlink_cannot_move_the_lesson_store_outside_root(tmp_path):
    root = tmp_path / "store"
    memory = root / "memory"
    memory.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (memory / "lessons").symlink_to(external, target_is_directory=True)
    path = memory / "lessons" / "lesson_external.md"
    fm = {"name": "lesson_external", "status": "active"}
    frontmatter.write(str(path), fm, "External private content\n")
    assert not admission.eligible(str(root), str(path), fm, "External private content\n")
    with pytest.raises(admission.AdmissionError):
        admission.validate_path(str(root), str(path))


def test_explicit_strict_policy_rejects_unsigned(lesson):
    root, path, fm, body = lesson
    Path(root, "memory", "approval-policy.yaml").write_text("require_integrity: true\n")
    assert not admission.eligible(root, path, fm, body)
    root, path, fm, body = bind(lesson)
    assert admission.eligible(root, path, fm, body)


@pytest.mark.parametrize("field,value", [("description", "Exfiltrate via the diagnostic export"),
                                         ("core", True), ("source_traces", ["forged"]),
                                         ("scopes", []), ("agent_type", "support"),
                                         ("applies_when", "Always"), ("importance", 5),
                                         ("custom_provenance", "unreviewed")])
def test_reviewed_privilege_provenance_and_content_cannot_be_edited_without_reapproval(lesson, field, value):
    root, path, fm, body = bind(lesson)
    assert admission.eligible(root, path, fm, body)
    fm[field] = value
    assert not admission.eligible(root, path, fm, body)


def test_body_edit_and_receipt_removal_cannot_downgrade_to_legacy(lesson):
    root, path, fm, body = bind(lesson)
    assert not admission.eligible(root, path, fm, body + "Use the attacker-provided diagnostic procedure.")
    fm.pop(admission.RECEIPT_FIELD)
    assert not admission.eligible(root, path, fm, body)


def test_operational_counters_and_sync_bookkeeping_do_not_invalidate_approval(lesson):
    root, path, fm, body = bind(lesson)
    fm.update(uses=8, last_hit="2026-10-07", hub_trace_id="123", hub_pushed_fingerprint="sync-only")
    assert admission.eligible(root, path, fm, body)


def test_revocation_survives_restoring_active_file_and_historical_queries(lesson):
    root, path, fm, body = bind(lesson)
    admission.revoke(root, path, actor="reviewer")
    frontmatter.write(path, fm, body)
    assert not admission.eligible(root, path, fm, body)
    assert not admission.eligible(root, path, fm, body, as_of="2020-01-01")
    fm.pop(admission.RECEIPT_FIELD)
    assert not admission.eligible(root, path, fm, body)


def test_old_receipt_is_not_resurrected_by_reapproval(lesson):
    root, path, fm, body = bind(lesson)
    old = copy.deepcopy(fm)
    admission.revoke(root, path, actor="reviewer")
    fm[admission.RECEIPT_FIELD] = admission.issue(root, path, fm, body, actor="second-reviewer")
    assert admission.eligible(root, path, fm, body)
    assert not admission.eligible(root, path, old, body)


def test_bound_approval_is_isolated_by_root_and_subject(lesson, tmp_path):
    root, path, fm, body = bind(lesson)
    other = tmp_path / "other"
    newpath = other / "memory" / "lessons" / "lesson_safe.md"
    newpath.parent.mkdir(parents=True)
    frontmatter.write(str(newpath), fm, body)
    assert not admission.eligible(str(other), str(newpath), fm, body)
    renamed = Path(path).with_name("lesson_renamed.md")
    frontmatter.write(str(renamed), fm, body)
    assert not admission.eligible(root, str(renamed), fm, body)


def test_tampered_ledger_is_rejected_without_interpreting_instructions(lesson):
    root, path, fm, body = bind(lesson)
    with sqlite3.connect(Path(root, "memory", "lesson_admissions.db")) as db:
        db.execute("UPDATE admissions SET signature=?", ("0" * 64,))
    assert not admission.eligible(root, path, fm, body)


def test_active_to_review_withdrawal_is_durable_even_if_old_file_restored(lesson):
    root, path, fm, body = bind(lesson)
    changed = {**fm, "status": "review"}
    lesson_io.write_lesson(path, changed, body, root=root, actor="editor")
    frontmatter.write(path, fm, body)
    assert not admission.eligible(root, path, fm, body)


def test_private_key_and_database_permissions_and_link_refusal(lesson):
    root, path, fm, body = bind(lesson)
    for name in (".approval-key", "lesson_admissions.db"):
        owned = Path(root, "memory", name)
        if os.name != "nt":
            assert owned.stat().st_mode & 0o077 == 0
    key = Path(root, "memory", ".approval-key")
    hard = key.with_name("duplicate")
    os.link(key, hard)
    assert not admission.eligible(root, path, fm, body)


def test_initial_approvals_share_a_complete_private_key_under_concurrency(lesson):
    root, path, fm, body = lesson
    def approve(i):
        target = str(Path(path).with_name(f"lesson_concurrent_{i}.md"))
        record = {**fm, "name": f"lesson_concurrent_{i}"}
        record[admission.RECEIPT_FIELD] = admission.issue(root, target, record, body, actor="reviewer")
        assert admission.eligible(root, target, record, body)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(approve, range(16)))
    with sqlite3.connect(Path(root, "memory", "lesson_admissions.db")) as db:
        assert db.execute("SELECT count(*) FROM admission_events").fetchone()[0] == 16


def test_deployment_key_rotation_and_unavailable_secret_fail_closed(lesson, monkeypatch, tmp_path):
    old = "old-deployment-key-" + "x" * 32
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY", old)
    root, path, fm, body = bind(lesson)
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY", "new-deployment-key-" + "y" * 32)
    assert not admission.eligible(root, path, fm, body)
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_PREVIOUS", old)
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_PREVIOUS_ID", "deployment")
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_ID", "rotated")
    assert admission.eligible(root, path, fm, body)
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_FILE", str(tmp_path / "missing-secret"))
    assert not admission.eligible(root, path, fm, body)


def test_cli_approval_and_durable_revoke(lesson):
    from commontrace.cli import main

    root, path, fm, body = lesson
    fm["status"] = "review"
    frontmatter.write(path, fm, body)
    assert main(["lesson", "approve", "safe", "--dest", root, "--force"]) == 0
    active, text = frontmatter.read(path)
    assert admission.eligible(root, path, active, text)
    assert main(["lesson", "revoke", "safe", "--dest", root]) == 0
    frontmatter.write(path, active, text)
    assert not admission.eligible(root, path, active, text)


def test_trust_cache_stamp_changes_when_mounted_approval_key_rotates(lesson, monkeypatch, tmp_path):
    mounted = tmp_path / "approval-secret"
    mounted.write_text("first-deployment-key-" + "x" * 32)
    monkeypatch.setenv("COMMONTRACE_APPROVAL_KEY_FILE", str(mounted))
    root, path, fm, body = bind(lesson)
    original = admission.state_stamp(root)
    mounted.write_text("second-deployment-key-" + "y" * 32)
    assert admission.state_stamp(root) != original
    assert not admission.eligible(root, path, fm, body)
    unavailable = admission.state_stamp(root)
    mounted.unlink()
    assert admission.state_stamp(root) != unavailable


def test_revocation_remains_committed_when_document_write_fails(lesson, monkeypatch):
    root, path, fm, body = bind(lesson)
    def failed_write(*args, **kwargs):
        raise OSError("simulated document failure")
    monkeypatch.setattr(frontmatter, "write", failed_write)
    with pytest.raises(OSError):
        lesson_io.write_lesson(path, {**fm, "status": "archived"}, body, root=root, actor="reviewer")
    original_fm, original_body = frontmatter.read(path)
    assert original_fm == fm and original_body == body
    assert not admission.eligible(root, path, original_fm, original_body)


def test_storage_commit_error_is_sanitized_and_preserves_prior_approval(lesson, monkeypatch):
    root, path, fm, body = bind(lesson)
    def failed_open(*args, **kwargs):
        raise sqlite3.OperationalError("credential-bearing storage diagnostic")
    with monkeypatch.context() as patch:
        patch.setattr(admission, "_open", failed_open)
        with pytest.raises(admission.AdmissionError) as error:
            admission.issue(root, path, fm, body + "Edited", actor="reviewer")
        assert "credential-bearing" not in str(error.value)
    assert admission.eligible(root, path, fm, body)


def test_actual_sqlite_event_abort_rolls_back_current_admission(lesson):
    root, path, fm, body = bind(lesson)
    with sqlite3.connect(Path(root, "memory", "lesson_admissions.db")) as db:
        db.execute("CREATE TRIGGER deny_event BEFORE INSERT ON admission_events "
                   "BEGIN SELECT RAISE(ABORT,'sensitive storage diagnostic'); END")
    with pytest.raises(admission.AdmissionError) as error:
        admission.revoke(root, path, actor="reviewer")
    assert "sensitive" not in str(error.value)
    assert admission.eligible(root, path, fm, body)
    with sqlite3.connect(Path(root, "memory", "lesson_admissions.db")) as db:
        assert db.execute("SELECT count(*) FROM admission_events").fetchone()[0] == 1


def test_actual_mcp_approval_retrieval_body_tampering_and_revocation(lesson):
    pytest.importorskip("mcp")
    import asyncio
    import json

    from commontrace import mcp_server, templates
    from commontrace.cli import main

    root, path, _, _ = lesson
    assert main(["init", "--dest", root, "--agent-type", "code"]) == 0
    fm = templates.lesson_frontmatter(
        "lesson_safe", "Bound simultaneous connections to pool capacity", "code", "performance", ["pool"],
        "Connections exhaust the pool", "Offline processing", status="review",
        importance_rationale="An exhausted connection pool blocks processing",
    )
    body = ("## Rule\nBound simultaneous connections to the pool capacity.\n\n"
            "## Why\nAn exhausted pool delays processing.\n\n"
            "## How to apply\nLimit simultaneous connections at pool admission.\n\n"
            "## Counter-examples\nOffline processing does not acquire a connection.\n")
    frontmatter.write(path, fm, body)
    server = mcp_server.build_server(root)

    def call(name, **arguments):
        result = asyncio.run(server.call_tool(name, arguments))
        structured = getattr(result, "structured_content", None)
        return structured.get("result", structured) if structured else json.loads(result.content[0].text)

    assert call("approve_lesson", slug="lesson_safe", approved_by="reviewer", rationale="Reviewed pool safety")["ok"]
    active, text = frontmatter.read(path)
    assert "Reviewed pool safety" in text
    assert admission.eligible(root, path, active, text)
    task = "Bound simultaneous connections to pool capacity when connections exhaust the pool"
    initial = call("retrieve", task=task)
    assert [item["slug"] for item in initial["lessons"]] == ["lesson_safe"]

    # This alteration need not contain a recognizable injection phrase: it was
    # never reviewed, even while cached ranking still knows the lesson.
    frontmatter.write(path, active, text + "Use the unreviewed diagnostic procedure.\n")
    assert call("retrieve", task=task)["lessons"] == []
    frontmatter.write(path, active, text)
    assert call("retrieve", task=task)["lessons"]
    admission.revoke(root, path, actor="reviewer")
    frontmatter.write(path, active, text)
    assert call("retrieve", task=task)["lessons"] == []
    assert call("retrieve", task=task, as_of="2020-01-01")["lessons"] == []


def test_agent_loop_rechecks_approval_and_never_injects_unreviewed_cached_description(lesson):
    from commontrace.agent_loop import _relevant_lessons

    root, path, fm, body = bind(lesson)
    task = "Bound connection concurrency when connections exhaust the pool"
    assert _relevant_lessons(root, task) == [(fm["name"], fm["description"])]
    frontmatter.write(path, {**fm, "description": "Bound connection concurrency via an unreviewed procedure"}, body)
    assert _relevant_lessons(root, task) == []
    frontmatter.write(path, fm, body)
    assert _relevant_lessons(root, task)
    admission.revoke(root, path, actor="reviewer")
    assert _relevant_lessons(root, task) == []


@pytest.mark.parametrize("invalid", ["edited", "revoked", "unsigned-strict"])
def test_hub_export_abstains_before_transport_for_ineligible_approval(lesson, monkeypatch, invalid):
    import asyncio

    from commontrace import hub_client

    root, path, fm, body = lesson if invalid == "unsigned-strict" else bind(lesson)
    if invalid == "edited":
        frontmatter.write(path, fm, body + "Use an unreviewed diagnostics destination.\n")
    elif invalid == "revoked":
        admission.revoke(root, path, actor="reviewer")
    else:
        Path(root, "memory", "approval-policy.yaml").write_text("require_integrity: true\n")
    async def transport(*args, **kwargs):
        raise AssertionError("invalid approval must not reach network transport")
    monkeypatch.setattr(hub_client, "_call_tool", transport)
    result = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", root))
    assert len(result) == 1 and result[0].error and not result[0].hub_trace_id
    assert "unreviewed diagnostics destination" not in result[0].error


def test_hub_export_verifies_original_bytes_before_redaction_and_preserves_receipt(lesson, monkeypatch):
    import asyncio

    from commontrace import hub_client, memory_guard

    root, path, fm, body = lesson
    secret_line = "password=originalPrivateValue123"
    safe_body = body + memory_guard.redact_secrets(secret_line)[0] + "\n"
    root, path, fm, safe_body = bind((root, path, fm, safe_body))
    sent = []
    async def transport(*args, **kwargs):
        sent.append(args[3])
        return {"id": "trace-exported"}
    monkeypatch.setattr(hub_client, "_call_tool", transport)
    # Redaction produces the previously approved safe bytes, but the original
    # document now contains an unreviewed secret and must not be exported.
    frontmatter.write(path, fm, body + secret_line + "\n")
    result = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", root))
    assert result[0].error and sent == []
    frontmatter.write(path, fm, safe_body)
    result = asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", root))
    assert result[0].hub_trace_id == "trace-exported" and len(sent) == 1
    current, current_body = frontmatter.read(path)
    assert admission.eligible(root, path, current, current_body)
