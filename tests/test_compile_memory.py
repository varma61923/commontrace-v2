from __future__ import annotations

import pytest

from unimem import compile_memory, evidence, holdout_io, lineage

pytest.importorskip("mcp")
from tests.test_mcp_server import _write_lesson


def test_source_retraction_removes_compiled_artifact_from_serving(tmp_path, monkeypatch):
    root = str(tmp_path)
    path = _write_lesson(root, "rule", body="Verify the target object before planning a grasp.")
    from unimem import frontmatter, lesson_admission
    fm, body = frontmatter.read(path)
    fm["approval_receipt"] = lesson_admission.issue(root, path, fm, body, actor="operator")
    frontmatter.write(path, fm, body)
    monkeypatch.setattr(evidence, "graduated", lambda root: {"rule": {"effect": .5}})
    artifact = compile_memory.build(root, "tiny", ["rule"], target="lora", model="tiny-open", open_weight=True)
    assert not artifact["payload"]["production_weights"]
    assert not compile_memory.stale(root, "tiny")
    with pytest.raises(PermissionError, match="F2 certificate"):
        compile_memory.serve(root, "tiny", {})
    holdout_io.configure(root, rate=.5, salt="compiled")
    compile_memory.register_trial(root, "tiny", "compile-trial", propensity_floor=.5)
    modes = {compile_memory.assign(root, "tiny", str(i))["mode"] for i in range(10)}
    assert modes == {"compiled", "retrieval"}
    from unimem import promotion
    for i in range(10, 610):
        decision = compile_memory.assign(root, "tiny", str(i))
        holdout_io.record_outcome(root, str(i), decision["mode"] == "compiled")
    # Resolve the initial diagnostic occasions through their canonical assignment.
    for row in holdout_io.read_log(root)[0][:10]:
        holdout_io.record_outcome(root, row.occasion_id, row.injected)
    certificate = promotion.certify(root, "compile-trial")
    assert compile_memory.serve(root, "tiny", certificate)["id"] == artifact["id"]
    lineage.taint(root, "rule", "source retracted")
    assert compile_memory.stale(root, "tiny")
    with pytest.raises(PermissionError, match="stale"):
        compile_memory.assign(root, "tiny", "later")


def test_open_weight_and_positive_admission_required(tmp_path):
    with pytest.raises(PermissionError, match="open-weight"):
        compile_memory.build(str(tmp_path), "no", ["source"], target="lora", model="closed", open_weight=False)
    with pytest.raises(PermissionError):
        compile_memory.build(str(tmp_path), "no", ["source"], target="lora", model="open", open_weight=True)
