from __future__ import annotations

import asyncio
import importlib.util
import json

import pytest

from commontrace.conversation import AsyncStore, Options, Store
from commontrace.frameworks import FrameworkUnavailable, MemoryTools


def tools(root: str, space: str = "owner-a", session: str = "session-a") -> MemoryTools:
    return MemoryTools(root, space, session, options=Options(embedder="none", rerank=None, summaries=False))


def test_native_tools_capture_and_recall_actual_sqlite_evidence(tmp_path):
    bundle = tools(str(tmp_path))
    recall, remember = bundle.native_tools()
    written = json.loads(remember.invoke({
        "user": "Which deployment region did we choose?", "assistant": "We selected the Stockholm region."
    }))
    assert written["added"] == 2
    response = json.loads(recall.invoke({"query": "Which deployment region did we choose?"}))
    assert "Stockholm" in response["context"]
    assert response["turns"]
    assert response["tokens"] <= 1500
    definitions = recall.definition()
    definitions["function"]["parameters"]["properties"]["query"]["maxLength"] = 1
    assert recall.definition()["function"]["parameters"]["properties"]["query"]["maxLength"] == 20_000


def test_tools_pin_space_and_session_instead_of_trusting_model_parameters(tmp_path):
    a, b = tools(str(tmp_path)), tools(str(tmp_path), "owner-b", "session-b")
    a.remember("What is our release codename?", "The release codename is Kestrel.")
    assert "Kestrel" not in json.loads(b.recall("release codename"))["context"]
    with pytest.raises(ValueError, match="exactly"):
        a.native_tools()[0].invoke({"query": "release codename", "space": "owner-b"})
    with Store(str(tmp_path), "owner-a") as store:
        assert [r[0] for r in store.db.execute("SELECT id FROM sessions")] == ["session-a"]


@pytest.mark.parametrize("query", [None, 2, "", " " * 2, "x" * 20_001])
def test_tool_text_validation_precedes_store_creation(tmp_path, query):
    with pytest.raises(ValueError):
        tools(str(tmp_path)).native_tools()[0].invoke({"query": query})
    assert not (tmp_path / "memory" / "conversations").exists()


@pytest.mark.parametrize("space,session", [("../owner", "s"), ("owner", ""), ("owner", "a\ns")])
def test_invalid_owner_configuration_is_rejected(tmp_path, space, session):
    with pytest.raises(ValueError):
        tools(str(tmp_path), space, session)


def test_async_framework_tools_use_bounded_worker_and_retain_caller_lifecycle(tmp_path):
    async def exercise():
        options = Options(embedder="none", rerank=None, summaries=False)
        async with await AsyncStore.open(str(tmp_path), "owner-a", max_pending=2) as store:
            bundle = MemoryTools(str(tmp_path), "owner-a", "session-a", options=options, async_store=store)
            recall, remember = bundle.native_tools()
            written = await asyncio.gather(*[remember.ainvoke({
                "user": f"Deployment decision {i}", "assistant": f"The selected region for service {i} is Stockholm."
            }) for i in range(4)])
            assert sum(json.loads(item)["added"] for item in written) == 8
            response = json.loads(await recall.ainvoke({"query": "selected region for service 1"}))
            assert "Stockholm" in response["context"]
            assert (await store.stats())["turns"] == 8
            with pytest.raises(ValueError, match="pinned"):
                MemoryTools(str(tmp_path), "owner-b", "session-a", async_store=store)

    asyncio.run(exercise())


def test_async_fallback_works_without_owner_managing_worker(tmp_path):
    async def exercise():
        bundle = tools(str(tmp_path))
        assert json.loads(await bundle.aremember("Release name?", "The name is Kestrel."))["added"] == 2
        assert "Kestrel" in json.loads(await bundle.arecall("release name"))["context"]

    asyncio.run(exercise())


def test_missing_sdk_is_an_actionable_optional_import_error(tmp_path):
    if importlib.util.find_spec("langchain_core") is not None:
        pytest.skip("LangChain SDK installed")
    with pytest.raises(FrameworkUnavailable, match="Install langchain-core"):
        tools(str(tmp_path)).tools("langchain")


def test_unknown_framework_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown framework"):
        tools(str(tmp_path)).tools("invented")


@pytest.mark.parametrize("framework", ["langchain", "langgraph"])
def test_installed_langchain_tools_invoke_real_functions(tmp_path, framework):
    pytest.importorskip("langchain_core.tools")
    recall, remember = tools(str(tmp_path)).tools(framework)
    assert json.loads(remember.invoke({"user": "Release name?", "assistant": "Kestrel is the release name."}))["added"] == 2
    assert "Kestrel" in json.loads(recall.invoke({"query": "release name"}))["context"]


def test_installed_autogen_tools_invoke_real_functions(tmp_path):
    async def exercise():
        pytest.importorskip("autogen_core.tools")
        from autogen_core import CancellationToken

        recall, remember = tools(str(tmp_path)).tools("autogen")
        written = await remember.run_json({"user": "Release name?", "assistant": "Kestrel is the release name."}, CancellationToken())
        assert json.loads(written)["added"] == 2
        recalled = await recall.run_json({"query": "release name"}, CancellationToken())
        assert "Kestrel" in json.loads(recalled)["context"]

    asyncio.run(exercise())


def test_installed_crewai_tools_invoke_real_functions(tmp_path):
    pytest.importorskip("crewai.tools")
    recall, remember = tools(str(tmp_path)).tools("crewai")
    assert json.loads(remember.run(user="Release name?", assistant="Kestrel is the release name."))["added"] == 2
    assert "Kestrel" in json.loads(recall.run(query="release name"))["context"]


def test_installed_llamaindex_tools_invoke_real_functions(tmp_path):
    async def exercise():
        pytest.importorskip("llama_index.core.tools")
        recall, remember = tools(str(tmp_path)).tools("llamaindex")
        written = await remember.acall(user="Release name?", assistant="Kestrel is the release name.")
        assert json.loads(written.raw_output)["added"] == 2
        recalled = await recall.acall(query="release name")
        assert "Kestrel" in json.loads(recalled.raw_output)["context"]

    asyncio.run(exercise())
