import json
import os
import subprocess
import sys

import pytest

from commontrace import frontmatter, paths, trace_io, validate
from commontrace.cli import main
from commontrace.commands import _shellout


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_scaffolds_store_for_any_agent_type(store):
    assert main(["init", "--agent-type", "support", "--dest", str(store)]) == 0
    assert os.path.isdir(store / "memory" / "lessons")
    assert os.path.isdir(store / "memory" / "traces")
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
            "importance": 9,
            "importance_rationale": "x",
            "applies_when": "x",
            "do_not_apply_when": "x",
            "uses": 0,
            "last_hit": "NEVER",
            "source_traces": [],
            "status": "nope",
        },
        "body",
    )
    assert main(["lesson", "validate", str(bad_path), "--dest", str(store)]) == 1


def test_lesson_validate_accepts_a_v1_lesson_with_only_source_episodes(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    v1_path = store / "memory" / "lessons" / "lesson_v1_legacy.md"
    frontmatter.write(
        str(v1_path),
        {
            "name": "v1_legacy",
            "description": "A lesson written before source_traces existed",
            "tags": ["legacy"],
            "agent_type": "code",
            "domain": "testing",
            "importance": 3,
            "importance_rationale": "x",
            "applies_when": "x",
            "do_not_apply_when": "x",
            "uses": 0,
            "last_hit": "NEVER",
            "source_episodes": ["2025-01-01_example.md"],
            "status": "active",
        },
        "body",
    )
    assert main(["lesson", "validate", str(v1_path), "--dest", str(store)]) == 0


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
    assert not (store / "evil.md").exists()
    assert list((store / "memory" / "lessons").glob("*evil*")) == []


def test_validate_catches_invalid_nested_outcome_fields(store):
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
    schema = validate.load_schema("trace.schema.json")
    instance = {
        "id": "x", "title": "t", "context_text": "c", "solution_text": "s",
        "tags": [], "agent_type": "code", "trust": True,
    }
    errors = validate.validate(instance, schema)
    assert any("trust" in e for e in errors)


def test_install_claude_code_finds_skill_md_via_dest(store, monkeypatch):
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
    assert main(["install", "--target", "generic-mcp", "--dest", str(store)]) == 0
    example = store / "commontrace.hub.mcp.json.example"
    doc = json.loads(example.read_text())
    assert "mcpServers" in doc
    assert "commontrace" in doc["mcpServers"]

    entry = doc["mcpServers"]["commontrace"]
    assert entry["type"] == "http"
    assert entry["url"].endswith("/mcp")
    assert entry["headers"]["Authorization"].startswith("Bearer ")
    assert "command" not in entry


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
    capsys.readouterr()
    assert main(["install", "--target", "generic", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] overwriting existing file" in out
    assert "COMMONTRACE.md" in out


def test_doctor_runs_without_crashing(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    assert main(["doctor", "--dest", str(store)]) == 0


def test_a_missing_pyyaml_install_is_a_clean_error_not_a_traceback():
    script = (
        "import sys\n"
        "class _Blocker:\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name == 'yaml':\n"
        "            raise ModuleNotFoundError(f\"No module named {name!r}\", name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Blocker())\n"
        "from commontrace.cli import main\n"
        "sys.exit(main(['doctor']))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "Traceback" not in result.stderr
    assert "yaml" in result.stderr
    assert "not installed correctly" in result.stderr


def test_paths_env_var_override(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_ROOT", str(tmp_path))
    assert paths.resolve_root() == str(tmp_path)


def test_subprocess_handoff_passes_the_resolved_root_not_the_inherited_env(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, env=None):
        captured["root"] = env["COMMONTRACE_ROOT"]
        return subprocess.CompletedProcess(cmd, 0)

    script = tmp_path / "script.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setenv("COMMONTRACE_ROOT", "/some/other/store")
    monkeypatch.setattr(_shellout.subprocess, "run", fake_run)
    monkeypatch.setattr(_shellout, "find_reference_script", lambda root, rel: str(script))

    assert _shellout.run_script(str(tmp_path), "whatever.py", [], "hint") == 0
    assert captured["root"] == str(tmp_path)
    assert captured["root"] != "/some/other/store"


def test_captured_subprocess_output_is_pinned_to_utf8(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, env=None, stdout=None, text=None, encoding=None, errors=None):
        captured.update(env=env, stdout=stdout, text=text, encoding=encoding, errors=errors)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok")

    script = tmp_path / "script.py"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(_shellout.subprocess, "run", fake_run)
    monkeypatch.setattr(_shellout, "find_reference_script", lambda root, rel: str(script))

    rc, out = _shellout.run_script(str(tmp_path), "whatever.py", [], "hint", capture=True)
    assert rc == 0
    assert out == "ok"
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["env"]["PYTHONUTF8"] == "1"


def test_sync_without_hub_configured_prints_setup_instructions(store, capsys, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_HUB_URL", raising=False)
    monkeypatch.delenv("COMMONTRACE_HUB_API_KEY", raising=False)
    assert main(["sync", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "No Hub is configured" in out
    assert "COMMONTRACE_HUB_URL" in out


def test_sync_warns_when_api_key_passed_on_the_command_line(store, capsys, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_HUB_URL", raising=False)
    monkeypatch.delenv("COMMONTRACE_HUB_API_KEY", raising=False)
    assert main(["sync", "--dest", str(store), "--hub-api-key", "ct_live_test"]) == 0
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "--hub-api-key" in err


def test_query_lexical_finds_matching_lesson(store, capsys):
    main(["init", "--agent-type", "sales", "--dest", str(store)])
    main(
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
            "--importance-rationale", "x",
            "--dest", str(store),
        ]
    )
    lesson_path = store / "memory" / "lessons" / "lesson_handle_price_objection.md"
    fm, _ = frontmatter.read(str(lesson_path))
    frontmatter.write(
        str(lesson_path),
        fm,
        "## Rule\nReframe the price objection around ROI rather than a discount.\n\n"
        "## Why\nDiscounting first trains prospects to always ask.\n\n"
        "## How to apply\nQuantify payback period, then restate the price.\n\n"
        "## Counter-examples\nDoes not apply before a demo.\n",
    )
    assert main(["lesson", "approve", "lesson_handle_price_objection", "--dest", str(store)]) == 0

    capsys.readouterr()
    rc = main(["query", "prospect is objecting to price", "--lexical", "--dest", str(store)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lesson_handle_price_objection" in out


def test_query_lexical_reports_no_matches_cleanly(store, capsys):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    capsys.readouterr()
    rc = main(["query", "something nobody has a lesson about", "--lexical", "--dest", str(store)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "empty" in out
    assert "commontrace capture" in out


def test_query_rejects_a_negative_top_k(store, capsys):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["query", "anything", "--lexical", "--top-k", "-1", "--dest", str(store)])
    assert exc.value.code != 0
    assert "--top-k must be >= 1" in capsys.readouterr().err


def test_query_lexical_excludes_review_status_lessons(store, capsys):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    main(
        [
            "lesson", "new",
            "--slug", "lesson_pending_review",
            "--description", "candidate lesson about widgets",
            "--agent-type", "code",
            "--domain", "other",
            "--dest", str(store),
        ]
    )
    lesson_path = store / "memory" / "lessons" / "lesson_pending_review.md"
    fm, body = frontmatter.read(str(lesson_path))
    fm["status"] = "review"
    frontmatter.write(str(lesson_path), fm, body)

    capsys.readouterr()
    rc = main(["query", "widgets", "--lexical", "--dest", str(store)])
    assert rc == 0
    out = capsys.readouterr().out

    result_lines = [ln for ln in out.splitlines() if "rel=" in ln]
    assert not any("lesson_pending_review" in ln for ln in result_lines), (
        f"an unapproved lesson was served as a result: {result_lines}"
    )
    assert "status=review" in out
    assert "commontrace lesson approve lesson_pending_review" in out


def test_sync_partial_hub_config_still_prints_setup_instructions(store, capsys, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_HUB_URL", "http://localhost:8420/mcp")
    monkeypatch.delenv("COMMONTRACE_HUB_API_KEY", raising=False)
    assert main(["sync", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "No Hub is configured" in out


def test_packaged_schemas_are_identical_to_the_normative_ones():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("trace.schema.json", "lesson.schema.json"):
        normative = os.path.join(repo, "protocol", "schemas", name)
        packaged = os.path.join(repo, "commontrace", "schemas", name)
        with open(normative, encoding="utf-8") as fh:
            a = fh.read()
        with open(packaged, encoding="utf-8") as fh:
            b = fh.read()
        assert a == b, (
            f"{name} differs between protocol/schemas/ and commontrace/schemas/. "
            "Edit the normative copy under protocol/ and copy it across."
        )


def test_skill_description_fits_the_claude_code_limit():
    import yaml as _yaml

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "SKILL.md"), encoding="utf-8") as fh:
        front = fh.read().split("---")[1]
    description = _yaml.safe_load(front)["description"]
    assert len(description) <= 1024, f"SKILL.md description is {len(description)} chars (limit 1024)"


def test_shipped_schemas_use_only_keywords_the_validator_enforces():
    for name in ("trace.schema.json", "lesson.schema.json"):
        validate.assert_supported_schema(validate.load_schema(name))


def test_an_unenforced_keyword_is_rejected_rather_than_ignored():
    with pytest.raises(validate.UnsupportedSchemaError, match="pattern"):
        validate.assert_supported_schema(
            {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[0-9a-f-]+$"}}}
        )


def test_a_permissive_additional_properties_is_accepted_as_a_genuine_no_op():
    validate.assert_supported_schema({"type": "object", "additionalProperties": True})
    with pytest.raises(validate.UnsupportedSchemaError):
        validate.assert_supported_schema({"type": "object", "additionalProperties": False})


class TestListingCommands:
    def _lesson(self, store, name, **overrides):
        fm = {
            "name": name, "description": "d", "tags": ["t"], "agent_type": "support",
            "domain": "other", "importance": 3, "applies_when": "when",
            "do_not_apply_when": "not", "uses": 0, "last_hit": "NEVER",
            "source_traces": [], "status": "active", "importance_rationale": "r",
            "importance_history": [],
        }
        fm.update(overrides)
        import yaml as _yaml
        path = store / "memory" / "lessons" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + _yaml.safe_dump(fm) + "---\n\nbody\n", encoding="utf-8")
        return path

    def test_lists_a_lesson(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        self._lesson(store, "lesson_alpha")
        capsys.readouterr()
        assert main(["lesson", "list", "--dest", str(store)]) == 0
        assert "lesson_alpha" in capsys.readouterr().out

    def test_a_present_but_empty_field_does_not_crash_the_listing(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        self._lesson(store, "lesson_broken", status=None, agent_type=None, description=None)
        capsys.readouterr()
        assert main(["lesson", "list", "--dest", str(store)]) == 0
        assert "lesson_broken" in capsys.readouterr().out

    def test_json_output_is_one_parseable_array(self, store, capsys):
        import datetime
        import json

        main(["init", "--agent-type", "support", "--dest", str(store)])
        self._lesson(store, "lesson_alpha", status="active", last_reviewed=datetime.date(2026, 1, 2))
        self._lesson(store, "lesson_broken", status=None, description=None)
        capsys.readouterr()
        assert main(["lesson", "list", "--json", "--dest", str(store)]) == 0
        rows = json.loads(capsys.readouterr().out)
        by_name = {r["name"]: r for r in rows}
        assert set(by_name) == {"lesson_alpha", "lesson_broken"}
        assert by_name["lesson_alpha"]["status"] == "active"
        assert by_name["lesson_broken"]["description"] == ""
        assert main(["lesson", "list", "--json", "--status", "nope", "--dest", str(store)]) == 0
        assert json.loads(capsys.readouterr().out) == []

    def test_status_filter_selects(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        self._lesson(store, "lesson_active", status="active")
        self._lesson(store, "lesson_review", status="review")
        capsys.readouterr()
        assert main(["lesson", "list", "--status", "review", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "lesson_review" in out and "lesson_active" not in out

    def test_trace_list_survives_an_empty_field(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        path = store / "memory" / "traces" / "2026-01-01_broken.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "---\nid:\nagent_type:\ntitle: still listable\n---\n\n## Context\nc\n\n## Solution\ns\n",
            encoding="utf-8",
        )
        capsys.readouterr()
        assert main(["trace", "list", "--dest", str(store)]) == 0
        assert "still listable" in capsys.readouterr().out


class TestBadPathsAreReportedNotRaised:
    def test_missing_file(self, store, capsys):
        assert main(["lesson", "validate", "/nope/does-not-exist.md", "--dest", str(store)]) == 1
        assert "cannot read" in capsys.readouterr().err

    def test_directory_where_a_file_was_meant(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        assert main(["trace", "validate", str(store / "memory"), "--dest", str(store)]) == 1
        assert "cannot read" in capsys.readouterr().err


class TestCaptureRefusesInvalidTraces:
    def test_a_negative_cost_is_refused_at_write_time(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["capture", "--title", "t", "--context", "c", "--solution", "s",
                   "--tokens-used", "-5", "--dest", str(store)])
        assert rc == 1
        assert "refusing to write an invalid trace" in capsys.readouterr().err
        written = list((store / "memory" / "traces").glob("*.md"))
        assert [p.name for p in written if p.name != "README.md"] == []

    def test_a_valid_trace_still_writes_and_validates(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        assert main(["capture", "--title", "t", "--context", "c", "--solution", "s",
                     "--tokens-used", "10", "--resolved", "--dest", str(store)]) == 0
        capsys.readouterr()
        assert main(["trace", "validate", "--dest", str(store)]) == 0


def test_every_subcommand_module_registers_exactly_its_own_name():
    import argparse
    import pathlib

    from commontrace import cli

    on_disk = sorted(p.stem[:-4] for p in pathlib.Path(cli.__file__).parent.joinpath("commands").glob("*_cmd.py"))
    assert sorted(cli._COMMANDS) == on_disk
    for name in cli._COMMANDS:
        sub = argparse.ArgumentParser().add_subparsers()
        (module,) = cli._command_modules(name)
        module.add_parser(sub)
        assert list(sub.choices) == [name]


def test_a_command_imports_only_what_it_uses():
    script = (
        "import sys\n"
        "from commontrace import cli\n"
        "for name in ('capture', 'query', 'lesson'):\n"
        "    cli.build_parser(name)\n"
        "print(','.join(m for m in ('numpy', 'asyncio', 'ssl', 'commontrace.commands.commons_cmd') if m in sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_a_mistyped_command_suggests_the_one_meant(capsys):
    from commontrace import cli

    assert cli.main(["captur"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'captur'" in err and "Did you mean: capture?" in err
    assert cli.main(["xyzzy"]) == 2
    assert "Did you mean" not in capsys.readouterr().err


def test_no_command_at_all_prints_the_command_list(capsys):
    from commontrace import cli

    assert cli.main([]) == 2
    err = capsys.readouterr().err
    assert "usage:" in err and "capture" in err and "doctor" in err


def test_bare_init_is_general_not_a_business_function(store):
    assert main(["init", "--dest", str(store)]) == 0
    assert (store / "memory" / "INDEX.md").read_text().startswith("# Memory Index — agent_type: general")
    assert not os.path.isdir(store / "memory" / "episodes")
    assert paths.store_agent_type(str(store)) == "general"


def test_init_function_maps_to_agent_type(store):
    assert main(["init", "--function", "coding", "--dest", str(store)]) == 0
    assert paths.store_agent_type(str(store)) == "code"
    assert os.path.isdir(store / "memory" / "episodes")


def test_init_function_custom_takes_the_slug_from_agent_type(store):
    assert main(["init", "--function", "custom", "--agent-type", "legal", "--dest", str(store)]) == 0
    assert paths.store_agent_type(str(store)) == "legal"


def test_init_function_conflicting_with_agent_type_is_refused(store, capsys):
    assert main(["init", "--function", "support", "--agent-type", "sales", "--dest", str(store)]) == 2
    assert "conflicts" in capsys.readouterr().err
    assert not os.path.isdir(store / "memory")

