"""CLI, native MCP and real OpenTelemetry SDK contracts for delegation."""
from __future__ import annotations

import json

import pytest

from commontrace import capability_io as io
from commontrace import hierarchical
from tests.test_capabilities import policy
from tests.test_capability_io import authenticate, fixed_time, prepare, proof  # noqa: F401


def test_cli_grant_attenuate_and_revoke_use_pinned_agent_keys(tmp_path, capsys):
    from commontrace.cli import main

    root, _ = prepare(tmp_path)
    assert main(["team", "grant", json.dumps({"caveats": policy()}), "--dest", root]) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["caveats"]["audience"] == "agent-a"
    args = {"token": issued["token"], "caveats": policy(audience="agent-b", namespaces=["facts"]),
            "secret_file": io.agent(root, "agent-a")["secret_file"]}
    assert main(["team", "attenuate", json.dumps(args), "--dest", root]) == 0
    child = json.loads(capsys.readouterr().out)
    assert child["caveats"]["namespaces"] == ["facts"]
    assert main(["team", "revoke", json.dumps({"chain_id": issued["chain_ids"][0], "reason": "compromised"}),
                 "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["revoked"] == issued["chain_ids"][0]
    with pytest.raises(PermissionError):
        io.check(root, child["token"])


def test_native_mcp_handoff_bundle_is_registered_and_calls_canonical_admission(tmp_path):
    pytest.importorskip("mcp")
    from commontrace import mcp_server
    from commontrace.mcp_tools import LOCAL_TOOLS
    from tests.test_mcp_server import call, tool_names

    root, _ = prepare(tmp_path)
    fact = hierarchical.append_facts(root, [{"statement": "Inspect wrist calibration.", "scopes": ["project:a"]}])[0][0]
    server = mcp_server.build_server(root)
    assert "handoff_bundle" in LOCAL_TOOLS and "handoff_bundle" in tool_names(server)
    bundle = call(server, "handoff_bundle", identities=[fact.id], recipient="agent-b", context=["project:a"])
    assert bundle["ok"] and bundle["claim"]["record"]["recipient"] == "agent-b"
    denied = call(server, "handoff_bundle", identities=[fact.id], recipient="agent-b", context=["project:b"])
    assert not denied["ok"]


def test_real_otel_span_contains_authenticated_chain_digest_and_ceiling(tmp_path, monkeypatch):
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from commontrace import otel_memory, telemetry

    root, token = prepare(tmp_path)
    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_tracer", lambda: provider.get_tracer("unimem-capability-test"))
    otel_memory.configure(root, enabled=True)
    with io.activate(authenticate(root, token, proof(root, token))):
        memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist load.", "scopes": ["project:a"]}])[0][0]
        with otel_memory.operation(root, "search", [memory.id], context=["project:a"],
                                   extensions={"delegation.chain_digest": "forged", "label.ceiling": 3}):
            pass
    spans = exporter.get_finished_spans()
    assert spans
    attributes = spans[-1].attributes
    assert attributes["unimem.delegation.chain_digest"] == io.check(root, token)["chain_digest"]
    assert attributes["unimem.label.ceiling"] == 2
    assert attributes["gen_ai.memory.record.id"] == memory.id
    provider.shutdown()


def test_agents_input_filter_replaces_all_incoming_histories_and_preserves_runtime_context(tmp_path):
    from dataclasses import dataclass
    from pathlib import Path

    from commontrace import frontier_protocol as fp
    from tests.test_capabilities import B_KEY
    from tests.test_capability_io import proof

    fixture = json.loads((Path(__file__).parent / "fixtures" / "capability_handoff_contract.json").read_text())
    @dataclass
    class HandoffInputData:
        input_history: object
        pre_handoff_items: tuple
        new_items: tuple
        run_context: object = None
    assert set(HandoffInputData.__dataclass_fields__) == set(fixture["fields"])
    root, _ = prepare(tmp_path)
    memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist calibration.", "scopes": ["project:a"]}])[0][0]
    claim = io.handoff(root, [memory.id], "agent-b", context=["project:a"])["claim"]
    token = io.grant(root, policy(audience="agent-b"))["token"]
    authorization = io.authenticate(root, token, proof(root, token, key=B_KEY), request_digest="f"*64,
                                   operation="read", purpose="support", principal="b")
    context = object()
    incoming = HandoffInputData("private transcript", ("private old tool output",), ("private new event",), context)
    with io.activate(authorization):
        result = io.agents_input_filter(root, claim, context=["project:a"])(incoming)
        assert result.pre_handoff_items == result.new_items == ()
        assert "private" not in str(result.input_history)
        assert "Inspect wrist calibration." in str(result.input_history)
        assert result.run_context is context
        assert io.inherit_labels(root, []) == ["project:a"]
    assert io.a2a_extension(claim)["params"]["claim"] == claim
    assert fp.verify(root, claim)
