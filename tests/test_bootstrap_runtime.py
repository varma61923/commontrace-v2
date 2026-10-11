import json

import pytest

from commontrace.bootstrap_runtime import merge_hooks, setup


def test_setup_is_idempotent_and_merges_native_hooks(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing"}]}]}}))
    config = {"agents": {"claude": {"config_path": "hooks.json"}}, "histories": []}
    first = setup(str(tmp_path), config=config)
    second = setup(str(tmp_path), config=config)
    assert first["agents"][0]["native_hooks_installed"]
    assert len(json.loads(path.read_text())["hooks"]["SessionStart"]) == 2
    assert second["elapsed_seconds"] < 120
    assert first["storage"] == "sqlite-wal"


@pytest.mark.parametrize("previous", ["python -m commontrace.hook_kit", "node hook-bridge.mjs", "python hook_bridge.py"])
def test_reinstall_replaces_previous_transport_and_preserves_other_hooks(tmp_path, previous):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"command": previous}, {"command": "other-tool"}]}}))
    merge_hooks(str(path), {"hooks": {"SessionStart": [{"command": "python hook_bridge.py --event SessionStart"}]}})
    assert json.loads(path.read_text())["hooks"]["SessionStart"] == [
        {"command": "other-tool"}, {"command": "python hook_bridge.py --event SessionStart"},
    ]
