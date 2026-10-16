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
