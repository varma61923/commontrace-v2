from __future__ import annotations

import json

import pytest

from commontrace import frontier_protocol as fp
from commontrace import hierarchical, incident, privacy_erasure, receipts


def test_freeze_replay_and_erasure_hold(tmp_path):
    root = str(tmp_path)
    fact, _ = hierarchical.add_fact(root, "Arm is in bay A", valid_from="2026-01-01T00:00:00Z")
    receipts.record(root, receipts.Receipt("task", "2026-01-01T00:00:00Z", query="where is the arm"))
    frozen = incident.freeze(root, "event-1", fp.now())
    assert fact.id in frozen["held_ids"]
    before = incident.snapshot(root, "event-1")
    hierarchical.supersede_fact(root, fact.id, "Arm is in bay B", as_of=fp.now())
    assert incident.snapshot(root, "event-1") == before
    assert incident.timeline(root, "event-1")["retrievals"][0]["occasion_id"] == "task"
    with pytest.raises(PermissionError, match="legal hold"):
        privacy_erasure.purge_fact(root, fact.id, reason="subject request")
    bundle = incident.pack(root, "event-1")
    assert incident.verify_pack(root, bundle)
    bundle["files"]["evidence.json"]["snapshot"]["id"] = "forged"
    assert not incident.verify_pack(root, bundle)
    incident.release(root, "event-1", "investigation completed")
    assert not incident.timeline(root, "event-1")["held"]


def test_incident_surfaces_and_old_store(tmp_path):
    from commontrace.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call, cli

    root = str(tmp_path)
    out = cli("incident", "freeze", "event", "--valid-time", fp.now(), "--dest", root)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["id"] == "event"
    assert call(build_server(root), "incident_timeline", incident_id="event")["held"]
    out = cli("incident", "pack", "event", "--regime", "iso42001", "--dest", root)
    assert out.returncode == 0, out.stderr
    assert incident.verify_pack(root, json.loads(out.stdout))


@pytest.mark.parametrize("identity", ["../escape", "", "x" * 129])
def test_path_forgery_refused(tmp_path, identity):
    with pytest.raises(ValueError):
        incident.freeze(str(tmp_path), identity, fp.now())


def test_replay_returns_delivered_bytes_after_later_correction(tmp_path):
    pytest.importorskip("mcp")
    from commontrace.mcp_server import build_server
    from tests.test_mcp_server import _write_lesson, call

    root = str(tmp_path)
    path = _write_lesson(root, "arm-zone", body="Keep the arm inside bay A during calibration.")
    server = build_server(root)
    delivered = call(server, "retrieve", task="arm calibration bay", occasion_id="incident-task")
    assert [item["slug"] for item in delivered["lessons"]] == ["arm-zone"]
    valid_time = fp.now()
    from commontrace import frontmatter, lesson_io

    fm, _ = frontmatter.read(path)
    lesson_io.write_lesson(path, fm, "Keep the arm inside bay B during calibration.", root=root,
                           actor="test", reason="correction after the incident")
    incident.freeze(root, "arm-incident", valid_time)
    replayed = incident.replay(root, "arm-incident", "incident-task")
    assert [row["body"] for row in replayed["injected"]] == [delivered["lessons"][0]["body"]]


def test_large_store_freezes_and_tampered_part_is_refused(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson

    root = str(tmp_path)
    for i in range(12):  # 12 x 120 KB: well above the former single-record 1 MiB bound
        _write_lesson(root, f"manual-{i}", body=f"Cell {i} calibration manual. " + "step " * 24_000)
    old, _ = hierarchical.add_fact(root, "Gripper torque limit is 5 Nm", valid_from="2026-01-01T00:00:00Z")
    frozen = incident.freeze(root, "large", fp.now())
    assert len(frozen["held_ids"]) == 13
    hierarchical.supersede_fact(root, old.id, "Gripper torque limit is 4 Nm", as_of=fp.now())
    row = next(r for r in incident.timeline(root, "large")["corrections"] if r["id"] == old.id)
    assert row["believed_at_incident"] and row["then"] == "Gripper torque limit is 5 Nm"
    assert row["current_status"] != "active"
    assert old.id in incident.held(root)["large"]
    part = tmp_path / ".commontrace" / "incidents" / "large" / "facts.json"
    if not part.exists():
        part = next(tmp_path.rglob("incidents/large/facts.json"))
    part.write_text(part.read_text().replace("Gripper torque limit is 5 Nm", "Gripper torque limit is 9 Nm"))
    with pytest.raises(ValueError, match="does not match"):
        incident.snapshot(root, "large")
