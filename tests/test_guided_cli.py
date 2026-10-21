from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from unimem import cli, gateway, hierarchical, ui_commands
from unimem.conversation import Store
from tests.test_workbench import TOKEN, call


def test_all_commands_are_categorized_and_topic_discovery_is_useful(capsys):
    catalog = ui_commands.catalog()
    assert {row["name"] for row in catalog} == set(cli._COMMANDS)
    assert all(row["group"] != "Other" for row in catalog)
    assert cli.main(["help", "how", "do", "I", "use", "semantic", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in payload["commands"]} >= {"query", "recall", "index"}
    assert payload["aliases"]["search"] == ["recall"]


def test_common_operation_alias_executes_the_existing_fact_handler(tmp_path, capsys):
    assert cli.main(["add", "Deployment uses a stable idempotency key.", "--dest", str(tmp_path)]) == 0
    assert any("idempotency" in fact.statement for fact in hierarchical.load_facts(str(tmp_path)).values())
    capsys.readouterr()
    assert cli.main(["search", "What key should deployment use?", "--dest", str(tmp_path), "--json"]) == 0
    assert "idempotency" in capsys.readouterr().out


def test_noninteractive_dry_run_does_not_create_a_store(tmp_path, capsys):
    destination = tmp_path / "new store"
    assert cli.main(["setup", "--workflow", "runtime", "--dest", str(destination), "--non-interactive",
                     "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False and result["steps"] == []
    assert result["commands"][0][:2] == ["up", "--setup-only"]
    assert not destination.exists()


def test_guided_runtime_initialization_is_real_and_does_not_start_a_server(tmp_path, capsys):
    destination = tmp_path / "store"
    assert cli.main(["setup", "--workflow", "runtime", "--dest", str(destination), "--profile", "local",
                     "--agent", "codex", "--non-interactive", "--apply", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] and result["steps"][0]["exit_code"] == 0
    assert (destination / "memory" / "INDEX.md").is_file()
    assert not (destination / "memory" / "hooks.sock").exists()
    assert result["next_commands"][0][0] == "up"


def test_guided_ingestion_preserves_argument_boundaries_and_writes_evidence(tmp_path, capsys):
    source = tmp_path / "notes; not-a-command.md"
    source.write_text("# Release\nThe reviewed release location is Oslo.\n")
    destination = tmp_path / "store"
    destination.mkdir()
    (destination / "unimem.yaml").write_text("profiles:\n  local:\n    embedding: none\n")
    assert cli.main(["setup", "--workflow", "ingest", "--source", str(source), "--context", "release",
                     "--space", "notes", "--dest", str(destination), "--non-interactive", "--apply", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["steps"][0]["command"][1] == str(source)
    with Store(str(destination), "notes", create=False) as store:
        assert any("Oslo" in turn.text for session in store.sessions() for turn in store.session_turns(session["id"]))


def test_interactive_plan_requires_explicit_application(tmp_path, monkeypatch, capsys):
    answers = iter(["runtime", str(tmp_path / "new"), "local", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert cli.main(["setup", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("arguments", [[], ["--workflow", "ingest"],
                                       ["--workflow", "migrate", "--source", "missing"]])
def test_invalid_guided_workflows_fail_before_writes(tmp_path, arguments):
    assert cli.main(["setup", "--non-interactive", "--dest", str(tmp_path / "new"), *arguments]) != 0
    assert not (tmp_path / "new").exists()


def test_provider_cli_exposes_redacted_validated_configuration(tmp_path, capsys):
    pytest.importorskip("pydantic")
    source = tmp_path / "provider.json"
    source.write_text(json.dumps({"provider": "anthropic", "model": "test", "api_key": "fixture-private"}))
    assert cli.main(["providers", "validate", "llm", str(source)]) == 0
    result = capsys.readouterr().out
    assert json.loads(result)["valid"] and "fixture-private" not in result
    assert cli.main(["providers", "list", "--json"]) == 0
    assert "openai" in json.loads(capsys.readouterr().out)["llm"]


@pytest.mark.parametrize("command", ["up", "setup", "providers"])
def test_process_and_sensitive_setup_commands_are_terminal_only(tmp_path, command):
    gw = gateway.Gateway(str(tmp_path), token=TOKEN, allow_approval=True)
    status, response = call(gw, "POST", "/v1/command", {"command": command, "args": []})
    assert status == 409 and response["error"]["code"] == "command_unavailable"


def test_cli_capture_bounds_memory_while_preserving_write_contract():
    capture = ui_commands.BoundedCapture(8)
    assert capture.write("first") == 5
    assert capture.write("a" * 1_000_000) == 1_000_000
    assert capture.getvalue() == "firstaaa" and capture.truncated


@pytest.mark.parametrize("command", sorted(cli._COMMANDS))
def test_every_command_has_lazy_help_without_store_mutation(command, tmp_path, monkeypatch):
    monkeypatch.setenv("UNIMEM_ROOT", str(tmp_path / "no-store"))
    with pytest.raises(SystemExit) as caught:
        cli.build_parser(only=command).parse_args([command, "--help"])
    assert caught.value.code == 0
    assert not (tmp_path / "no-store").exists()
