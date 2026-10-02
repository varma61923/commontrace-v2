"""Tests for the autonomous agent execution loop (M3)."""
from __future__ import annotations

import os

import pytest


@pytest.fixture()
def store(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    from commontrace import paths
    os.makedirs(paths.lessons_dir(str(store)), exist_ok=True)
    os.makedirs(paths.traces_dir(str(store)), exist_ok=True)
    return str(store)


class TestAgentLoop:
    def test_basic_run_completes_and_writes_trace(self, store):
        from commontrace.agent_loop import AgentLoop

        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt="Diagnose the slow database queries")

        assert result.run_id
        assert result.success
        assert len(result.turns) >= 1
        assert result.trace_path is not None
        assert os.path.exists(result.trace_path), f"Trace not written: {result.trace_path}"

    def test_run_with_custom_executor(self, store):
        from commontrace.agent_loop import AgentLoop

        call_count = {"n": 0}

        def my_executor(context, tool_calls):
            call_count["n"] += 1
            return "I have completed the task.", [{"type": "done", "done": True}]

        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt="Fix the bug", tool_executor=my_executor)

        assert result.success
        assert call_count["n"] == 1
        assert "completed the task" in result.final_answer

    def test_run_updates_memory_blocks(self, store):
        from commontrace import memory_blocks
        from commontrace.agent_loop import AgentLoop

        # Pre-create a block
        memory_blocks.set_block(store, "persona", "You are a senior engineer.", actor="test")

        def block_updater(context, tool_calls):
            return "Done.", [
                {"type": "memory_block_update", "name": "persona", "content": "Senior engineer focused on payments."},
                {"type": "done", "done": True},
            ]

        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt="Update persona for payments work", tool_executor=block_updater)

        assert result.blocks_updated >= 1
        block = memory_blocks.get_block(store, "persona")
        assert "payments" in block.content

    def test_run_records_facts(self, store):
        from commontrace import hierarchical
        from commontrace.agent_loop import AgentLoop

        def fact_recorder(context, tool_calls):
            return "Fact recorded.", [
                {"type": "fact_record", "statement": "Database pool must not exceed 100 connections.",
                 "category": "constraint", "confidence": 0.9},
                {"type": "done", "done": True},
            ]

        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt="Record infrastructure constraint", tool_executor=fact_recorder)

        assert result.facts_recorded >= 1
        facts = hierarchical.list_facts(store, status="active")
        assert any("100 connections" in f.statement for f in facts)

    def test_run_trace_contains_prompt_context(self, store):
        from commontrace.agent_loop import AgentLoop

        prompt = "Investigate latency spike in payment processing pipeline"
        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt=prompt)

        assert result.trace_path
        content = open(result.trace_path, encoding="utf-8").read()
        assert "payment processing" in content.lower() or "Agent" in content

    def test_max_turns_respected(self, store):
        from commontrace.agent_loop import AgentLoop

        turn_count = {"n": 0}

        def infinite_executor(context, tool_calls):
            turn_count["n"] += 1
            return f"Still working, turn {turn_count['n']}.", []  # never done

        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt="Infinite task", tool_executor=infinite_executor, max_turns=3)

        assert len(result.turns) == 3
        assert turn_count["n"] == 3
