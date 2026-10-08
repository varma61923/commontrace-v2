import asyncio
import json

import pytest

from commontrace.cli import main
from commontrace.conversation import Store


def test_cli_exposes_opt_in_strategy_and_retains_default(tmp_path, capsys):
    with Store(str(tmp_path), "user") as store:
        store.add("s", [{"text": "Lavender grows in the garden."}], session_at="2025-01-01")
    common = ["conversation", "recall", "user", "garden lavender", "--dest", str(tmp_path),
              "--lexical", "--json"]
    assert main(common) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert main(common + ["--context-strategy", "coverage-v1"]) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert "Lavender grows" in legacy["context"] and "Lavender grows" in candidate["context"]
    with pytest.raises(SystemExit) as exc:
        main(common + ["--context-strategy", "invalid"])
    assert exc.value.code == 2


def test_real_mcp_tool_accepts_strategy_and_refuses_unknown_strategy(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from commontrace.mcp_server import build_server

    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    with Store(str(tmp_path), "user") as store:
        store.add("s", [{"text": "Lavender grows in the garden."}], session_at="2025-01-01")
    server = build_server(str(tmp_path))

    async def drive():
        definitions = {tool.name: tool for tool in await server.list_tools()}
        assert "context_strategy" in definitions["conversation_recall"].input_schema["properties"]
        for strategy, expected in (("coverage-v1", True), ("invalid", False)):
            result = await server.call_tool("conversation_recall", {
                "space": "user", "question": "garden lavender", "context_strategy": strategy})
            payload = json.loads(result.content[0].text)
            assert payload["ok"] is expected
            if expected:
                assert "Lavender grows" in payload["context"]

    asyncio.run(drive())
