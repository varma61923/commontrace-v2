"""`commontrace init --agent-caller NAME`: one-command, no-dashboard agent signup."""
import json

from commontrace import agent_registry
from commontrace.cli import main


def _last_json(capsys):
    lines = capsys.readouterr().out.strip().splitlines()
    return json.loads(next(line for line in reversed(lines) if line.startswith('{"agent_id"')))


def test_caller_names_the_agent_and_records_its_scope(tmp_path, capsys):
    assert main(["init", "--dest", str(tmp_path), "--agent-caller", "claude-code"]) == 0
    result = _last_json(capsys)
    assert result["agent_id"].startswith("claude-code-")
    assert "caller:claude-code" in result["scopes"] and "agent:" + result["agent_id"] in result["scopes"]
    assert result["token"].startswith("cta_")
    stored = agent_registry.load(str(tmp_path))[result["agent_id"]]
    assert "token" not in stored and stored["token_hash"]
    assert agent_registry.authenticate(str(tmp_path), result["token"])["id"] == result["agent_id"]


def test_invalid_caller_is_refused_without_minting(tmp_path, capsys):
    assert main(["init", "--dest", str(tmp_path), "--agent-caller", "bad name!"]) == 2
    assert agent_registry.load(str(tmp_path)) == {}


def test_existing_agent_id_is_refused_not_rotated(tmp_path, capsys):
    assert main(["init", "--dest", str(tmp_path), "--agent", "fixed-id"]) == 0
    capsys.readouterr()
    assert main(["init", "--dest", str(tmp_path), "--agent", "fixed-id"]) == 2
    assert "already registered" in capsys.readouterr().err
