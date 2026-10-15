import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from commontrace.commands.integration_cmd import CLIENTS, configuration

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('client', CLIENTS)
def test_native_configuration_keeps_argument_boundaries_and_disables_approval(client, tmp_path):
    root = str(tmp_path / 'store with spaces')
    config = configuration(client, root, command='/executables with spaces/commontrace')
    if client == 'zed':
        server = config['context_servers']['commontrace']['command']
        assert server['path'] == '/executables with spaces/commontrace'
    else:
        server = config['servers' if client == 'vscode' else 'mcpServers']['commontrace']
    assert server['args'] == ['serve', '--dest', root, '--no-approval']
    assert not os.path.exists(root)


def test_cli_prints_json_without_creating_a_store(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'commontrace', 'integration', '--client', 'zed',
                             '--dest', str(tmp_path)], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)['context_servers']['commontrace']['command']['args'][-1] == '--no-approval'
    assert not list(tmp_path.iterdir())


def test_registry_builder_requires_an_absolute_store_and_disables_approval_by_default():
    import yaml

    manifest = yaml.safe_load((ROOT / "smithery.yaml").read_text())
    source = manifest["startCommand"]["commandFunction"]
    assert "storeRoot must be an absolute path" in source
    assert "approvalTools === true" in source


@pytest.mark.parametrize('command', ['', 'binary\nextra', 'binary\x00'])
def test_config_rejects_invalid_executables(command):
    with pytest.raises(ValueError):
        configuration('cursor', '.', command=command)


def action_module():
    spec = importlib.util.spec_from_file_location('action_check', ROOT / 'distribution' / 'action' / 'check.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_action_rejects_path_escape_before_running_any_command(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('COMMONTRACE_ACTION_STORE', '../outside')
    with pytest.raises(ValueError, match='inside'):
        action_module().main()


def test_action_uses_real_installed_cli_to_initialize_and_check(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / 'distribution' / 'action' / 'check.py')],
                            env={**os.environ, 'GITHUB_WORKSPACE': str(tmp_path),
                                 'COMMONTRACE_ACTION_INITIALIZE': 'true', 'COMMONTRACE_ACTION_STORE': '.'},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'memory' / 'INDEX.md').is_file()


@pytest.mark.parametrize(('api', 'document'), [('gateway', 'openapi/gateway.json'), ('hub', 'hub/openapi.json')])
def test_generator_selects_versioned_openapi_contracts(monkeypatch, tmp_path, api, document):
    from scripts import generate_sdks
    commands = []
    monkeypatch.setattr(sys, 'argv', ['generate_sdks', '--api', api, '--languages', 'typescript',
                                    '--output', str(tmp_path)])
    monkeypatch.setattr(generate_sdks, 'checked_jar', lambda _: Path('verified.jar'))
    monkeypatch.setattr(generate_sdks.subprocess, 'run', lambda args, **_: commands.append(args))
    assert generate_sdks.main() == 0
    assert len(commands) == 4
    assert commands[-1][commands[-1].index('-i') + 1] == str(ROOT / document)


def test_kotlin_generator_normalization_preserves_canonical_enum_constraints(tmp_path):
    from scripts.generate_sdks import kotlin_compatible_spec

    source = tmp_path / "source.json"
    target = tmp_path / "normalized.json"
    source.write_text(json.dumps({"components": {"schemas": {
        "blank": {"type": "string", "enum": ["", "first", "forced"]},
        "status": {"type": "string", "enum": ["ready", "failed"]},
        "scopes": {"type": "array", "items": {"type": "string", "enum": ["read", "write"]}},
    }}}))
    normalized = json.loads(kotlin_compatible_spec(source, target).read_text())
    schemas = normalized["components"]["schemas"]
    assert "enum" not in schemas["blank"]
    assert schemas["status"]["enum"] == ["ready", "failed"]
    assert "enum" not in schemas["scopes"]["items"]
    assert json.loads(source.read_text())["components"]["schemas"]["blank"]["enum"][0] == ""
