from __future__ import annotations

import json

import pytest

from unimem import frontier_protocol as fp
from unimem import hierarchical, lineage, memory_authority


def bind(root, identity, sources=(), authority="operator", principal="local", text="evidence"):
    with memory_authority.writer(principal, authority):
        return memory_authority.bind(root, {"id": identity, "text": text}, sources=list(sources))


def test_no_summary_or_echo_laundering(tmp_path):
    root = str(tmp_path)
    bind(root, "untrusted", authority="external", principal="web")
    bind(root, "summary", ["untrusted"])
    bind(root, "triplet", ["summary"])
    assert lineage.labels(root, "triplet")["integrity"] == "external"
    assert lineage.ancestors(root, "triplet") == ["summary", "untrusted"]
    assert lineage.descendants(root, "untrusted") == ["summary", "triplet"]
    lineage.taint(root, "untrusted", "harm withdrawal")
    assert lineage.labels(root, "triplet")["review_required"]


def test_legacy_is_unknown_and_missing_parent_cannot_upgrade(tmp_path):
    root = str(tmp_path)
    assert lineage.labels(root, "old")["integrity"] == "unknown"
    bind(root, "derived", ["old"])
    assert lineage.labels(root, "derived")["integrity"] == "unknown"


def test_corroboration_counts_independent_roots(tmp_path, monkeypatch):
    from unimem import causal_gate

    root = str(tmp_path)
    bind(root, "target", authority="external")
    for i in range(3):
        bind(root, f"echo{i}", ["target"])
    with pytest.raises(ValueError, match="quorum"):
        lineage.corroborate(root, "target", [f"echo{i}" for i in range(3)])
    model = causal_gate.Model(sources={f"org{i}": causal_gate.SourceReputation(f"org{i}", 10, .2, .01, .99)
                                     for i in range(3)})
    monkeypatch.setattr(causal_gate, "fit", lambda *a, **kw: model)
    for i in range(3):
        bind(root, f"source{i}", principal=f"org{i}")
    lineage.corroborate(root, "target", [f"source{i}" for i in range(3)])
    assert lineage.labels(root, "target")["integrity"] == "reviewed"


def test_supersession_flags_derived_objects(tmp_path):
    root = str(tmp_path)
    old, _ = hierarchical.add_fact(root, "Robot arm is in room A", valid_from="2026-01-01T00:00:00Z")
    bind(root, "lesson", [old.id])
    bind(root, "triplet", ["lesson"])
    hierarchical.supersede_fact(root, old.id, "Robot arm is in room B", as_of="2026-02-01T00:00:00Z")
    assert lineage.labels(root, "lesson")["review_required"]
    assert lineage.labels(root, "triplet")["review_required"]


def test_forged_replayed_and_oversized_attestations_fail(tmp_path):
    root = str(tmp_path)
    signed = bind(root, "first")
    with pytest.raises(PermissionError):
        lineage.attest(root, {"id": "forged"}, signed, inputs=[])
    with pytest.raises(ValueError):
        lineage.attest(root, signed["record"], signed, inputs=["x"] * 257)
    path = tmp_path / "memory" / lineage.JOURNAL
    row = json.loads(path.read_text())
    row["record"]["integrity"] = "reviewed"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="invalid"):
        lineage.labels(root, "first")


def test_lineage_surfaces(tmp_path):
    from unimem.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import call, cli

    root = str(tmp_path)
    bind(root, "item")
    result = call(build_server(root), "lineage_explain", id="item")
    assert result["ok"] and result["integrity"] == "operator"
    result = cli("lineage", "taint", "item", "--reason", "review", "--dest", root)
    assert result.returncode == 0, result.stderr
    result = cli("lineage", "show", "item", "--dest", root)
    assert json.loads(result.stdout)["review_required"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_rejected(value):
    with pytest.raises(ValueError):
        fp.number(value, 0, 1)
