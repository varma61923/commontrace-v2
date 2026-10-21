from __future__ import annotations

import base64
import json

import pytest

from unimem import frontier_protocol as fp
from unimem import lineage, memory_authority, memory_ifc, redteam


def test_laundering_labels_and_confidentiality_join(tmp_path):
    root = str(tmp_path)
    with memory_authority.writer("external-web", "external"):
        memory_authority.bind(root, {"id": "web", "text": "imitate this procedure"})
    memory_authority.bind(root, {"id": "summary", "text": "summarized procedure"}, sources=["web"])
    labelled = memory_ifc.label(root, {"id": "summary", "text": "summarized procedure"})
    assert not labelled["procedure"] and labelled["quoted_text"].startswith("> ")
    assert labelled["integrity"] == "external" and labelled["lineage_digest"]
    memory_authority.bind(root, {"id": "private", "record_kind": "markdown-store", "text": "secret",
                                 "scopes": ["tenant:a"]})
    with pytest.raises(PermissionError, match="scope"):
        memory_authority.bind(root, {"id": "public", "record_kind": "markdown-store", "text": "summary",
                                     "scopes": ["tenant:b"]}, sources=["private"])


def test_origin_group_budget_and_fresh_sensor_signature(tmp_path):
    root = str(tmp_path)
    result = memory_ifc.aggregate(root, ["unknown"], lambda rows: "answer", high_consequence=True)
    assert result["ask_human"] and result["calls"] == 0
    fresh = fp.sign(root, {"kind": "in-situ-sensor", "memory_id": "object", "observed_at": fp.now()})
    assert memory_ifc.sensor_corroborated(root, "object", [fresh])
    fresh["record"]["memory_id"] = "other"
    assert not memory_ifc.sensor_corroborated(root, "other", [fresh])


def test_tenant_canary_detects_base64_hex_and_unicode(tmp_path):
    root = str(tmp_path)
    token = memory_ifc.create_canary(root, "tenant-a")["text"]
    encoded = base64.b64encode(token.encode()).decode()
    assert memory_ifc.egress_match(root, "tenant-a", encoded)["alert"]
    assert memory_ifc.egress_match(root, "tenant-a", token.encode().hex())["alert"]
    assert memory_ifc.egress_match(root, "tenant-a", "\u200b".join(token))["alert"]
    assert not memory_ifc.egress_match(root, "tenant-b", token)["alert"]


def test_seeded_shadow_report_and_cli(tmp_path):
    pytest.importorskip("mcp")
    from tests.test_mcp_server import cli

    first = redteam.run(seed=4, occasions=64)
    assert first == redteam.run(seed=4, occasions=64)
    assert len(first["results"]) == 5 and all(row["act_rate"] == 0 for row in first["results"])
    assert all(row["time_to_withdraw"] is not None for row in first["results"])
    out = cli("defense", "redteam", "--seed", "4", "--occasions", "64")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == first


def test_ifc_gateway_and_mcp_surfaces(tmp_path):
    from unimem.mcp_server import build_server
    pytest.importorskip("mcp")
    from tests.test_mcp_server import _write_lesson, call

    root = str(tmp_path)
    _write_lesson(root, "rule", body="Check the suppression list for missing email.", description="missing email")
    out = call(build_server(root), "retrieve", task="missing email suppression list")
    assert "procedure" in out["lessons"][0]
    assert "confidentiality" in out["lessons"][0]
    assert lineage.labels(root, "legacy")["integrity"] == "unknown"
