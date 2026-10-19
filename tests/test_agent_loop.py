from __future__ import annotations

import io
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

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
            return f"Still working, turn {turn_count['n']}.", []

        loop = AgentLoop(root=store, dream_every=0)
        result = loop.run(prompt="Infinite task", tool_executor=infinite_executor, max_turns=3)

        assert len(result.turns) == 3
        assert turn_count["n"] == 3

    def test_concurrent_dream_triggers_do_not_redirect_process_streams(self, store, monkeypatch):
        import time

        from commontrace import agent_loop

        # The previous in-process dream call replaced sys.stdout/stderr while it
        # ran. Keep the schedule concurrent and check every independent write.
        output = io.StringIO()
        monkeypatch.setattr(sys, "stdout", output)
        calls = []

        def isolated_run(args, **kwargs):
            calls.append((args, kwargs))
            assert kwargs["stdout"] == subprocess.DEVNULL
            assert kwargs["stderr"] == subprocess.DEVNULL
            time.sleep(.01)
            return subprocess.CompletedProcess(args, 0)

        monkeypatch.setattr(agent_loop.subprocess, "run", isolated_run)
        loop = agent_loop.AgentLoop(store, dream_every=2)

        def run_agent(index):
            return loop.run(f"task {index}", tool_executor=lambda context, history: ("done", [{"done": True}]))

        def write_output():
            for index in range(80):
                print(f"worker-output-{index}")

        with ThreadPoolExecutor(max_workers=5) as pool:
            writer = pool.submit(write_output)
            list(pool.map(run_agent, range(4)))
            writer.result()

        lines = output.getvalue().splitlines()
        assert lines == [f"worker-output-{index}" for index in range(80)]
        assert loop._run_count == 4
        assert len(calls) == 2

    def test_recalled_facts_and_lessons_use_safe_quoted_labels(self, store, monkeypatch):
        from commontrace import agent_loop, frontmatter, hierarchical, lesson_cache, memory_ifc, retrieval, retrieval_io

        labels = []

        def quote(root, item, *, identity=None):
            labels.append(identity)
            return {**item, "presentation": "quoted-data", "quoted_text": "> " + item["text"]}

        monkeypatch.setattr(memory_ifc, "safe_label", quote)
        lesson_path = os.path.join(store, "lesson.md")
        ranked_lesson = SimpleNamespace(path=lesson_path, slug="external-lesson")
        monkeypatch.setattr(lesson_cache, "load_active_with_terms", lambda *args, **kwargs: ([ranked_lesson], None))
        monkeypatch.setattr(lesson_cache, "filter_eligible", lambda values: values)
        monkeypatch.setattr(retrieval_io, "load_config", lambda _root: SimpleNamespace(floor=0, scorer=None))
        monkeypatch.setattr(retrieval, "rank_lessons", lambda *args, **kwargs: [ranked_lesson])
        monkeypatch.setattr(frontmatter, "read", lambda _path: ({"description": "lesson source data"}, "body"))
        monkeypatch.setattr(lesson_cache, "fresh_eligible", lambda *args, **kwargs: True)
        lessons = agent_loop._relevant_lessons(store, "lesson")
        assert lessons == [("external-lesson", "> lesson source data")]

        fact = SimpleNamespace(id="external-fact", statement="fact source data", scopes=[], confidence=.8)
        monkeypatch.setattr(hierarchical, "search_facts", lambda *args, **kwargs: [(fact, 1.0)])
        monkeypatch.setattr(agent_loop, "_relevant_lessons", lambda *args: [])
        context = agent_loop._assemble_context(store, "fact")
        assert "- > fact source data" in context
        assert labels == ["external-lesson", "external-fact"]
