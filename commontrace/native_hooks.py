"""Host-specific serializers over the shared hook core.

Only events documented by each host are installed. Missing lifecycle events
remain available through the portable bridge; capture support is distinct from
support for adding text to the model context.
"""

from __future__ import annotations

import json
import os
import re

from commontrace import _jsonl, paths
from commontrace.hook_bridge import format_reply as format_reply

PROJECT_CONFIGS = {
    "claude": os.path.join(".claude", "settings.local.json"),
    "codex": os.path.join(".codex", "hooks.json"),
    "cursor": os.path.join(".cursor", "hooks.json"),
    "gemini": os.path.join(".gemini", "settings.json"),
    "kimi": os.path.join("~", ".kimi-code", "config.toml"),
    "cline": os.path.join(".clinerules", "hooks"),
    "windsurf": os.path.join(".windsurf", "hooks.json"),
    "openhands": os.path.join(".openhands", "hooks.json"),
}
CLINE = {
    "TaskStart": "SessionStart",
    "TaskResume": "SessionStart",
    "UserPromptSubmit": "UserPromptSubmit",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "TaskCancel": "SessionEnd",
    "TaskComplete": "SessionEnd",
}
WINDSURF = {
    "pre_user_prompt": "UserPromptSubmit",
    "pre_run_command": "PreToolUse",
    "pre_read_code": "PreToolUse",
    "pre_write_code": "PreToolUse",
    "pre_mcp_tool_use": "PreToolUse",
    "post_run_command": "PostToolUse",
    "post_read_code": "PostToolUse",
    "post_write_code": "PostToolUse",
    "post_mcp_tool_use": "PostToolUse",
    "post_cascade_response": "Stop",
}
OPENHANDS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop")
KIMI_HOOK_EVENTS = frozenset(
    {
        "UserPromptSubmit",
        "UserPromptQueued",
        "PreToolUse",
        "Stop",
        "TurnStarted",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "PermissionResult",
        "SessionStart",
        "SessionEnd",
        "SessionHeartbeat",
        "SubagentStart",
        "SubagentStop",
        "TaskStarted",
        "StopFailure",
        "Interrupt",
        "PreCompact",
        "PostCompact",
        "Notification",
    }
)


def resolve_kimi_config_target(root: str, configured_path: str | None = None) -> tuple[str, bool]:
    """Resolve Kimi's active config and whether its explicit target may be external."""
    if configured_path is None:
        kimi_home = os.environ.get("KIMI_CODE_HOME") or "~/.kimi-code"
        target = os.path.join(os.path.expanduser(os.path.expandvars(kimi_home)), "config.toml")
        target = os.path.abspath(target)
    else:
        if not isinstance(configured_path, str) or not configured_path.strip():
            raise ValueError("Kimi config_path must be a non-empty path")
        configured = os.path.expanduser(os.path.expandvars(configured_path))
        if os.path.isabs(configured):
            target = os.path.abspath(configured)
        else:
            target = os.path.abspath(os.path.join(root, configured))
            paths.enforce_boundary(root, target)
    return target, not paths.is_within_directory(root, target)


def install_special(
    root: str, agent: str, target: str, manifest: dict, *, allow_external: bool = False
) -> dict | None:
    if agent == "kimi":
        if allow_external:
            if not os.path.isabs(target):
                raise ValueError("external Kimi config paths must be absolute")
            target = os.path.abspath(target)
        else:
            paths.enforce_boundary(root, target)
            target = os.path.abspath(target)
        events = manifest.get("events")
        if not isinstance(events, dict):
            raise ValueError("Kimi hooks require an event-to-command mapping")
        for event, command in events.items():
            if event not in KIMI_HOOK_EVENTS:
                raise ValueError("unsupported Kimi hook event: " + str(event))
            if not isinstance(command, str) or not command.strip():
                raise ValueError("Kimi hook commands must be non-empty strings")
        paths.safe_prepare_output_path(target, allow_unlink_leaf=False)
        paths.safe_prepare_output_path(target + ".lock", allow_unlink_leaf=False)
        if os.path.exists(target) and not os.path.isfile(target):
            raise ValueError("Kimi config target must be a regular file")
        begin, end = "# BEGIN COMMONTRACE HOOKS", "# END COMMONTRACE HOOKS"
        with _jsonl.locked(target):
            original = ""
            if os.path.isfile(target):
                with open(target, "rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Kimi config exceeds 64 KiB")
                original = raw.decode("utf-8")
            start_count, end_count = original.count(begin), original.count(end)
            if start_count != end_count or start_count > 1:
                raise ValueError("Kimi config contains an incomplete or duplicate CommonTrace hook region")
            pattern = r"(?m)^# BEGIN COMMONTRACE HOOKS\r?\n.*?^# END COMMONTRACE HOOKS\r?(?:\n|$)"
            original, removed = re.subn(pattern, "", original, flags=re.S)
            if start_count and removed != 1:
                raise ValueError("Kimi config contains a malformed CommonTrace hook region")
            blocks = [begin]
            for event, command in events.items():
                blocks.append(
                    "[[hooks]]\nevent = "
                    + json.dumps(event, ensure_ascii=False)
                    + "\ncommand = "
                    + json.dumps(command + " --format kimi", ensure_ascii=False)
                    + "\ntimeout = 1"
                )
            blocks.append(end)
            separator = ""
            if original and not original.endswith("\n\n"):
                separator = "\n" if original.endswith("\n") else "\n\n"
            paths.safe_prepare_output_path(target, allow_unlink_leaf=False)
            with open(target, "w", encoding="utf-8") as stream:
                stream.write(original + separator + "\n\n".join(blocks) + "\n")
        return {
            "native_hooks_installed": True,
            "context_native": True,
            "activation": "kimi",
            "events": list(events),
        }
    paths.enforce_boundary(root, target)
    if agent == "cline":
        os.makedirs(target, exist_ok=True)
        for native, event in CLINE.items():
            filename = os.path.join(target, native)
            if os.path.isfile(filename):
                with open(filename, encoding="utf-8") as stream:
                    original = stream.read(64001)
                if "# Generated by CommonTrace" not in original:
                    raise ValueError("existing Cline hook requires an explicit chained wrapper: " + native)
            paths.enforce_boundary(root, filename)
            with open(filename, "w", encoding="utf-8") as stream:
                stream.write(
                    "#!/usr/bin/env bash\n# Generated by CommonTrace\nexec "
                    + manifest["events"][event]
                    + " --format cline\n"
                )
            os.chmod(filename, 0o700)
        return {
            "native_hooks_installed": True,
            "context_native": True,
            "activation": "Enable Hooks in Cline host settings",
            "events": list(CLINE),
        }
    return None


def special_json(manifest: dict) -> dict | None:
    agent = manifest["agent"]
    if agent == "openhands":
        return {
            "hooks": {
                event: [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": manifest["events"][event] + " --format openhands",
                                "timeout": 1,
                            }
                        ],
                    }
                ]
                for event in OPENHANDS
            }
        }
    if agent == "windsurf":
        # Cascade hook stdout is UI output, not an additional-context API.
        return {
            "hooks": {
                native: [{"command": manifest["events"][event] + " --format windsurf", "show_output": False}]
                for native, event in WINDSURF.items()
            }
        }
    return None


def normalize(agent: str | None, event: str, payload: dict) -> tuple[str, dict]:
    data = dict(payload)
    if agent == "cline":
        nested = data.get(event[0].lower() + event[1:], {})
        native = data.get("hookName", "")
        nested = data.get(native[0].lower() + native[1:], nested) if native else nested
        if isinstance(nested, dict):
            data.update(nested)
        data.setdefault("session_id", data.get("taskId"))
        data.setdefault("tool_name", data.get("toolName", ""))
        data.setdefault("tool_input", data.get("parameters", {}))
        if event == "PostToolUse" and data.get("success") is False:
            event = "PostToolUseFailure"
            data.setdefault("error", data.get("result", ""))
    elif agent == "windsurf":
        data.update(data.get("tool_info", {}) if isinstance(data.get("tool_info"), dict) else {})
        data.setdefault("session_id", data.get("trajectory_id"))
        data.setdefault("tool_name", data.get("mcp_tool_name") or data.get("agent_action_name", ""))
        data.setdefault(
            "tool_input", data.get("mcp_tool_arguments") or data.get("command_line") or data.get("file_path", "")
        )
    elif agent == "codex" and event == "PostToolUse":
        response = data.get("tool_response")
        failed = None
        if isinstance(response, dict):
            exit_code = response.get("exit_code")
            if type(exit_code) is int:
                failed = exit_code != 0
            elif type(response.get("isError")) is bool:
                failed = response["isError"]
            elif type(response.get("success")) is bool:
                failed = not response["success"]
        if failed:
            event = "PostToolUseFailure"
            data.setdefault("error", response)
    elif agent == "openhands":
        data.setdefault("prompt", data.get("message", ""))
    return event, data
