"""The public learning API uses canonical sources and authenticated scope."""

import json

import pytest

from unimem import agent_registry, learning
from unimem.gateway import Gateway
from unimem.learning_contract import SCHEMA
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord

TOKEN = "t" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN, "Host": "localhost"}


def call(gateway, operation, body=None, *, headers=None, query=""):
    method = "GET" if operation in ("capabilities", "status") else "POST"
    response = gateway.handle(
        method,
        "/v1/learning/" + operation + query,
        HEADERS if headers is None else headers,
        json.dumps(body).encode() if body is not None else None,
    )
    return response.status, json.loads(response.body)


def test_learning_roundtrip_and_conflicting_retries(tmp_path):
    root = str(tmp_path)
    Journal(root).put(MemoryRecord("source", "fact", 1, {"text": "Verify the sensor."}))
    gateway = Gateway(root, token=TOKEN)
    assert call(gateway, "capabilities", headers={})[0] == 401
    assert call(gateway, "capabilities")[1]["schema"] == SCHEMA
    assert call(gateway, "status")[0] == 404
    configured = call(gateway, "configure", {"mode": "adaptive"})
    assert configured[0] == 200
    request = {"occasion_id": "task-1", "memory_ids": ["source"]}
    status, assignment = call(gateway, "assign", request)
    assert status == 200 and assignment["schema"] == SCHEMA
    assert call(gateway, "assign", request)[1] == assignment
    assert {r["id"] for r in assignment["memories"]} == set(assignment["inject"])
    receipt = {"occasion_id": "task-1", "memory_ids": assignment["inject"]}
    assert call(gateway, "delivery", receipt)[0] == 200
    assert call(gateway, "outcome", {"occasion_id": "task-1", "reward": 0.75})[0] == 200
    assert call(gateway, "outcome", {"occasion_id": "task-1", "reward": 0.75})[0] == 200
    assert call(gateway, "outcome", {"occasion_id": "task-1", "reward": 0.25})[0] == 409
    assert call(gateway, "run", {})[0] == 200
    assert call(gateway, "status")[1]["occasions"] == 1
    assert call(gateway, "rollback", {"reason": "operator review"})[0] == 200


@pytest.mark.parametrize("context", ["agent:other", None, [1], [""], ["x" * 257]])
def test_scope_shape_is_validated(tmp_path, context):
    gateway = Gateway(str(tmp_path), token=TOKEN)
    assert call(gateway, "configure", {"mode": "observe", "context": context})[0] == 400


def test_oversized_integer_rate_is_a_validation_error(tmp_path):
    gateway = Gateway(str(tmp_path), token=TOKEN)
    assert call(gateway, "configure", {"mode": "observe", "holdout_rate": 10**400})[0] == 400


def test_registered_agent_cannot_administer_or_escape_its_scope(tmp_path):
    root = str(tmp_path)
    credential = agent_registry.signup(root, "robot-1", labels=["tenant:factory"])
    headers = {"Authorization": "Bearer " + credential["token"], "Host": "localhost"}
    gateway = Gateway(root, token=TOKEN)
    scope = ["tenant:factory", "agent:robot-1"]
    Journal(root).put(
        MemoryRecord(
            "source", "fact", 1, {"text": "Use gripper calibration."}, scope={"tenant": "factory", "agent": "robot-1"}
        )
    )
    assert call(gateway, "configure", {"mode": "adaptive", "context": scope})[0] == 200
    for operation, body in [("configure", {"mode": "adaptive"}), ("run", {}), ("rollback", {"reason": "untrusted"})]:
        assert call(gateway, operation, body, headers=headers)[0] == 403
    request = {"occasion_id": "task", "memory_ids": ["source"], "context": ["tenant:other"]}
    assert call(gateway, "assign", request, headers=headers)[0] == 200
    assert call(gateway, "status", headers=headers, query="?context=tenant:other")[1]["occasions"] == 1
    # The owner scope is a separate experiment, regardless of caller-supplied context.
    assert call(gateway, "status")[0] == 404


def test_source_revision_and_payload_authority(tmp_path):
    root = str(tmp_path)
    journal = Journal(root)
    journal.put(MemoryRecord("source", "fact", 1, {"text": "Version one."}))
    gateway = Gateway(root, token=TOKEN)
    call(gateway, "configure", {"mode": "observe"})
    request = {"occasion_id": "task", "memory_ids": ["source"]}
    status, assignment = call(gateway, "assign", request)
    assert status == 200
    journal.put(MemoryRecord("source", "fact", 2, {"text": "Version two."}))
    assert call(gateway, "assign", request)[0] == 409
    assert call(gateway, "delivery", {"occasion_id": "task", "memory_ids": assignment["inject"]})[0] == 409
    assert call(gateway, "assign", {**request, "occasion_id": "forged", "sources": [{"text": "forged"}]})[0] == 400
    assert learning.operate(root, "status")["occasions"] == 1
