from __future__ import annotations

import pytest

from commontrace import frontier_protocol as fp
from commontrace import spatial

CONTEXT = {"tenant": "acme", "site": "depot", "robot": "r7"}


def delta(position, timestamp):
    return {"object_id": "crate", "class": "box", "pose": position, "frame": "map", "robot_id": "r7",
            "timestamp": timestamp, "confidence": .9, "relations": {"located_in": ["depot"]},
            "keyframe": "sha256:abc"}


def test_moves_corrections_and_record_time_beliefs(tmp_path):
    root = str(tmp_path)
    a = spatial.update(root, delta([1, 2, 3], "2026-01-01T00:00:00Z"), context=CONTEXT)
    belief_time = fp.now()
    b = spatial.update(root, delta([4, 5, 6], "2026-02-01T00:00:00Z"), context=CONTEXT)
    spatial.update(root, delta([7, 8, 9], "2026-01-01T00:00:00Z"), context=CONTEXT, correction_of=a["id"])
    assert spatial.where_was(root, "crate", "2026-01-15T00:00:00Z", context=CONTEXT)["delta"]["pose"] == [7, 8, 9]
    assert spatial.where_is(root, "crate", context=CONTEXT)["id"] == b["id"]
    assert spatial.believed_at(root, "r7", belief_time, context=CONTEXT)[0]["delta"]["pose"] == [1, 2, 3]
    assert spatial.where_is(root, "crate", context={**CONTEXT, "tenant": "other"}) is None
    assert len(spatial.changes_since(root, belief_time, context=CONTEXT)) == 2
    with pytest.raises(PermissionError):
        spatial.sharing_record(a, tenant="other")
    assert spatial.sharing_record(a, tenant="acme").scope["labels"] == ["site:depot"]


def test_bounded_updates_and_planner_tools(tmp_path):
    from commontrace.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call
    root = str(tmp_path)
    spatial.update(root, delta([1, 2, 3], "2026-01-01T00:00:00Z"), context=CONTEXT)
    assert call(build_server(root), "spatial_where", object_id="crate", context=CONTEXT)["object"]["keyframe"] == "sha256:abc"
    with pytest.raises(ValueError):
        spatial.update(root, {**delta([1, 2, 3], fp.now()), "embedding_ref": "x" * 5000}, context=CONTEXT)


def test_scene_objects_obey_canonical_forgetting_and_validity(tmp_path):
    from commontrace import hierarchical
    root = str(tmp_path)
    item = spatial.update(root, delta([1, 2, 3], "2026-01-01T00:00:00Z"), context=CONTEXT)
    hierarchical.invalidate_fact(root, item["fact_id"], at="2026-02-01T00:00:00Z")
    assert spatial.where_is(root, "crate", context=CONTEXT) is None
    assert spatial.where_was(root, "crate", "2026-01-15T00:00:00Z", context=CONTEXT) is not None
    hierarchical.forget_fact(root, item["fact_id"])
    assert spatial.history(root, "crate", context=CONTEXT) == []
