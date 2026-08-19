"""Smoke + contract tests for the `commontrace` CLI (protocol/PROTOCOL.md client)."""
import json
import os

import pytest

from commontrace import frontmatter, paths, trace_io, validate
from commontrace.cli import main


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_scaffolds_store_for_any_agent_type(store):
    assert main(["init", "--agent-type", "support", "--dest", str(store)]) == 0
    assert os.path.isdir(store / "memory" / "lessons")
    assert os.path.isdir(store / "memory" / "traces")
    # code-review profile's episodes/ dir is code-specific, not created for other agent types
    assert not os.path.isdir(store / "memory" / "episodes")
    assert os.path.isfile(store / "memory" / "INDEX.md")


def test_init_creates_episodes_dir_for_code_agent_type(store):
    assert main(["init", "--agent-type", "code", "--dest", str(store)]) == 0
    assert os.path.isdir(store / "memory" / "episodes")


def test_lesson_new_then_validate_round_trips(store):
    main(["init", "--agent-type", "sales", "--dest", str(store)])
    rc = main(
        [
            "lesson", "new",
            "--slug", "lesson_handle_price_objection",
            "--description", "Reframe price objections around ROI, not discount",
            "--agent-type", "sales",
            "--domain", "objection-handling",
            "--tags", "pricing,objection",
            "--applies-when", "prospect objects to price after seeing a demo",
            "--do-not-apply-when", "prospect has not seen the product value yet",
            "--importance", "4",
            "--importance-rationale", "Leading with discount trains prospects to always ask for one",
            "--dest", str(store),
        ]
    )
    assert rc == 0
    lesson_path = store / "memory" / "lessons" / "lesson_handle_price_objection.md"
    assert lesson_path.is_file()

    fm, body = frontmatter.read(str(lesson_path))
    assert fm["agent_type"] == "sales"
    assert fm["importance"] == 4
    assert "## Rule" in body

    schema = validate.load_schema("lesson.schema.json")
    assert validate.validate(fm, schema) == []

    assert main(["lesson", "validate", "--dest", str(store)]) == 0


def test_lesson_validate_catches_schema_violations(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    bad_path = store / "memory" / "lessons" / "lesson_bad.md"
    frontmatter.write(
        str(bad_path),
        {
            "name": "bad",
            "description": "x",
            "tags": [],
            "agent_type": "code",
            "domain": "testing",
            "importance": 9,  # out of range [1,5]
            "importance_rationale": "x",
            "applies_when": "x",
            "do_not_apply_when": "x",
            "uses": 0,
            "last_hit": "NEVER",
            "source_traces": [],
            "status": "nope",  # not in enum
        },
        "body",
    )
    assert main(["lesson", "validate", str(bad_path), "--dest", str(store)]) == 1


def test_capture_writes_a_schema_valid_trace(store):
    main(["init", "--agent-type", "support", "--dest", str(store)])
    rc = main(
        [
            "capture",
            "--title", "Refund before apology increases churn",
            "--context", "Agent offered a refund immediately without acknowledging frustration",
            "--solution", "Acknowledge the issue first, then offer the remedy",
            "--tags", "refunds,tone",
            "--agent-type", "support",
            "--dest", str(store),
        ]
    )
    assert rc == 0
    traces = list((store / "memory" / "traces").glob("*.md"))
    traces = [p for p in traces if p.name != "README.md"]
    assert len(traces) == 1

    instance, body = trace_io.read(str(traces[0]))
    schema = validate.load_schema("trace.schema.json")
    assert validate.validate(instance, schema) == []
    assert "Agent offered a refund" in body
    assert instance["context_text"].startswith("Agent offered a refund")
    assert instance["solution_text"].startswith("Acknowledge the issue first")

    assert main(["trace", "validate", "--dest", str(store)]) == 0


def test_capture_with_outcome_flags_writes_pilot_metrics_fields(store):
    main(["init", "--agent-type", "support", "--dest", str(store)])
    rc = main(
        [
            "capture",
            "--title", "Escalated ticket resolved after lesson injection",
            "--context", "Customer threatened to churn",
            "--solution", "Applied the de-escalation lesson before replying",
            "--agent-type", "support",
            "--resolved",
            "--escalated",
            "--frustration",
            "--tokens-used", "420",
            "--llm-calls", "3",
            "--baseline",
            "--dest", str(store),
        ]
    )
    assert rc == 0
    traces = [p for p in (store / "memory" / "traces").glob("*.md") if p.name != "README.md"]
    assert len(traces) == 1

    instance, _ = trace_io.read(str(traces[0]))
    assert instance["outcome"] == {
        "resolved": True,
        "escalated": True,
        "frustration_signal": True,
        "tokens_used": 420,
        "llm_calls": 3,
        "baseline": True,
    }

    schema = validate.load_schema("trace.schema.json")
    assert validate.validate(instance, schema) == []


def test_capture_without_outcome_flags_omits_outcome_field(store):
    main(["init", "--agent-type", "support", "--dest", str(store)])
    main(
        [
            "capture",
            "--title", "Plain capture",
            "--context", "ctx",
            "--solution", "sol",
            "--agent-type", "support",
            "--dest", str(store),
        ]
    )
    traces = [p for p in (store / "memory" / "traces").glob("*.md") if p.name != "README.md"]
    instance, _ = trace_io.read(str(traces[0]))
    assert "outcome" not in instance


def test_lesson_new_rejects_path_traversal_slug(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    rc = main(
        [
            "lesson", "new",
            "--slug", "../../evil",
            "--description", "x",
            "--agent-type", "code",
            "--domain", "testing",
            "--dest", str(store),
        ]
    )
    assert rc == 1
    # Nothing should have been written outside (or inside) the store. ldir="<store>/memory/lessons",
    # so "../../evil.md" would land at "<store>/evil.md" if the traversal weren't blocked.
    assert not (store / "evil.md").exists()
    assert list((store / "memory" / "lessons").glob("*evil*")) == []


def test_validate_catches_invalid_nested_outcome_fields(store):
    """Regression: validate.py must recurse into Trace.outcome's own properties, not just
    check that outcome is a dict."""
    main(["init", "--agent-type", "code", "--dest", str(store)])
    bad_path = store / "memory" / "traces" / "bad.md"
    frontmatter.write(
        str(bad_path),
        {
            "id": "x", "title": "t", "agent_type": "code",
            "outcome": {"resolved": "not-a-boolean", "tokens_used": -50},
        },
        "## Context\nc\n\n## Solution\ns\n",
    )
    assert main(["trace", "validate", str(bad_path), "--dest", str(store)]) == 1


def test_validate_rejects_bool_for_number_typed_field(store):
    """Regression: _check_type must exclude bool from 'number', not just 'integer',
    or minimum/maximum bounds checking is silently skipped for True/False."""
    schema = validate.load_schema("trace.schema.json")
    instance = {
        "id": "x", "title": "t", "context_text": "c", "solution_text": "s",
        "tags": [], "agent_type": "code", "trust": True,
    }
    errors = validate.validate(instance, schema)
    assert any("trust" in e for e in errors)


def test_install_claude_code_finds_skill_md_via_dest(store, monkeypatch):
    """Regression: install --dest must be checked when looking for a real SKILL.md,
    not just COMMONTRACE_ROOT/cwd."""
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    (store / "SKILL.md").write_text("# Real skill content for the dest project\n" * 50)
    other_cwd = store.parent / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    assert main(["install", "--target", "claude-code", "--dest", str(store)]) == 0
    out = store / ".claude" / "skills" / "commontrace" / "SKILL.md"
    assert out.is_file()
    assert "Real skill content" in out.read_text()


def test_capture_can_record_a_definite_negative_for_every_outcome_flag(store):
    """Regression: --escalated/--repeated-error/--frustration originally had no
    --not-* counterpart (unlike --resolved/--not-resolved), so there was no way to
    record "definitely not escalated" -- only "escalated" or "unknown". Since a rate's
    denominator only counts traces where the field was actually set, this made
    escalation_rate/repeated_error_rate/frustration_rate degenerate to N/A or 100%,
    never a real number reflecting actual performance."""
    main(["init", "--agent-type", "support", "--dest", str(store)])
    rc = main(
        [
            "capture",
            "--title", "definitely fine", "--context", "c", "--solution", "s",
            "--agent-type", "support",
            "--not-resolved", "--not-escalated", "--not-repeated-error", "--not-frustration",
            "--dest", str(store),
        ]
    )
    assert rc == 0
    traces = [p for p in (store / "memory" / "traces").glob("*.md") if p.name != "README.md"]
    instance, _ = trace_io.read(str(traces[0]))
    assert instance["outcome"] == {
        "resolved": False,
        "escalated": False,
        "repeated_error": False,
        "frustration_signal": False,
    }
    schema = validate.load_schema("trace.schema.json")
    assert validate.validate(instance, schema) == []


def test_install_generic_target_writes_pointer_doc(store):
    assert main(["install", "--target", "generic", "--dest", str(store)]) == 0
    assert (store / "COMMONTRACE.md").is_file()


def test_install_claude_code_copies_real_skill_md_when_found(store, monkeypatch):
    # Simulate running from within a checkout that has SKILL.md at its root.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monkeypatch.setenv("COMMONTRACE_ROOT", repo_root)
    assert main(["install", "--target", "claude-code", "--dest", str(store)]) == 0
    out = store / ".claude" / "skills" / "commontrace" / "SKILL.md"
    assert out.is_file()
    assert out.stat().st_size > 1000


def test_install_generic_mcp_warns_about_credentials_in_gitignore(store, capsys):
    assert main(["install", "--target", "generic-mcp", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert ".gitignore" in out
    example = store / "commontrace.hub.mcp.json.example"
    assert example.is_file()
    assert ".gitignore" in example.read_text()


def test_install_generic_mcp_writes_valid_json_with_correct_mcp_servers_shape(store):
    """Regression: the generated commontrace.hub.mcp.json.example previously interpolated
    a quoted tool list straight into a JSON string field via an f-string template, so the
    unescaped quotes broke JSON parsing. It must be valid JSON with the real mcpServers
    shape used by Claude Code / Cursor / Windsurf MCP client configs."""
    assert main(["install", "--target", "generic-mcp", "--dest", str(store)]) == 0
    example = store / "commontrace.hub.mcp.json.example"
    doc = json.loads(example.read_text())
    assert "mcpServers" in doc
    assert "commontrace" in doc["mcpServers"]
    assert "command" in doc["mcpServers"]["commontrace"]


def test_install_cursor_writes_valid_json_mcp_example(store):
    assert main(["install", "--target", "cursor", "--dest", str(store)]) == 0
    example = store / "commontrace.hub.mcp.json.example"
    doc = json.loads(example.read_text())
    assert "commontrace" in doc["mcpServers"]


def test_install_cursor_warns_about_credentials_in_gitignore(store, capsys):
    assert main(["install", "--target", "cursor", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert ".gitignore" in out


def test_install_warns_before_overwriting_existing_generated_file(store, capsys):
    assert main(["install", "--target", "generic", "--dest", str(store)]) == 0
    capsys.readouterr()  # discard first-run output
    assert main(["install", "--target", "generic", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] overwriting existing file" in out
    assert "COMMONTRACE.md" in out


def test_doctor_runs_without_crashing(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    assert main(["doctor", "--dest", str(store)]) == 0


def test_paths_env_var_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_ROOT", str(tmp_path))
    assert paths.resolve_root() == str(tmp_path)
