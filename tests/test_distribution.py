import json
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "distribution")
MARKET = os.path.join(ROOT, "claude-marketplace")
PLUGIN = os.path.join(MARKET, "plugins", "unimem")


def _json(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return json.load(fh)


def test_the_marketplace_entry_matches_the_plugin_it_points_at():
    market = _json(MARKET, ".claude-plugin", "marketplace.json")
    manifest = _json(PLUGIN, ".claude-plugin", "plugin.json")
    assert market["owner"]["name"] and market["name"]
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["source"] == "./plugins/unimem" and os.path.isdir(os.path.join(MARKET, entry["source"]))


def test_the_plugin_version_is_the_package_version():
    with open(os.path.join(os.path.dirname(ROOT), "pyproject.toml"), encoding="utf-8") as fh:
        version = re.search(r'^version = "([^"]+)"', fh.read(), re.M).group(1)
    assert _json(PLUGIN, ".claude-plugin", "plugin.json")["version"] == version
    server = _json(ROOT, "mcp-registry", "server.json")
    assert server["version"] == version and server["packages"][0]["version"] == version


@pytest.mark.parametrize("skill", ["install-unimem", "prove-memory"])
def test_each_skill_has_a_name_and_a_description_that_says_when_to_use_it(skill):
    text = open(os.path.join(PLUGIN, "skills", skill, "SKILL.md"), encoding="utf-8").read()
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    fields = dict(line.split(": ", 1) for line in front.splitlines())
    assert fields["name"] == skill and "Use when" in fields["description"]


def test_the_skills_name_only_commands_that_exist():
    import argparse

    from unimem.cli import build_parser

    def choices(parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices
        return {}

    top = choices(build_parser())
    for skill in ("install-unimem", "prove-memory"):
        text = open(os.path.join(PLUGIN, "skills", skill, "SKILL.md"), encoding="utf-8").read()
        for first, second in re.findall(r"^unimem ([a-z]+)(?: ([a-z]+))?", text, re.M):
            assert first in top, f"{skill}: `unimem {first}` is not a command"
            sub = choices(top[first])
            if sub and second:
                assert second in sub, f"{skill}: `unimem {first} {second}` is not a command"


def test_the_mcp_registry_entry_obeys_the_published_schema_limits():
    server = _json(ROOT, "mcp-registry", "server.json")
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", server["name"])
    assert 1 <= len(server["description"]) <= 100
    package = server["packages"][0]
    assert package["registryType"] == "pypi" and package["transport"] == {"type": "stdio"}
    assert package["packageArguments"] == [{"type": "positional", "value": "serve"}]


def test_the_mcp_server_the_plugin_registers_is_the_real_command():
    cfg = _json(PLUGIN, ".mcp.json")["mcpServers"]["unimem"]
    assert cfg == {"command": "unimem", "args": ["serve"]}


def test_marketplace_metadata_uses_the_repository_license():
    with open(os.path.join(os.path.dirname(ROOT), "LICENSE"), encoding="utf-8") as stream:
        license_text = stream.read(4096)
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
    plugin = _json(PLUGIN, ".claude-plugin", "plugin.json")
    vscode = _json(ROOT, "vscode", "package.json")
    assert plugin["license"] == vscode["license"] == "AGPL-3.0-only"
    with open(os.path.join(os.path.dirname(ROOT), "pyproject.toml"), encoding="utf-8") as stream:
        assert 'license = "AGPL-3.0-only"' in stream.read()
