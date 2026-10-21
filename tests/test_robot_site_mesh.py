from __future__ import annotations

import itertools

import pytest

from unimem import frontier_protocol as fp
from unimem import memory_authority, mesh_crdt
from unimem.robotics import site_memory, vda5050


def keypair():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    private = crypto.Ed25519PrivateKey.generate()
    private_hex = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption()).hex()
    public_hex = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                   serialization.PublicFormat.Raw).hex()
    return private_hex, public_hex


def test_vda5050_subset_ingest_scene_merge_and_review_only_site_rules(tmp_path):
    root = str(tmp_path)
    message = {"headerId": 1, "timestamp": "2026-06-20T14:05:00Z", "version": "3.0.0",
        "manufacturer": "robot-vendor", "serialNumber": "r-1", "orderId": "order-7", "orderUpdateId": 2,
        "nodeStates": [{"nodeId": "dock", "sequenceId": 0, "nodePosition": {"x": 1, "y": 2, "mapId": "map-1"}}],
        "edgeStates": [{"edgeId": "lane-3", "sequenceId": 1, "startNodeId": "dock", "endNodeId": "shelf"}],
        "zoneIds": ["zone-a"]}
    normalized = vda5050.validate_state(message)
    row = site_memory.ingest_state(root, normalized, site="depot", robot="robot-1", vendor="vda5050-3.0.0")
    assert row["valid_from"] == fp.moment(message["timestamp"])
    assert site_memory.scene_graph(root, site="depot")["layers"]["dock"][0]["robot"] == "robot-1"

    observations = [{"timestamp": f"2026-06-20T14:{minute:02d}:00Z", "zone": "zone-a", "congested": minute < 16}
                    for minute in range(20)]
    rules = site_memory.rules(root, site="depot", observations=observations, minimum_observations=10)
    assert rules[0]["hour"] == 14 and rules[0]["interval"][0] <= rules[0]["congested_rate"]
    suggestion = site_memory.suggest(root, site="depot", ruleset=rules)
    assert suggestion["status"] == "review-required"
    with pytest.raises(ValueError, match="version 3.0.0"):
        vda5050.validate_state({**message, "version": "2.0.0"})


def test_vda_order_path_and_zone_data_are_ingested_as_bounded_path_event(tmp_path):
    message = {"headerId": 2, "timestamp": "2026-06-20T14:05:00Z", "version": "3.0.0",
        "manufacturer": "robot-vendor", "serialNumber": "r-1", "orderId": "order-8", "orderUpdateId": 3,
        "nodes": [{"nodeId": "dock", "sequenceId": 0, "released": True},
                  {"nodeId": "shelf", "sequenceId": 2, "released": False}],
        "edges": [{"edgeId": "lane-3", "sequenceId": 1, "startNodeId": "dock", "endNodeId": "shelf", "released": True}],
        "zoneIds": ["zone-a"], "pathSharing": ["lane-3"]}
    normalized = vda5050.validate_order(message)
    assert normalized["zones"] == ["zone-a"]
    assert normalized["path_sharing"] == ["lane-3"]
    row = site_memory.ingest_vda_order(str(tmp_path), message, site="depot", robot="robot-1")
    assert row["kind"] == "path" and row["state"]["path_sharing"] == ["lane-3"]
    with pytest.raises(ValueError, match="released flag"):
        vda5050.validate_order({**message, "nodes": [{**message["nodes"][0], "released": "true"}]})
    with pytest.raises(ValueError, match="same message"):
        vda5050.validate_order({**message, "pathSharing": ["unknown-edge"]})


def test_fleet_site_ingest_dispatches_vda_order_path(tmp_path, capsys):
    import json
    from argparse import Namespace

    from unimem.commands.governance import fleet_run_site

    message = {"headerId": 1, "timestamp": "2026-06-20T14:05:00Z",
        "version": "3.0.0", "manufacturer": "robot-vendor", "serialNumber": "r-1",
        "orderId": "order-9", "orderUpdateId": 1,
        "nodes": [{"nodeId": "dock", "sequenceId": 0, "released": True}], "edges": []}
    request = {"message_type": "order", "message": message, "site": "depot", "robot": "robot-1"}
    status = fleet_run_site(Namespace(dest=str(tmp_path), site_action="ingest", request=json.dumps(request)))
    row = json.loads(capsys.readouterr().out)
    assert status == 0 and row["kind"] == "path" and row["site"] == "depot"


def test_vda_zone_event_accepts_only_bounded_zone_envelope(tmp_path):
    message = {"headerId": 3, "timestamp": "2026-06-20T14:05:00Z", "version": "3.0.0",
        "manufacturer": "robot-vendor", "serialNumber": "r-1", "zoneIds": ["zone-a", "zone-b"]}
    row = site_memory.ingest_vda_zone(str(tmp_path), message, site="depot", robot="robot-1")
    assert row["kind"] == "zone" and row["state"]["zones"] == ["zone-a", "zone-b"]
    assert row["state"]["nodes"] == [] and row["state"]["edges"] == []
    with pytest.raises(ValueError, match="zone IDs"):
        vda5050.validate_zone({key: value for key, value in message.items() if key != "zoneIds"})


def test_open_rmf_adapter_contract_ingests_bounded_bitemporal_state(tmp_path):
    class FixtureAdapter:
        def to_unimem_state(self, message):
            assert message == {"fixture": "rmf-state"}
            return {"timestamp": "2026-06-20T14:05:00Z",
                    "nodes": [{"node_id": "dock", "position": {"x": 1, "y": 2}}],
                    "edges": [{"edge_id": "lane-3", "from": "dock", "to": "shelf"}],
                    "zones": ["zone-a", "zone-a"]}

    root = str(tmp_path)
    row = site_memory.ingest_open_rmf(root, FixtureAdapter(), {"fixture": "rmf-state"},
                                      site="depot", robot="robot-1", scopes=["site:depot"])
    assert row["vendor"] == "open-rmf-adapter"
    assert row["valid_from"] == fp.moment("2026-06-20T14:05:00Z")
    assert row["scopes"] == ["site:depot"]
    assert row["state"]["zones"] == ["zone-a", "zone-a"]
    layers = site_memory.scene_graph(root, site="depot", context=["site:depot"])["layers"]
    assert layers["dock"][0]["vendor"] == "open-rmf-adapter"


@pytest.mark.parametrize("invalid_state", [
    None,
    {"timestamp": "not-a-time", "nodes": [], "edges": [], "zones": []},
    {"timestamp": "2026-06-20T14:05:00Z", "nodes": [{"id": ""}], "edges": [], "zones": []},
    {"timestamp": "2026-06-20T14:05:00Z", "nodes": [], "edges": [], "zones": [None]},
])
def test_open_rmf_adapter_rejects_invalid_normalized_state_without_writing(tmp_path, invalid_state):
    class FixtureAdapter:
        def to_unimem_state(self, _message):
            return invalid_state

    root = str(tmp_path)
    with pytest.raises(ValueError):
        site_memory.ingest_open_rmf(root, FixtureAdapter(), {}, site="depot", robot="robot-1")
    assert fp.journal(root, site_memory.JOURNAL) == []


def test_open_rmf_adapter_rejects_oversized_mapping_before_writing(tmp_path):
    class FixtureAdapter:
        def to_unimem_state(self, _message):
            return {"timestamp": "2026-06-20T14:05:00Z",
                    "nodes": [{"id": f"node-{index}"} for index in range(4097)],
                    "edges": [], "zones": []}

    root = str(tmp_path)
    with pytest.raises(ValueError, match="site nodes"):
        site_memory.ingest_open_rmf(root, FixtureAdapter(), {}, site="depot", robot="robot-1")
    assert fp.journal(root, site_memory.JOURNAL) == []


def test_signed_op_crdt_converges_conflicts_are_visible_and_safety_is_irremovable(tmp_path):
    root = str(tmp_path)
    private_a, public_a = keypair()
    private_b, public_b = keypair()
    with memory_authority.writer("site-admin", "operator"):
        mesh_crdt.register_robot(root, "robot-a", public_a)
        mesh_crdt.register_robot(root, "robot-b", public_b)
    safety = mesh_crdt.sign_operation("robot-a", private_a, counter=1, operation="add",
        entity="safety:hot-zone", value={"zone": "kiln"}, safety=True)
    normal = mesh_crdt.sign_operation("robot-a", private_a, counter=2, operation="add",
        entity="route:west", value={"path": [1, 2]})
    remove = mesh_crdt.sign_operation("robot-b", private_b, counter=1, operation="remove",
        entity="safety:hot-zone", targets=[safety["body"]["id"]])
    put_a = mesh_crdt.sign_operation("robot-a", private_a, counter=3, operation="put",
        entity="zone-a", field="congestion", value="high")
    put_b = mesh_crdt.sign_operation("robot-b", private_b, counter=2, operation="put",
        entity="zone-a", field="congestion", value="low")
    operations = [safety["body"], normal["body"], remove["body"], put_a["body"], put_b["body"]]
    expected = mesh_crdt.merge_views(operations)
    assert expected["entities"]["safety:hot-zone"]
    assert expected["contradictions"][0]["field"] == "congestion"
    for order in itertools.permutations(operations):
        assert mesh_crdt.merge_views(list(order)) == expected


def test_forged_unknown_and_revoked_robot_operations_are_rejected_and_tainted(tmp_path):
    root = str(tmp_path)
    private_a, public_a = keypair()
    private_b, public_b = keypair()
    with memory_authority.writer("site-admin", "operator"):
        mesh_crdt.register_robot(root, "robot-a", public_a)
        mesh_crdt.register_robot(root, "robot-b", public_b)
    valid = mesh_crdt.sign_operation("robot-a", private_a, counter=1, operation="add", entity="zone", value="clear")
    mesh_crdt.apply(root, [valid])
    revoked_op = mesh_crdt.sign_operation("robot-b", private_b, counter=1, operation="put",
                                          entity="zone", field="state", value="blocked")
    with memory_authority.writer("site-admin", "operator"):
        mesh_crdt.revoke_robot(root, "robot-b", reason="robot retired")
    with pytest.raises(PermissionError, match="revoked robot"):
        mesh_crdt.apply(root, [revoked_op])
    result = mesh_crdt.merge(root)
    try:
        import automerge  # noqa: F401
    except ImportError:
        assert result["automerge_used"] is False
    else:
        assert result["automerge_used"] is True
    assert all(row["robot_id"] != "robot-b" for row in result["entities"].get("zone", []))
    assert any(row["robot_id"] == "robot-b" for row in fp.journal(root, mesh_crdt.TAINTS))

    unknown_private, unknown_public = keypair()
    unknown = mesh_crdt.sign_operation("robot-unknown", unknown_private, counter=1,
                                        operation="add", entity="x", value=1)
    with pytest.raises(PermissionError, match="key is unknown"):
        mesh_crdt.apply(root, [unknown])
    assert any(row["reason"] == "unknown or mismatched robot key" for row in fp.journal(root, mesh_crdt.TAINTS))
