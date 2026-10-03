"""The DolphinBench harness without DolphinBench: ingestion into CommonTrace, frozen
read-only memory, a scripted tool loop, checkpoint verification and cost accounting."""
import asyncio
import dataclasses
import importlib.util
import os
import sys
import types

import pytest

HARNESS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "benchmarks", "dolphinbench",
                       "commontrace_harness.py")


@dataclasses.dataclass
class InteractionRecord:
    settings: dict
    messages: list
    duration_ms: float | None = None
    attempts: list = dataclasses.field(default_factory=list)
    app_calls: list | None = None


@dataclasses.dataclass(frozen=True)
class Interaction:
    persona: str
    phase: str
    interaction_id: str
    message: str
    narrative_time: str
    apps: dict
    work_dir: str

    @property
    def dated_message(self):
        return f"[{self.narrative_time}] {self.message}"


@pytest.fixture
def H(monkeypatch):
    pkg = types.ModuleType("harness")
    adapter = types.ModuleType("harness.adapter")
    adapter.InteractionRecord = InteractionRecord
    monkeypatch.setitem(sys.modules, "harness", pkg)
    monkeypatch.setitem(sys.modules, "harness.adapter", adapter)
    spec = importlib.util.spec_from_file_location("commontrace_harness_under_test", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(name, props):
    return types.SimpleNamespace(name=name, description=f"{name} tool",
                                 inputSchema={"type": "object", "properties": props})


def test_ingest_freeze_test_and_cost(H, tmp_path, monkeypatch):
    seen = {}

    def fake_complete(self, system, messages, tools, max_tokens):
        usage = {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}
        if not tools:
            return {"role": "assistant", "content": "Noted."}, usage
        steps = sum(1 for m in messages if m["role"] == "assistant")
        if steps == 0:
            seen["memory"] = messages[0]["content"]
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "search_memory", "arguments": {"query": "coffee"}}]}, usage
        if steps == 1:
            seen["search"] = messages[-1]["content"]
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "name": "place_order", "arguments": {"restaurant": "Blue Bottle"}}]}, usage
        return {"role": "assistant", "content": "Ordered from Blue Bottle."}, usage

    monkeypatch.setattr(H.ChatModel, "complete", fake_complete)
    h = H.BenchmarkHarness({"model": "scripted", "embedder": "none", "rerank": "none",
                            "prices": {"input": 1.0, "output": 4.0}}, str(tmp_path))
    history = [("000001", "2023-01-09T08:00:00-08:00", "still doing blue bottle if i'm out early enough"),
               ("000002", "2023-01-10T08:30:00-08:00", "Q1 all-hands first thing."),
               ("000003", "2023-01-10T19:00:00-08:00", "Kibo is asleep on the couch.")]
    for sid, when, text in history:
        record = h.run_interaction(Interaction("morgan", "ingestion", sid, text, when, {}, str(tmp_path)))
        assert record.messages[1]["content"] == f"[{when}] {text}"
        assert record.messages[-1]["role"] == "assistant" and record.messages[-1]["usage"]
    checkpoint = h.freeze("morgan")
    assert checkpoint["turns"] == 3
    h.verify_checkpoint("morgan", checkpoint)

    calls = []

    async def call_app(name, arguments):
        calls.append((name, arguments))
        return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"ok": true}')], isError=False)

    request = Interaction("morgan", "tests", "001", "I'm out early. Please order one small latte for pickup.",
                          "2026-09-14", {}, str(tmp_path))
    record = asyncio.run(h.run_agent(request, [_tool("place_order", {"restaurant": {"type": "string"}})], call_app))
    roles = [m["role"] for m in record.messages]
    assert roles == ["system", "developer", "user", "assistant", "tool", "assistant", "tool", "assistant"]
    assert [m["content"] for m in record.messages if m["role"] == "user"] == [request.dated_message]
    assert "blue bottle" in seen["memory"].lower()
    assert calls == [("place_order", {"restaurant": "Blue Bottle"})]
    assert record.settings["tools"][0]["name"] == "search_memory"
    h.verify_checkpoint("morgan", checkpoint)  # a test run leaves memory untouched

    assert h.total_cost_usd("ingestion") == pytest.approx(3 * (1000 * 1.0 + 10 * 4.0) / 1e6)
    assert h.total_cost_usd("tests") == pytest.approx(3 * (1000 * 1.0 + 10 * 4.0) / 1e6)


def test_changed_memory_fails_verification(H, tmp_path, monkeypatch):
    monkeypatch.setattr(H.ChatModel, "complete",
                        lambda self, *a, **k: ({"role": "assistant", "content": "ok"}, {"input_tokens": 1,
                                                                                         "output_tokens": 1}))
    h = H.BenchmarkHarness({"model": "m", "embedder": "none", "prices": {"input": 1, "output": 1}}, str(tmp_path))
    h.run_interaction(Interaction("alex", "ingestion", "1", "hello", "2023-03-05T09:30:00-05:00", {}, str(tmp_path)))
    checkpoint = h.freeze("alex")
    h.run_interaction(Interaction("alex", "ingestion", "2", "more", "2023-03-06T09:30:00-05:00", {}, str(tmp_path)))
    with pytest.raises(H.HarnessError):
        h.verify_checkpoint("alex", checkpoint)


def test_options_and_missing_prices_are_refused(H, tmp_path):
    with pytest.raises(ValueError):
        H.BenchmarkHarness({"bogus": 1}, str(tmp_path))
    h = H.BenchmarkHarness({"model": "m"}, str(tmp_path))
    with pytest.raises(H.HarnessError):
        h.total_cost_usd("tests")
    assert "api_key_env" not in h.identity()["options"]
