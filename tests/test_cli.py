"""Smoke + contract tests for the `commontrace` CLI (protocol/PROTOCOL.md client)."""
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


def test_lesson_validate_accepts_a_v1_lesson_with_only_source_episodes(store):
    """[BUG-PROT-01]: PROTOCOL.md's versioning section promises "a Trace/
    Lesson written under v1.x remains valid under 2.0.0", but a v1 lesson
    uses `source_episodes` (the pre-2.0 field name) and has no
    `source_traces` key at all -- lesson.schema.json's `required` list used
    to demand `source_traces` unconditionally, which failed every such
    lesson with "'source_traces' is a required property" the moment
    `lesson validate` (or the Hub-side equivalent) touched it."""
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
            "source_episodes": ["2025-01-01_example.md"],  # v1 field -- no source_traces at all
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

    entry = doc["mcpServers"]["commontrace"]
    # The Hub serves streamable-HTTP MCP (hub/main.py -> HUB_HOST:HUB_PORT/mcp).
    # This template previously emitted the stdio shape (command/args/env), which
    # has nowhere to put an endpoint or a bearer token -- so anyone who pasted
    # it simply could not connect to the server this repo ships.
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
    capsys.readouterr()  # discard first-run output
    assert main(["install", "--target", "generic", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "[WARN] overwriting existing file" in out
    assert "COMMONTRACE.md" in out


def test_doctor_runs_without_crashing(store):
    main(["init", "--agent-type", "code", "--dest", str(store)])
    assert main(["doctor", "--dest", str(store)]) == 0


def test_a_missing_pyyaml_install_is_a_clean_error_not_a_traceback():
    """[BUG-CLI-01]: every subcommand module is imported at the top of
    cli.py, and several transitively import commontrace.frontmatter, which
    does a hard `import yaml`. PyYAML is a required dependency
    (pyproject.toml), so this only bites a broken/incomplete install -- but
    when it does, the ModuleNotFoundError fires at module-import time,
    before main()'s own try/except is ever reached, so `commontrace doctor`
    (whose whole job is diagnosing exactly this) could never even run: it
    crashed with a raw traceback instead of doctor's own clean
    "[WARN] PyYAML importable" line.

    Run out-of-process (rather than juggling sys.modules/meta_path in this
    test's own interpreter) so the module-import-time failure is exercised
    for real, and so it can never leak a fake `yaml` blocker into any other
    test's import state.
    """
    # find_spec, not the legacy find_module/load_module finder protocol:
    # the latter was deprecated since Python 3.4 and its import-system
    # fallback support was removed in 3.12, so a find_module-based blocker
    # is silently never consulted there -- `import yaml` then succeeds
    # normally and this whole scenario never triggers, which is exactly
    # what happened the first time this test shipped (green on 3.10/3.11,
    # red on 3.12 in CI).
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
    """--dest must beat an exported COMMONTRACE_ROOT across the subprocess boundary.

    resolve_root() already applies the documented precedence (paths.py: flag,
    then env, then cwd) on the parent side, so `root` here is the winner. The
    child previously inherited the *old* env value because run_script used
    setdefault, which silently discarded an explicit --dest. The failure was
    invisible -- `bench --pilot --dest B` rendered a normal report full of
    store A's numbers -- so this asserts the handoff, not just resolve_root.
    """
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
    """text=True alone decodes the pipe using the host locale
    (locale.getpreferredencoding(False)), which on Windows is commonly a
    legacy codepage rather than UTF-8 -- and the child's own stdout
    defaults to that same locale-dependent encoding when redirected to a
    pipe. Every reference script here writes UTF-8 in practice (lesson/
    trace content routinely contains non-ASCII text), so both ends of the
    pipe must be pinned to UTF-8 explicitly rather than left to whatever
    the host locale happens to be."""
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
    """A CLI argument is readable by any local user via `ps`/
    /proc/<pid>/cmdline and can land in shell history / auditd's
    process-exec logs -- none of which apply to COMMONTRACE_HUB_API_KEY.
    --help already recommends the env var; this is the same warning at
    the moment someone actually uses the flag."""
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
    # `lesson new` scaffolds at status=review (the Validator step activates a
    # lesson, never the thing that proposed it), and lexical query only reads
    # ACTIVE lessons -- so write the rule and approve it, which is the real
    # path from a scaffolded lesson to a retrievable one.
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
    """Exits 0 and SAYS something useful, rather than failing or going
    silent.

    This used to assert the literal string "no lexical matches", which was
    the same sentence the CLI printed for four different situations --
    including a freshly initialised store, where it was accompanied by
    "try `commontrace lesson list`" and that list is empty. The store here
    is exactly that case, so the assertion now checks what the message is
    FOR: naming the state and a command that moves it forward.
    """
    main(["init", "--agent-type", "code", "--dest", str(store)])
    capsys.readouterr()
    rc = main(["query", "something nobody has a lesson about", "--lexical", "--dest", str(store)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "empty" in out
    assert "commontrace capture" in out


def test_query_rejects_a_negative_top_k(store, capsys):
    """`order[:top_k]` is a Python slice, not a bounds check --
    `order[:-1]` means "all but the last", not "nothing" -- so
    `--top-k -1` used to silently return nearly the whole ranked list
    instead of failing. argparse now rejects it at parse time."""
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

    # The guarantee is that a review lesson is never SERVED -- not that its
    # name never appears on screen. `query` now explains why a store with
    # only review lessons returns nothing, and that explanation ends in
    # `commontrace lesson approve lesson_pending_review`, because naming
    # the real slug is the entire value of the message.
    #
    # So this asserts the guarantee directly rather than through a
    # substring that the guidance also trips: a served lesson prints as a
    # result line carrying its relevance score, and no such line may exist
    # for a lesson still under review.
    result_lines = [ln for ln in out.splitlines() if "rel=" in ln]
    assert not any("lesson_pending_review" in ln for ln in result_lines), (
        f"an unapproved lesson was served as a result: {result_lines}"
    )
    # ...and the explanation is still the one for this state, so a future
    # change that stopped serving it for the WRONG reason would show up.
    assert "status=review" in out
    assert "commontrace lesson approve lesson_pending_review" in out


def test_sync_partial_hub_config_still_prints_setup_instructions(store, capsys, monkeypatch):
    # A URL with no key (or vice versa) is not enough to attempt a connection --
    # sync must not try to talk to a Hub with half a credential.
    monkeypatch.setenv("COMMONTRACE_HUB_URL", "http://localhost:8420/mcp")
    monkeypatch.delenv("COMMONTRACE_HUB_API_KEY", raising=False)
    assert main(["sync", "--dest", str(store)]) == 0
    out = capsys.readouterr().out
    assert "No Hub is configured" in out


def test_packaged_schemas_are_identical_to_the_normative_ones():
    """protocol/schemas/ is what implementers read; commontrace/schemas/ is what
    the validator enforces. They are committed twice so a bare `pip install`
    can validate without a repo checkout, and nothing else keeps them equal.

    Without this test the next schema edit lands in one copy and the spec and
    the tool disagree silently -- the validator would enforce a constraint the
    published spec does not state, or vice versa.
    """
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
    """A skill whose description exceeds 1024 characters does not load, which
    would break `install --target claude-code` for every downstream user with
    nothing in the diff to catch it at review time."""
    import yaml as _yaml

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "SKILL.md"), encoding="utf-8") as fh:
        front = fh.read().split("---")[1]
    description = _yaml.safe_load(front)["description"]
    assert len(description) <= 1024, f"SKILL.md description is {len(description)} chars (limit 1024)"


def test_shipped_schemas_use_only_keywords_the_validator_enforces():
    """commontrace/validate.py implements a deliberate subset of JSON Schema.

    The risk is not today's schemas -- it is the next edit. Adding `pattern`
    to constrain Trace.id to a UUID, or `maxLength` to description, are
    natural things to want, and the validator would accept literally anything
    for that field while still reporting the document valid. This makes that
    a loud failure at the moment the schema widens.
    """
    for name in ("trace.schema.json", "lesson.schema.json"):
        validate.assert_supported_schema(validate.load_schema(name))


def test_an_unenforced_keyword_is_rejected_rather_than_ignored():
    with pytest.raises(validate.UnsupportedSchemaError, match="pattern"):
        validate.assert_supported_schema(
            {"type": "object", "properties": {"id": {"type": "string", "pattern": "^[0-9a-f-]+$"}}}
        )


def test_a_permissive_additional_properties_is_accepted_as_a_genuine_no_op():
    """`additionalProperties: true` means "anything else is fine", which is
    exactly what ignoring it does -- so it is not a silent unenforced rule."""
    validate.assert_supported_schema({"type": "object", "additionalProperties": True})
    with pytest.raises(validate.UnsupportedSchemaError):
        validate.assert_supported_schema({"type": "object", "additionalProperties": False})


class TestListingCommands:
    """`lesson list` and `trace list` had no coverage at all, which is how a
    crash on a present-but-empty YAML field reached the branch."""

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
        """`status:` with no value is valid YAML and parses to None, which has
        no __format__ for a width spec. A half-finished edit is exactly when
        someone runs a listing to find the file that needs fixing, and
        `lesson validate` already reports such a file cleanly -- the two
        commands disagreeing was the defect."""
        main(["init", "--agent-type", "support", "--dest", str(store)])
        self._lesson(store, "lesson_broken", status=None, agent_type=None, description=None)
        capsys.readouterr()
        assert main(["lesson", "list", "--dest", str(store)]) == 0
        assert "lesson_broken" in capsys.readouterr().out

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
    """Every other error path in this CLI prints "[commontrace] ..." and exits
    non-zero; a mistyped path reaching open() as a raw traceback was the odd
    one out."""

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
        """Otherwise the invalid record lands on disk and pilot_metrics
        averages it into a customer-facing cost figure before `trace validate`
        ever runs."""
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
    """main() imports only `commands/<name>_cmd.py` for `commontrace <name>`,
    so a module whose command is named anything else would be unreachable."""
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
    """Every command used to import every other command's dependencies --
    numpy, asyncio, ssl -- about 140ms on each `capture` an agent runs."""
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
