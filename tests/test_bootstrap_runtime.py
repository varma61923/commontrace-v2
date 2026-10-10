import json

from commontrace.bootstrap_runtime import setup


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
