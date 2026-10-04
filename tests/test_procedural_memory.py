import pytest

from commontrace.procedural import (
    ProceduralMemory,
    ProceduralStep,
    format_procedural_memory,
    list_procedural_memories,
    load_procedural_memory,
    save_procedural_memory,
)


def test_procedural_memory_serialization():
    step1 = ProceduralStep(
        step_number=1,
        action="GET /api/v1/users",
        result='{"users": [{"id": 1, "name": "Alice"}]}',
        key_findings="Found Alice as admin",
        current_context="Ready to inspect roles",
    )
    step2 = ProceduralStep(
        step_number=2,
        action="GET /api/v1/roles/1",
        result='{"role": "superuser"}',
        key_findings="Alice is confirmed superuser",
        current_context="Task finished",
    )
    mem = ProceduralMemory(
        id="mem-proc-001",
        task_objective="Verify Alice admin role",
        progress_status="100%",
        steps=[step1, step2],
        agent_id="agent-007",
    )

    d = mem.to_dict()
    roundtrip = ProceduralMemory.from_dict(d)
    assert roundtrip.id == "mem-proc-001"
    assert roundtrip.task_objective == "Verify Alice admin role"
    assert len(roundtrip.steps) == 2
    assert roundtrip.steps[0].action == "GET /api/v1/users"
    assert roundtrip.token_count > 0


def test_procedural_memory_save_load_list(tmp_path):
    root = str(tmp_path / "store")
    mem1 = ProceduralMemory(
        id="p1",
        task_objective="Task 1",
        progress_status="Done",
        steps=[ProceduralStep(1, "action 1", "output 1")],
        agent_id="bot-a",
    )
    mem2 = ProceduralMemory(
        id="p2",
        task_objective="Task 2",
        progress_status="Working",
        steps=[ProceduralStep(1, "action 2", "output 2")],
        agent_id="bot-b",
    )

    p1_path = save_procedural_memory(root, mem1)
    p2_path = save_procedural_memory(root, mem2)
    assert p1_path.endswith("p1.json")

    loaded = load_procedural_memory(root, "p1")
    assert loaded is not None
    assert loaded.id == "p1"
    assert loaded.task_objective == "Task 1"

    all_items = list_procedural_memories(root)
    assert len(all_items) == 2

    bot_a_items = list_procedural_memories(root, agent_id="bot-a")
    assert len(bot_a_items) == 1
    assert bot_a_items[0]["id"] == "p1"


def test_format_procedural_memory_budget_constraint():
    steps = [
        ProceduralStep(
            step_number=i,
            action=f"Step action {i}",
            result="A" * 500,  # long verbose result
            current_context=f"State after step {i}",
        )
        for i in range(1, 10)
    ]
    mem = ProceduralMemory(
        id="long-mem",
        task_objective="Long multi-step crawl",
        progress_status="90%",
        steps=steps,
    )

    # Without budget
    unbounded = format_procedural_memory(mem)
    assert "Summary of the agent's execution history\n" in unbounded
    assert len(unbounded) > 4000

    # With tight budget
    constrained = format_procedural_memory(mem, token_budget=200)
    assert "(budget-constrained)" in constrained
    assert "[truncated for token budget]" in constrained
    # Recent steps should still have full output
    assert "A" * 500 in constrained


def test_mcp_procedural_tools(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from commontrace import mcp_server

    root = str(tmp_path / "store")
    server = mcp_server.build_server(root)

    res = asyncio.run(
        server.call_tool(
            "procedural_memory_create",
            {
                "task_objective": "Database migration",
                "progress_status": "Step 1 complete",
                "steps": [
                    {"step_number": 1, "action": "Run alembic", "result": "Success"},
                ],
                "agent_id": "migration-bot",
            },
        )
    )
    # FastMCP tools return string/json/dict or CallToolResult
    res_text = res.content[0].text if hasattr(res, "content") else str(res)
    assert "id" in res_text

    replay_res = asyncio.run(
        server.call_tool(
            "procedural_memory_replay",
            {
                "memory_id": list_procedural_memories(root)[0]["id"],
                "token_budget": 1000,
            },
        )
    )
    replay_text = replay_res.content[0].text if hasattr(replay_res, "content") else str(replay_res)
    assert "prompt_context" in replay_text

