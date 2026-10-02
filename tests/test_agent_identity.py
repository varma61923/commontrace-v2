from __future__ import annotations

import os

import pytest

from commontrace import frontmatter, trace_io, validate
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    return tmp_path


def _only_trace(store):
    tdir = store / "memory" / "traces"
    paths = [p for p in tdir.glob("*.md") if p.name != "README.md"]
    assert len(paths) == 1
    return trace_io.read(str(paths[0]))[0]


def _capture(store, *extra):
    return main([
        "capture", "--title", "Refund delayed", "--context", "customer asked twice",
        "--solution", "state the window up front", "--agent-type", "support",
        "--dest", str(store), *extra,
    ])


class TestCaptureAgentId:
    def test_agent_id_is_recorded_when_given(self, store):
        assert _capture(store, "--agent-id", "support-worker-7") == 0
        assert _only_trace(store)["agent_id"] == "support-worker-7"

    def test_agent_id_defaults_to_empty_and_stays_valid(self, store):
        assert _capture(store) == 0
        instance = _only_trace(store)
        assert instance["agent_id"] == ""
        assert validate.validate(instance, validate.load_schema("trace.schema.json")) == []

    def test_two_agents_of_the_same_type_are_distinguishable(self, store):
        _capture(store, "--agent-id", "worker-a")
        main([
            "capture", "--title", "Another", "--context", "ctx", "--solution", "fix",
            "--agent-type", "support", "--agent-id", "worker-b", "--dest", str(store),
        ])
        tdir = store / "memory" / "traces"
        ids = set()
        types = set()
        for p in tdir.glob("*.md"):
            if p.name == "README.md":
                continue
            fm, _ = frontmatter.read(str(p))
            ids.add(fm.get("agent_id"))
            types.add(fm.get("agent_type"))
        assert ids == {"worker-a", "worker-b"}
        assert types == {"support"}

    def test_an_oversized_agent_id_is_refused_at_capture_not_at_sync(self, store, capsys):
        rc = _capture(store, "--agent-id", "x" * 129)
        assert rc != 0
        assert "maxLength" in capsys.readouterr().err
        tdir = store / "memory" / "traces"
        assert [p for p in tdir.glob("*.md") if p.name != "README.md"] == []

    def test_an_agent_id_at_exactly_the_limit_is_accepted(self, store):
        assert _capture(store, "--agent-id", "x" * 128) == 0


class TestValidatorMaxLength:
    def test_over_the_limit_is_an_error(self):
        errors = validate.validate({"s": "abcd"}, {"properties": {"s": {"type": "string", "maxLength": 3}}})
        assert len(errors) == 1
        assert "maxLength=3" in errors[0]

    def test_at_and_under_the_limit_pass(self):
        schema = {"properties": {"s": {"type": "string", "maxLength": 3}}}
        assert validate.validate({"s": "abc"}, schema) == []
        assert validate.validate({"s": "ab"}, schema) == []

    def test_it_composes_with_minlength(self):
        schema = {"properties": {"s": {"type": "string", "minLength": 2, "maxLength": 4}}}
        assert validate.validate({"s": "abc"}, schema) == []
        assert len(validate.validate({"s": "a"}, schema)) == 1
        assert len(validate.validate({"s": "abcde"}, schema)) == 1

    def test_the_keyword_is_registered_as_supported(self):
        assert "maxLength" in validate._SUPPORTED_KEYWORDS
        validate.assert_supported_schema(validate.load_schema("trace.schema.json"))


class TestHubClientForwardsAgentId:
    def test_sync_payload_includes_agent_id(self, store, monkeypatch):
        import asyncio

        from commontrace import frontmatter, hub_client, paths

        ldir = paths.lessons_dir(str(store))
        fm = {
            "name": "lesson_a", "description": "d", "tags": [], "agent_type": "support",
            "domain": "testing", "importance": 3, "importance_rationale": "r",
            "importance_history": [], "applies_when": "when", "do_not_apply_when": "never",
            "uses": 0, "last_hit": "NEVER", "source_traces": [], "source_episodes": [],
            "hub_trace_id": None, "status": "active", "agent_id": "support-worker-7",
        }
        frontmatter.write(os.path.join(ldir, "lesson_a.md"), fm, "## Rule\nx\n")

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append(arguments)
            return {"id": "trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1
        assert calls[0]["agent_id"] == "support-worker-7"

    def test_sync_payload_defaults_agent_id_to_empty_string(self, store, monkeypatch):
        import asyncio

        from commontrace import frontmatter, hub_client, paths

        ldir = paths.lessons_dir(str(store))
        fm = {
            "name": "lesson_b", "description": "d", "tags": [], "agent_type": "support",
            "domain": "testing", "importance": 3, "importance_rationale": "r",
            "importance_history": [], "applies_when": "when", "do_not_apply_when": "never",
            "uses": 0, "last_hit": "NEVER", "source_traces": [], "source_episodes": [],
            "hub_trace_id": None, "status": "active",
        }
        frontmatter.write(os.path.join(ldir, "lesson_b.md"), fm, "## Rule\nx\n")

        calls = []

        async def fake_call_tool(hub_url, api_key, name, arguments, **kw):
            calls.append(arguments)
            return {"id": "trace-1", "quarantined": False}

        monkeypatch.setattr(hub_client, "_call_tool", fake_call_tool)
        asyncio.run(hub_client.push_active_lessons("http://localhost:8420/mcp", "key", str(store)))

        assert len(calls) == 1
        assert calls[0]["agent_id"] == ""
