"""The generic command console cannot bypass the guarded review workflow."""
from __future__ import annotations

import json
import os

import pytest

from commontrace import frontmatter, gateway, lesson_io, ui_commands
from commontrace.cli import main
from tests.test_workbench import AUTH, TOKEN, _lesson, call


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "--agent-type", "support", "--dest", str(tmp_path)])
    _lesson(str(tmp_path), "lesson_good")
    return str(tmp_path)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("args", [["approve", "lesson_good"], ["approve", "lesson_good", "--fo"],
                                  ["reject", "lesson_good", "--reason", "broad"],
                                  ["auto-approve"], ["revoke", "lesson_good"]])
def test_lesson_admission_requires_dedicated_endpoint(root, enabled, args):
    gw = gateway.Gateway(root, token=TOKEN, allow_approval=enabled)
    status, response = call(gw, "POST", "/v1/command", {"command": "lesson", "args": args})
    assert status == 409 and response["error"]["code"] == "command_unavailable"
    assert frontmatter.read(lesson_io.lesson_path(root, "lesson_good"))[0]["status"] == "review"
    assert not os.path.exists(os.path.join(root, "memory", "lesson_admissions.db"))


@pytest.mark.parametrize("command,args", [
    ("init", []), ("install", []), ("distill", []), ("sync", []),
    ("kb", ["install", "support"]), ("release", ["rollback", "old-release"]),
    ("memory", ["checkout", "HEAD"]),
    ("fact", ["add", "new claim", "--category", "general"]),
    ("retrieval", ["--fusion", "bm25"]),
])
def test_disabled_approval_defaults_to_audited_read_handlers(root, command, args):
    gw = gateway.Gateway(root, token=TOKEN)
    status, response = call(gw, "POST", "/v1/command", {"command": command, "args": args})
    # Valid writes are forbidden; invalid parser inputs never reach a handler.
    assert status == 403 or (status == 200 and response["exit_code"] != 0)
    if status == 403:
        assert response["error"]["code"] == "approval_disabled"


def test_enabled_commands_can_create_facts_without_reopening_lesson_review(root):
    gw = gateway.Gateway(root, token=TOKEN, allow_approval=True)
    status, response = call(gw, "POST", "/v1/command", {
        "command": "fact", "args": ["add", "Retries carry a stable key.", "--category", "general"],
    })
    assert status == 200 and response["ok"]


@pytest.mark.parametrize("enabled", [False, True])
def test_abbreviated_dest_cannot_select_another_store(root, enabled, tmp_path):
    gw = gateway.Gateway(root, token=TOKEN, allow_approval=enabled)
    status, response = call(gw, "POST", "/v1/command", {
        "command": "lesson", "args": ["list", "--de", str(tmp_path / "other")],
    })
    assert status == 400 and response["error"]["code"] == "bad_request"


@pytest.mark.parametrize("args", [["list"], ["list", "--scope", "container:other"]])
def test_container_routing_cannot_be_bypassed_via_cli(root, args):
    gw = gateway.Gateway(root, token=TOKEN, allow_approval=True)
    response = gw.handle("POST", "/v1/command", {**AUTH, "X-Container-Tag": "one"},
                         json.dumps({"command": "lesson", "args": args}).encode())
    assert response.status == 403


@pytest.mark.parametrize("command,args", [("lesson", ["approve", "--help"]), ("distill", ["--help"]),
                                         ("query", ["--help"]), ("lesson", ["list"])])
def test_read_only_commands_and_help_remain_available(root, command, args):
    gw = gateway.Gateway(root, token=TOKEN)
    status, response = call(gw, "POST", "/v1/command", {"command": command, "args": args})
    assert status == 200 and response["ok"]


def test_literal_help_after_end_of_options_is_not_a_policy_bypass(root):
    with pytest.raises(ui_commands.UICommandError) as caught:
        ui_commands.run(root, "lesson", ["approve", "--", "--help"], allow_approval=True)
    assert caught.value.code == "command_unavailable"


def test_command_denial_restores_process_root(root, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_ROOT", "previous-root")
    with pytest.raises(ui_commands.UICommandError):
        ui_commands.run(root, "lesson", ["approve", "lesson_good"])
    assert os.environ["COMMONTRACE_ROOT"] == "previous-root"
