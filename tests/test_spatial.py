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


def test_resightings_reinforce_placements_and_respect_invalidation(tmp_path):
    from commontrace import hierarchical

    root = str(tmp_path)
    first = spatial.update(root, delta([1, 2, 3], "2026-01-01T00:00:00Z"), context=CONTEXT)
    again = spatial.update(root, delta([1.004, 2, 3], "2026-01-01T00:05:00Z"), context=CONTEXT)
    assert again["fact_id"] == first["fact_id"]  # same placement within tolerance: no new fact
    moved = spatial.update(root, delta([9, 2, 3], "2026-01-02T00:00:00Z"), context=CONTEXT)
    assert moved["fact_id"] != first["fact_id"]
    hierarchical.invalidate_fact(root, moved["fact_id"], at="2026-01-03T00:00:00Z")
    back = spatial.update(root, delta([9, 2, 3], "2026-01-04T00:00:00Z"), context=CONTEXT)
    assert back["fact_id"] != moved["fact_id"]  # an invalidated placement is never reinforced
    assert spatial.where_is(root, "crate", context=CONTEXT)["id"] == back["id"]
    history = spatial.history(root, "crate", context=CONTEXT)
    assert [row["id"] for row in history] == [first["id"], again["id"], back["id"]]
    history[0]["delta"]["pose"][0] = 999  # results are copies, never the cached journal
    assert spatial.history(root, "crate", context=CONTEXT)[0]["delta"]["pose"][0] == 1


def test_optional_robot_context_reuses_stationary_placement_fact(tmp_path):
    root = str(tmp_path)
    with_robot = {"tenant": "acme", "site": "depot", "robot": "r7"}
    without_robot = {"tenant": "acme", "site": "depot"}
    first = spatial.update(root, delta([1, 2, 3], "2026-01-01T00:00:00Z"), context=with_robot)
    second = spatial.update(root, delta([1, 2, 3], "2026-01-01T00:05:00Z"), context=without_robot)
    assert second["fact_id"] == first["fact_id"]


@pytest.mark.parametrize("bad", [{"relations": {"near": "not-a-list"}}, {"media": "sha256:x"},
                                 {"pose": [1, 2, float("nan")]}, {"confidence": float("inf")}])
def test_malformed_deltas_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        spatial.update(str(tmp_path), {**delta([1, 2, 3], fp.now()), **bad}, context=CONTEXT)
