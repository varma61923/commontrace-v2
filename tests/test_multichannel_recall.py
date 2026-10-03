"""Multi-channel recall: channels fused into one budget, diversity, truncation, per-agent
budgets, and one as_of across every channel."""
import json
import subprocess
import sys

import pytest

from commontrace import graph, hierarchical, recall
from commontrace.conversation import Store
from tests.conftest import write_lesson


@pytest.fixture
def store(tmp_memory):
    write_lesson(tmp_memory, "lesson_pool-reset", description="Reset the postgres pool after failover",
                 rule="When postgres fails over, reset the connection pool before retrying queries.")
    write_lesson(tmp_memory, "lesson_format", description="Run the formatter", rule="Format code before committing.")
    root = str(tmp_memory.parent)
    hierarchical.add_fact(root, "The postgres primary is backed up every hour.", valid_from="2024-01-01")
    graph.add_node(root, "service:postgres", "service", "postgres")
    graph.add_edge(root, "service:postgres", "place:eu_west_1", "located_in", valid_at="2024-01-01")
    graph.add_edge(root, "service:postgres", "place:us_east_1", "located_in", valid_at="2025-06-01")
    with Store(root, "chat") as conv:
        conv.add("s1", [{"speaker": "ana", "text": "The postgres failover last night took nine minutes.",
                         "at": "2025-07-01T10:00"}])
    return root


def test_all_channels_contribute_within_budget(store):
    result = recall.recall(store, "what should I do when postgres fails over?", budget=600)
    channels = {i.channel for i in result.items}
    assert {"lessons", "facts", "graph", "conversations"} <= channels
    assert result.tokens <= 600
    assert "reset the connection pool" in result.context
    assert result.items[0].fused >= result.items[-1].fused or len(result.items) == 1


def test_as_of_is_one_truth_across_channels(store):
    then = recall.recall(store, "where does postgres run?", as_of="2024-06-01", channels=("graph",))
    now = recall.recall(store, "where does postgres run?", channels=("graph",))
    assert "eu_west_1" in then.context and "us_east_1" not in then.context
    assert "us_east_1" in now.context and "eu_west_1" not in now.context
    past = recall.recall(store, "postgres failover", as_of="2025-01-01", channels=("conversations",))
    assert "nine minutes" not in past.context


def test_per_agent_budget_and_weights(store, tmp_memory):
    (tmp_memory / "budgets.json").write_text(json.dumps(
        {"default": 900, "agents": {"reviewer": {"budget": 120, "weights": {"lessons": 5}}}}), encoding="utf-8")
    assert recall.recall(store, "postgres failover").budget == 900
    small = recall.recall(store, "postgres failover", agent="reviewer")
    assert small.budget == 120 and small.tokens <= 120
    assert small.items[0].channel == "lessons"


def test_unknown_channel_and_bad_moment(store):
    with pytest.raises(ValueError):
        recall.recall(store, "x", channels=("nope",))
    with pytest.raises(ValueError):
        recall.recall(store, "x", as_of="not a date")


def test_diversify_drops_restatements():
    a = recall.Item("facts", "1", "postgres runs in eu-west-1 region", fused=0.5)
    b = recall.Item("lessons", "2", "postgres runs in eu-west-1 region", fused=0.4)
    c = recall.Item("graph", "3", "cache sits behind nginx", fused=0.3)
    assert [i.id for i in recall.diversify([a, b, c])] == ["1", "3"]


def test_pack_truncates_at_sentence_and_keeps_channel_floor():
    long = recall.Item("conversations", "c", " ".join(f"Sentence number {n} is here." for n in range(200)),
                       fused=0.9)
    short = recall.Item("lessons", "l", "Reset the pool.", fused=0.1)
    packed = recall.pack([long, short], 100)
    assert {i.id for i in packed} == {"c", "l"}
    cut = next(i for i in packed if i.id == "c")
    assert cut.truncated and cut.text.endswith("...") and recall.tokens(cut.text) <= 100
    assert sum(recall.tokens(i.text) for i in packed) <= 100


def test_truncate_prefers_sentence_boundary():
    text = "First sentence is fine. Second sentence is much longer and will not fit at all here."
    assert recall.truncate(text, 8) == "First sentence is fine. ..."


def test_cli(store):
    out = subprocess.run([sys.executable, "-m", "commontrace.cli", "recall", "postgres failover", "--json",  # nosec
                          "--budget", "400", "--dest", store], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["tokens"] <= 400 and data["items"]


def test_mcp_tool(store, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from commontrace import mcp_server

    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")
    server = mcp_server.build_server(store)

    def call(name, **arguments):
        result = asyncio.run(server.call_tool(name, arguments))
        sc = getattr(result, "structured_content", None)
        return sc.get("result", sc) if sc else json.loads(result.content[0].text)

    got = call("memory_recall", question="postgres failover", budget=500)
    assert got["ok"] and got["tokens"] <= 500 and got["items"]
    assert not call("memory_recall", question="x", channels=["nope"])["ok"]
