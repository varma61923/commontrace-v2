from types import SimpleNamespace

import pytest

from commontrace.memory_adapters import Mem0Adapter


@pytest.mark.parametrize("key", ["user_id", "org_id", "tenant_id", "agent_id", "session_id", "run_id", "filters"])
def test_configured_identity_scope_cannot_be_overridden(key):
    calls = []
    client = SimpleNamespace(search=lambda q, **kw: calls.append(kw) or [])
    pinned = {"user_id": "owner"} if key == "filters" else "owner"
    override = {"user_id": "other"} if key == "filters" else "other"
    adapter = Mem0Adapter(client, **{key: pinned})
    with pytest.raises(ValueError, match="pinned memory scope"):
        adapter.search("q", **{key: override})
    assert not calls
    assert adapter.search("q", top_k=5, **{key: pinned}) == []
    assert calls == [{key: pinned, "top_k": 5}]


def test_external_and_provider_mutation_do_not_change_pinned_filters():
    config = {"user_id": "owner"}
    calls = []

    def search(q, **kwargs):
        calls.append(kwargs["filters"].copy())
        kwargs["filters"]["user_id"] = "provider-mutated"
        return []

    adapter = Mem0Adapter(SimpleNamespace(search=search), filters=config)
    config["user_id"] = "caller-mutated"
    adapter.search("q")
    adapter.search("q")
    assert calls == [{"user_id": "owner"}] * 2
