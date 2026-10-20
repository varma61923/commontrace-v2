from __future__ import annotations

import pytest

from commontrace import frontier_protocol as fp
from commontrace import household, spatial

CONTEXT = {"tenant": "home-a", "site": "kitchen", "robot": "helper-1"}


def test_unknown_person_is_ephemeral_and_identity_resolver_cannot_return_biometrics(tmp_path):
    calls = []
    result = household.observe(str(tmp_path), {"object": "person"}, room_id="kitchen",
        identity_resolver=lambda _: [{"status": "unknown"}], persist=lambda *args: calls.append(args))
    assert result == {"persisted": False, "reason": "unknown-person-ephemeral"}
    assert calls == []
    with pytest.raises(ValueError, match="biometric"):
        household.observe(str(tmp_path), {}, room_id="kitchen",
            identity_resolver=lambda _: [{"person_id": "p1", "status": "recognized", "face_embedding": "secret"}],
            persist=lambda *args: calls.append(args))
    assert not (tmp_path / household.JOURNAL).exists()


def test_no_memory_room_blocks_scene_and_spatial_persistence(tmp_path):
    root = str(tmp_path)
    household.configure_room(root, "bedroom", no_memory=True, actor="operator:home")
    calls = []
    result = household.observe(root, {"object": "toy"}, room_id="bedroom",
        identity_resolver=lambda _: [], persist=lambda *args: calls.append(args))
    assert result["reason"] == "no-memory-room"
    assert calls == []
    delta = {"object_id": "chair", "class": "furniture", "pose": [0, 0, 0], "frame": "map",
             "robot_id": "helper-1", "timestamp": fp.now(), "confidence": 0.9, "room_id": "bedroom"}
    with pytest.raises(PermissionError, match="no-memory room"):
        spatial.update(root, delta, context=CONTEXT)
    assert not (tmp_path / spatial.JOURNAL).exists()


def test_proxy_consent_is_audited_and_every_person_must_consent(tmp_path):
    root = str(tmp_path)
    household.consent(root, "resident-1", "resident", granted=True, grantor="proxy-1",
                      proxy_for="resident-1", proxy_authority="capacity-review:case-42")
    household.consent(root, "guest-1", "guest", granted=True, grantor="guest-1")
    calls = []
    result = household.observe(root, {"scene": "shared meal"}, room_id="kitchen",
        identity_resolver=lambda _: [{"status": "recognized", "person_id": "resident-1"},
                                     {"status": "recognized", "person_id": "guest-1"}],
        persist=lambda scene, scopes, gate: calls.append((scene, scopes, gate)))
    assert result["persisted"] is True
    assert {row["scope"] for row in result["scopes"]} == {"resident", "guest"}
    consent_rows = fp.journal(root, household.JOURNAL)
    assert consent_rows[0]["proxy_authority"] == "capacity-review:case-42"
    assert calls[0][2]["record"]["scene_digest"]

    with pytest.raises(PermissionError, match="explicitly authorized"):
        household.consent(root, "resident-2", "resident", granted=True, grantor="stranger")


def test_person_spatial_data_requires_signed_household_gate(tmp_path):
    root = str(tmp_path)
    delta = {"object_id": "person-7", "class": "person", "pose": [0, 0, 0], "frame": "map",
             "robot_id": "helper-1", "timestamp": fp.now(), "confidence": 0.9, "contains_person": True}
    with pytest.raises(PermissionError, match="identity and consent"):
        spatial.update(root, delta, context=CONTEXT)
    assert not (tmp_path / spatial.JOURNAL).exists()


def test_robot_stack_gate_is_bound_to_scene_room_and_current_consent(tmp_path):
    root = str(tmp_path)
    household.consent(root, "resident-1", "resident", granted=True, grantor="resident-1")
    delta = {"object_id": "person-1", "class": "person", "pose": [0, 0, 0], "frame": "map",
             "robot_id": "helper-1", "timestamp": fp.now(), "confidence": 0.9}
    stored = []

    def persist(scene, scopes, gate):
        safe_delta = {**scene, "contains_person": True, "household_gate": gate}
        stored.append((spatial.update(root, safe_delta, context=CONTEXT), gate))

    result = household.observe(root, delta, room_id="kitchen",
        identity_resolver=lambda _: [{"status": "recognized", "person_id": "resident-1"}], persist=persist)
    assert result["persisted"] and len(stored) == 1
    household.consent(root, "resident-1", "resident", granted=False, grantor="resident-1")
    with pytest.raises(PermissionError, match="consent changed"):
        spatial.update(root, {**delta, "contains_person": True, "household_gate": stored[0][1]}, context=CONTEXT)
