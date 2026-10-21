from __future__ import annotations

import json

import pytest

from unimem import frontier_protocol as fp
from unimem import holdout_io, lineage, memory_adapters

FIXTURES = {
    "mem0": {"results": [{"id": "m1", "memory": "Verify the object class"}]},
    "zep": {"edges": [{"uuid": "e1", "fact": "Verify the object class"}]},
    "graphiti": {"edges": [{"uuid_": "g1", "fact": "Verify the object class"}]},
    "letta": {"passages": [{"id": "p1", "text": "Verify the object class"}]},
    "agentcore": {"memoryRecordSummaries": [{"memoryRecordId": "r1", "content": {"text": "Verify the object class"}}]},
    "vertex": {"memories": [{"name": "projects/p/memories/m1", "fact": "Verify the object class"}]},
    "foundry": {"items": [{"id": "f1", "text": "Verify the object class"}]},
    "anthropic": {"items": [{"key": "a1", "content": "Verify the object class"}]},
}


class Response:
    def __init__(self, data):
        self.data, self.content = data, json.dumps(data).encode()
    def raise_for_status(self):
        pass
    def json(self):
        return self.data


class Client:
    def __init__(self, data):
        self.data, self.requests = data, []
    def post(self, endpoint, **kwargs):
        self.requests.append(kwargs)
        return Response(self.data)


@pytest.mark.parametrize("provider", sorted(FIXTURES))
def test_pinned_vendor_fixtures_stable_ids_and_safe_provenance(provider, tmp_path):
    client = Client(FIXTURES[provider])
    adapter = memory_adapters.VendorReadAdapter(provider, endpoint="https://memory.example/retrieve",
        scope={"tenant_id": "acme"}, client=client, headers={"Authorization": "secret-token"})
    first, second = adapter.search("object"), adapter.search("object")
    assert first == second and first[0].text == "Verify the object class"
    assert "secret-token" not in str(first)
    with pytest.raises(ValueError, match="scopes are pinned"):
        adapter.search("object", tenant_id="other")
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="foreign")
    measured = memory_adapters.MeasuredMemory(adapter, root=root, check_every=1000)
    for i in range(30):
        recalled = measured.recall("object", occasion_id=str(i))
        measured.record_outcome(str(i), succeeded=bool(recalled))
    assert lineage.labels(root, first[0].id)["integrity"] == "external"
    detailed = measured.recall_detailed("object", occasion_id="metadata")
    if detailed.items:
        assert not detailed.items[0].raw["ifc"]["procedure"]
    assert provider in memory_adapters.comparative_report(root)["stores"]
    assert "secret-token" not in open(fp.filename(root, "lineage.jsonl")).read()


def test_credential_bearing_transport_errors_are_sanitized():
    class BadClient:
        def post(self, *args, **kwargs):
            raise RuntimeError("Authorization: super-secret")
    adapter = memory_adapters.VendorReadAdapter("vertex", endpoint="https://memory.example/retrieve",
                                                scope={"tenant": "acme"}, client=BadClient())
    with pytest.raises(ValueError) as exc:
        adapter.search("object")
    assert "super-secret" not in str(exc.value)


def test_one_bad_vendor_item_is_skipped_not_fatal(tmp_path):
    data = {"results": [{"id": "huge", "memory": "x" * 70_000}, {"id": "empty", "memory": ""},
                        {"id": "nested", "memory": {"unexpected": 1}}, {"id": "ok", "memory": "Verify the class"}]}
    adapter = memory_adapters.VendorReadAdapter("mem0", endpoint="https://memory.example/retrieve",
                                                scope={"tenant_id": "acme"}, client=Client(data))
    assert [item.text for item in adapter.search("class")] == ["Verify the class"]
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="foreign")
    measured = memory_adapters.MeasuredMemory(adapter, root=root, check_every=1000)
    assert measured.recall_detailed("class", occasion_id="one").items is not None
