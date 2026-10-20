"""Wire-faithful relationship adapters and native authorization before ranking."""
from __future__ import annotations

import json

import pytest

from commontrace import hierarchical, memory_authority, search_recipes
from commontrace import policy_relations as relations
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord


def config(provider="openfga", namespaces=None):
    return {"version": 1, "provider": provider, "endpoint": "https://authorization.example.test",
            "store_id": "01YCP46JKYM8FJCQ37NMBYHE5X" if provider == "openfga" else "",
            "model_id": "01G5JAVJ41T49E9TT3SKVS7X1J" if provider == "openfga" else "",
            "types": namespaces or {"records": "memory_record"}, "relation": "reader",
            "subjects": {"operator:local": {"type": "user", "id": "operator"}},
            "credential_env": "UNIMEM_RELATION_TOKEN"}


def test_openfga_exact_wire_model_pin_and_closed_subject():
    calls = []

    def transport(url, body, secret):
        calls.append((url, body, secret))
        return b'{"objects":["memory_record:allowed"]}'

    assert relations.lookup(config(), "records", "operator:local", transport=transport) == {"allowed"}
    url, body, secret = calls[0]
    assert url.endswith("/stores/01YCP46JKYM8FJCQ37NMBYHE5X/list-objects")
    assert body == {"authorization_model_id": "01G5JAVJ41T49E9TT3SKVS7X1J", "type": "memory_record",
                    "relation": "reader", "user": "user:operator", "consistency": "HIGHER_CONSISTENCY"}
    assert secret == "UNIMEM_RELATION_TOKEN"
    assert not relations.lookup(config(), "records", "agent:unknown", transport=transport)
    assert len(calls) == 1


def test_spicedb_streaming_full_consistency_and_no_conditional_grant():
    seen = []
    def transport(url, body, secret):
        seen.append((url, body))
        return b'{"result":{"resource_object_id":"allowed","permissionship":"LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"}}\n'

    assert relations.lookup(config("spicedb"), "records", "operator:local", transport=transport) == {"allowed"}
    url, body = seen[0]
    assert url.endswith("/v1/permissions/resources")
    assert body["consistency"] == {"fully_consistent": True}
    assert body["subject"] == {"object": {"object_type": "user", "object_id": "operator"}}
    assert body["optional_limit"] == relations.MAX_IDS+1
    for raw in (b'{"error":{"code":13}}',
                b'{"result":{"resource_object_id":"bad","permissionship":"LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION"}}'):
        with pytest.raises(PermissionError):
            relations.lookup(config("spicedb"), "records", "operator:local", transport=lambda *args: raw)


def test_invalid_or_oversized_response_never_becomes_allow_all():
    for raw in (b'{"objects":["other:allowed"]}', b'{"objects":"*"}', b'{"objects":[null]}', b'NaN',
                b'x'*(relations.MAX_BYTES+1), json.dumps({"objects": ["memory_record:x"]*8193}).encode()):
        with pytest.raises(PermissionError):
            relations.lookup(config(), "records", "operator:local", transport=lambda *args: raw)
    for endpoint in ("http://authorization.example.test", "https://token:secret@example.test", "https://x.test?q=secret"):
        with pytest.raises(ValueError):
            relations.lookup({**config(), "endpoint": endpoint}, "records", "operator:local")


def test_operator_only_immutable_enrollment_binds_configuration(tmp_path):
    root = str(tmp_path)
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError):
            relations.configure(root, config())
    assert relations.configure(root, config()) == relations.configure(root, config())
    with pytest.raises(PermissionError, match="immutable"):
        relations.configure(root, {**config(), "relation": "writer"})
    from commontrace import frontier_protocol as fp

    filename = fp.filename(root, relations.CONFIG)
    value = json.loads(open(filename).read())
    value["configuration"]["record"]["subjects"]["agent:evil"] = {"type": "user", "id": "operator"}
    with open(filename, "w") as stream:
        json.dump(value, stream)
    with pytest.raises(PermissionError, match="signature"):
        relations.load(root)


def test_native_runtime_filters_before_top_k_and_refreshes_provider_every_search(tmp_path, monkeypatch):
    root = str(tmp_path)
    journal = Journal(root)
    for identity, text in (("a-hidden", "Wrist wrist calibration."), ("z-visible", "Wrist calibration.")):
        journal.put(MemoryRecord(identity, "fact", 1, {"text": text}))
    relations.configure(root, config())
    calls = []
    allowed = ["z-visible"]
    def transport(*args):
        calls.append(args)
        return json.dumps({"objects": ["memory_record:"+key for key in allowed]}).encode()
    monkeypatch.setattr(relations, "_transport", transport)
    assert [row.id for row in journal.search("wrist", limit=1)] == ["z-visible"]
    assert len(calls) == 1  # one authorization response for this retrieval
    allowed.clear()
    assert journal.search("wrist", limit=1) == []
    assert len(calls) == 2
    with memory_authority.writer("unregistered", "agent"):
        assert journal.search("wrist") == []
    assert len(calls) == 2
    def unavailable(*args):
        raise PermissionError("provider unavailable")
    monkeypatch.setattr(relations, "_transport", unavailable)
    with pytest.raises(PermissionError):
        journal.search("wrist")


def test_indexed_facts_pass_authorized_ids_to_native_ann_before_top_k(tmp_path, monkeypatch):
    from commontrace import fact_store

    root = str(tmp_path)
    facts = hierarchical.append_facts(root, [{"statement": "Hidden wrist note."}, {"statement": "Visible wrist note."}])
    hidden, visible = [fact.id for fact, _ in facts]
    fact_store.migrate(root)
    relations.configure(root, config(namespaces={"facts": "memory_fact"}))
    monkeypatch.setattr(relations, "_transport", lambda *args: json.dumps({"objects": ["memory_fact:"+visible]}).encode())
    seen = []
    def ann(query, *, limit, allowed_ids):
        seen.append(set(allowed_ids))
        assert hidden not in allowed_ids
        return [(visible, .99)]
    rows = search_recipes.indexed_search(root, "wrist", limit=1, embedder="none", vector_search=ann)
    assert seen == [{visible}]
    assert [row["id"] for row in rows] == [visible]


def test_capture_buffer_and_grounded_record_require_their_own_relationship_ids(tmp_path, monkeypatch):
    root = str(tmp_path)
    journal = Journal(root)
    journal.capture("run", "sensor", {"text": "Private sensor detail."}, scope={}, event_id="raw")
    journal.put(MemoryRecord("derived", "fact", 1, {"text": "Derived sensor detail."}, relations={"caused_by": ["raw"]}))
    relations.configure(root, config())
    permitted = ["derived"]
    monkeypatch.setattr(relations, "_transport", lambda *args: json.dumps({
        "objects": ["memory_record:"+key for key in permitted]}).encode())
    assert journal.search("sensor") == []
    assert journal.buffer("run", scope={}) == []
    permitted.append("raw")
    assert [record.id for record in journal.search("sensor")] == ["derived"]
    assert [item["id"] for item in journal.buffer("run", scope={})] == ["raw"]
