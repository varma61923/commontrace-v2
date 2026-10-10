import json

from commontrace.history_import import import_history
from commontrace.memory_journal import Journal


def test_authorship_code_verification_and_coverage(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "parser.py").write_text("def parse():\n    return 1\n")
    history = tmp_path / "chat.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"text": "Use parser.py for parsing."}]},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"text": "parser.py defines def parse()"}]},
        },
    ]
    history.write_text("\n".join(json.dumps(row) for row in rows))
    result = import_history(str(root), str(history), agent="codex", scope={"agent": "a"})
    assert result["messages"] == 2
    assert result["claims"][0]["author"] == "user"
    assert result["claims"][1]["author"] == "assistant"
    assert result["claims"][1]["code_verified"]
    assert import_history(str(root), str(history), agent="codex", scope={"agent": "a"})["messages"] == 0
    assert not Journal(str(root)).search("", scope={"agent": "a"}, kinds=("lesson",))
