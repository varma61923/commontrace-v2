"""Session context layer: scoped memory blocks, rolling summaries, working-memory assembly, gateway routes."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from commontrace import gateway, memory_blocks, working_memory
from commontrace.conversation import ConversationError, Store
from commontrace.conversation import summary as summary_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "s" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN, "Host": "localhost"}


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    monkeypatch.setenv("COMMONTRACE_CONVERSATION_EMBEDDER", "none")


@pytest.fixture
def root(tmp_path):
    os.makedirs(tmp_path / "memory")
    return str(tmp_path)


def _seed(root: str) -> None:
    with Store(root, "u1") as store:
        store.add("trip", [
            {"speaker": "Alice", "text": "I moved to Paris in May and started work at a bakery.", "at": "2024-05-02"},
            {"speaker": "Bot", "text": "Congratulations on the bakery job in Paris.", "at": "2024-05-02"}])
        store.add("plan", [{"speaker": "Alice", "text": f"Note {i}: booking the Lisbon train with my sister.",
                            "at": f"2024-06-{i + 1:02d}"} for i in range(8)])


# --- scoped memory blocks ------------------------------------------------------------


def test_global_blocks_are_unchanged_by_scoping(root):
    block = memory_blocks.set_block(root, "persona", "Global persona.")
    assert "scope" not in block.to_dict()
    assert [b.name for b in memory_blocks.list_blocks(root)] == ["persona"]
    memory_blocks.set_block(root, "persona", "Session persona.", scope="session:s1")
    # A scoped write never leaks into the global listing, history or reads.
    assert memory_blocks.get_block(root, "persona").content == "Global persona."
    assert [b.content for b in memory_blocks.list_blocks(root)] == ["Global persona."]
    assert len(memory_blocks.block_history(root, "persona")) == 1
    assert len(memory_blocks.block_history(root, "persona", scope="session:s1")) == 1
    assert len(memory_blocks.block_history(root, "persona", scope="*")) == 2


def test_resolution_order_is_session_then_agent_then_global(root):
    memory_blocks.set_block(root, "persona", "global")
    memory_blocks.set_block(root, "human", "global human")
    memory_blocks.set_block(root, "persona", "agent", scope="agent:bot")
    memory_blocks.set_block(root, "persona", "session", scope="session:s1")
    memory_blocks.set_block(root, "scratch", "agent scratch", scope="agent:bot")
    assert memory_blocks.resolve_block(root, "persona", session="s1", agent="bot").content == "session"
    assert memory_blocks.resolve_block(root, "persona", session="s2", agent="bot").content == "agent"
    assert memory_blocks.resolve_block(root, "persona").content == "global"
    effective = {b.name: (b.content, b.scope) for b in memory_blocks.resolved_blocks(root, session="s1", agent="bot")}
    assert effective == {"human": ("global human", ""), "persona": ("session", "session:s1"),
                         "scratch": ("agent scratch", "agent:bot")}
    assert memory_blocks.list_scopes(root) == ["agent:bot", "session:s1"]
    with pytest.raises(memory_blocks.BlockNotFoundError):
        memory_blocks.resolve_block(root, "missing", session="s1")


def test_scoped_mutations_and_validation(root):
    memory_blocks.append_block(root, "notes", "first", scope="session:s1")
    memory_blocks.insert_block(root, "notes", "zeroth", 0, scope="session:s1")
    memory_blocks.replace_block(root, "notes", "first", "second", scope="session:s1")
    assert memory_blocks.get_block(root, "notes", scope="session:s1").content == "zeroth\nsecond"
    with pytest.raises(memory_blocks.BlockNotFoundError, match="session:s1"):
        memory_blocks.get_block(root, "nothing", scope="session:s1")
    assert memory_blocks.delete_block(root, "notes", scope="session:s1") is True
    for bad in ("tenant:x", "session:", "session:\x00x"):
        with pytest.raises(memory_blocks.MemoryBlockError):
            memory_blocks.set_block(root, "x", "y", scope=bad)
    # Ids that sanitise identically still get separate directories.
    memory_blocks.set_block(root, "p", "one", scope="session:a/b")
    memory_blocks.set_block(root, "p", "two", scope="session:a_b")
    assert memory_blocks.get_block(root, "p", scope="session:a/b").content == "one"
    assert 'scope="session:a_b"' in memory_blocks.render_memory_blocks(
        memory_blocks.list_blocks(root, scope="session:a_b"))


def test_block_cli_scope_flags(root):
    def cli(*argv):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "block", *argv, "--dest", root],
                              capture_output=True, text=True, cwd=REPO, check=False)

    assert cli("set", "persona", "Global.").returncode == 0
    assert cli("set", "persona", "Session only.", "--scope", "session:s9").returncode == 0
    assert "Global." in cli("get", "persona").stdout
    assert "Session only." in cli("get", "persona", "--session", "s9").stdout
    assert "session:s9" in cli("list", "--scope", "session:s9").stdout
    assert cli("get", "persona", "--scope", "bogus:1").returncode == 1


# --- rolling summaries -----------------------------------------------------------------


def test_rolling_from_scratch_matches_extractive(root):
    _seed(root)
    with Store(root, "u1") as store:
        out = summary_mod.rolling(store, "plan")
        assert out["mode"] == "full" and out["reason"] == "first" and out["turns"] == 8
        assert out["text"] == summary_mod.extractive(store.session_turns("plan"))
        assert store.summaries()["plan"]["method"] == "rolling-extractive"


def test_rolling_reads_only_new_turns(root, monkeypatch):
    _seed(root)
    with Store(root, "u1") as store:
        summary_mod.rolling(store, "plan")
        assert summary_mod.rolling(store, "plan")["mode"] == "unchanged"
        store.add("plan", [{"speaker": "Alice", "text": "The Lisbon train leaves Friday morning at nine sharp.",
                            "at": "2024-06-20"}])
        folded = []
        original = summary_mod._fold_extractive

        def spy(state, turns, sentences, limit):
            turns = list(turns)
            folded.append([t.idx for t in turns])
            return original(state, turns, sentences, limit)

        monkeypatch.setattr(summary_mod, "_fold_extractive", spy)
        out = summary_mod.rolling(store, "plan")
        assert out["mode"] == "incremental" and out["new_turns"] == 1 and out["turns"] == 9
        assert folded == [[8]]
        state = summary_mod.rolling_state(store, "plan")
        assert state["through_idx"] == 8 and len(state["pool"]) <= summary_mod.ROLLING_POOL
        assert store.summaries()["plan"]["turns"] == 9


def test_rolling_rebuilds_when_earlier_turns_change(root):
    _seed(root)
    with Store(root, "u1") as store:
        summary_mod.rolling(store, "plan")
        # Tamper with a covered turn behind the summary's back.
        with store.db:
            store.db.execute("UPDATE turns SET text='Note 0: the Lisbon trip is cancelled entirely now.' "
                             "WHERE session='plan' AND idx=0")
        out = summary_mod.rolling(store, "plan")
        assert out["mode"] == "full" and out["reason"] == "history-changed"
        # A purged prefix is caught by the cheap tail check as well.
        summary_mod.rolling(store, "plan", force=True)
        store.purge(before="2024-06-03")
        assert summary_mod.rolling(store, "plan", verify="tail")["reason"] == "history-changed"


def test_rolling_model_path_sends_only_new_turns(root):
    _seed(root)
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return f"Summary {len(prompts)}.", {}

    with Store(root, "u1") as store:
        assert summary_mod.rolling(store, "plan", method="model", complete=complete)["text"] == "Summary 1."
        store.add("plan", [{"speaker": "Alice", "text": "We will take the late train instead.", "at": "2024-06-21"}])
        out = summary_mod.rolling(store, "plan", method="model", complete=complete)
        assert out["mode"] == "incremental" and out["text"] == "Summary 2."
        assert "Running summary:\nSummary 1." in prompts[1] and "late train" in prompts[1]
        assert "Note 0" not in prompts[1]
        assert summary_mod.rolling(store, "plan", complete=complete)["reason"] == "method-changed"
        with pytest.raises(ConversationError, match="no session"):
            summary_mod.rolling(store, "absent")


# --- working memory --------------------------------------------------------------------


def test_assemble_labels_sections_dedupes_and_is_deterministic(root):
    _seed(root)
    memory_blocks.set_block(root, "persona", "You are a travel assistant.")
    memory_blocks.set_block(root, "persona", "Be terse in this session.", scope="session:plan")
    memory_blocks.set_block(root, "human", "Alice prefers trains.", scope="agent:bot")
    with Store(root, "u1") as store:
        summary_mod.rolling(store, "plan")
    first = working_memory.assemble(root, "u1", "plan", "Where did Alice move?", budget=600, recent_turns=3,
                                    agent="bot")
    again = working_memory.assemble(root, "u1", "plan", "Where did Alice move?", budget=600, recent_turns=3,
                                    agent="bot")
    assert first == again
    names = [s["name"] for s in first["sections"]]
    assert names == ["core_blocks", "session_summary", "recent_turns", "evidence"]
    sections = {s["name"]: s for s in first["sections"]}
    assert "Be terse" in sections["core_blocks"]["text"] and "travel assistant" not in sections["core_blocks"]["text"]
    assert "Alice prefers trains" in sections["core_blocks"]["text"]
    assert sections["session_summary"]["text"].startswith("## Session summary")
    assert [i["idx"] for i in sections["recent_turns"]["items"]] == [5, 6, 7]
    evidence_turns = {i["turn"] for i in sections["evidence"]["items"]}
    assert not evidence_turns & {i["turn"] for i in sections["recent_turns"]["items"]}
    assert "Paris" in sections["evidence"]["text"]
    explain = first["explain"]
    assert first["tokens"] <= 600 and sum(explain["spent"].values()) <= 600
    assert set(explain["allocated"]) == set(names) and explain["summary"]["source"] == "rolling"
    assert first["context"].index("## Core memory") < first["context"].index("## Retrieved evidence")


def test_assemble_respects_tight_budgets_and_validates(root):
    _seed(root)
    out = working_memory.assemble(root, "u1", "plan", "Lisbon train", budget=80, recent_turns=8)
    assert out["tokens"] <= 80 + 4  # section joins are the only overhead beyond the per-section caps
    assert out["explain"]["dropped"]["recent_turns"] > 0
    with pytest.raises(ConversationError):
        working_memory.assemble(root, "u1", "plan", "q", budget=10)
    with pytest.raises(ConversationError):
        working_memory.assemble(root, "nowhere", "plan", "q")


def test_sessions_overview(root):
    _seed(root)
    with Store(root, "u1") as store:
        summary_mod.rolling(store, "trip")
        listing = working_memory.sessions(store, limit=1)
        assert listing["total"] == 2 and listing["next_after"] == 1
        row = listing["sessions"][0]
        assert row["id"] == "trip" and row["turns"] == 2 and row["first_at"].startswith("2024-05-02")
        assert row["summary"]["current"] and row["summary"]["pending_turns"] == 0
        rest = working_memory.sessions(store, after_seq=listing["next_after"])
        assert [r["id"] for r in rest["sessions"]] == ["plan"] and rest["next_after"] is None


# --- gateway -------------------------------------------------------------------------------


def _call(gw, method, target, body=None, headers=None):
    response = gw.handle(method, target, headers or HEADERS, json.dumps(body).encode() if body is not None else None)
    return response.status, json.loads(response.body)


def test_gateway_session_routes(root):
    gw = gateway.Gateway(root, token=TOKEN, durable=False)
    assert _call(gw, "POST", "/v1/conversation/add", {"space": "u1", "session": "s1", "messages": [
        {"speaker": "A", "text": "I moved to Oslo in March for a new job.", "at": "2024-03-01"},
        {"speaker": "B", "text": "Oslo winters are long and dark.", "at": "2024-03-01"}]})[0] == 200
    status, listing = _call(gw, "GET", "/v1/conversation/sessions?space=u1")
    assert status == 200 and listing["space"] == "u1" and listing["sessions"][0]["turns"] == 2
    status, out = _call(gw, "POST", "/v1/conversation/summarize", {"space": "u1", "session": "s1"})
    assert status == 200 and out["mode"] == "full" and out["space"] == "u1"
    assert _call(gw, "POST", "/v1/conversation/summarize", {"space": "u1", "session": "s1"})[1]["mode"] == "unchanged"
    assert _call(gw, "POST", "/v1/conversation/summarize", {"space": "u1", "session": "s1", "mode": "x"})[0] == 400
    status, wm = _call(gw, "POST", "/v1/working-memory", {"space": "u1", "session": "s1", "question": "Where?",
                                                          "budget": 300, "recent_turns": 1})
    assert status == 200 and wm["space"] == "u1" and len(wm["sections"]) == 4
    assert _call(gw, "POST", "/v1/working-memory", {"space": "nope", "session": "s1", "question": "q"})[0] == 404


def test_gateway_container_scoping_isolates_sessions_and_blocks(root):
    gw = gateway.Gateway(root, token=TOKEN, durable=False)
    tenant_a = {**HEADERS, "X-Container-Tag": "team-a"}
    tenant_b = {**HEADERS, "X-Container-Tag": "team-b"}
    _call(gw, "POST", "/v1/conversation/add", {"space": "u1", "session": "s1", "messages": [
        {"speaker": "A", "text": "Team A secret plan for the launch."}]}, headers=tenant_a)
    status, _ = _call(gw, "GET", "/v1/conversation/sessions?space=u1", headers=tenant_b)
    assert status == 404
    assert _call(gw, "GET", "/v1/conversation/sessions?space=u1", headers=tenant_a)[0] == 200
    from commontrace import telemetry

    with telemetry.bind(container_tag="team-a"):
        namespace = gw._scoped_space("u1")  # the namespace the gateway derives for team-a
    memory_blocks.set_block(root, "persona", "Team A session persona.", scope=f"session:{namespace}/s1")
    memory_blocks.set_block(root, "persona", "Unscoped session persona.", scope="session:s1")
    status, wm = _call(gw, "POST", "/v1/working-memory", {"space": "u1", "session": "s1", "question": "plan"},
                       headers=tenant_a)
    assert status == 200
    assert "Team A session persona." in wm["context"] and "Unscoped" not in wm["context"]
