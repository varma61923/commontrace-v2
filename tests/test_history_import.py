import hashlib
import json

import pytest

from unimem.history_import import import_history
from unimem.memory_journal import Journal


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


def test_actionable_code_grounded_import_seeds_only_quarantined_candidates(tmp_path):
    from unimem.session_learning import shadow

    (tmp_path / "parser.py").write_text("def parse():\n    return 1\n")
    source = tmp_path / "history.json"
    source.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Always test `parse` in parser.py before changing its boundary handling.",
                    },
                    {"role": "assistant", "content": "Always use missing.py `invented` for all parser work."},
                    {"role": "system", "content": "Always run `parse` in `parser.py` before edits."},
                ]
            }
        )
    )
    result = import_history(str(tmp_path), str(source), agent="cursor", scope={"agent": "a"})
    assert result["messages"] == 3
    assert result["lessons_seeded"] == 1
    assert [claim["author"] for claim in result["claims"]] == ["user", "assistant", "system"]
    journal = Journal(str(tmp_path))
    assert not journal.search("", scope={"agent": "a"}, kinds=("lesson",))
    identity = result["lesson_ids"][0]
    lesson = shadow(str(tmp_path), identity, actor="owner")
    assert lesson.status == "shadow"
    assert journal.search("parse", scope={"agent": "a"}, kinds=("lesson",), statuses=("shadow",))
    assert import_history(str(tmp_path), str(source), agent="cursor", scope={"agent": "a"})["lessons_seeded"] == 0
    (tmp_path / "parser.py").write_text("def replacement():\n    return 2\n")
    assert not journal.search("parse", scope={"agent": "a"}, kinds=("lesson",), statuses=("shadow",))


def test_verification_requires_all_quoted_symbols_and_rejects_foreign_paths(tmp_path):
    from unimem.history_import import verify_code

    (tmp_path / "parser.py").write_text("def parse():\n    return 1\n")
    assert not verify_code(str(tmp_path), "parser.py has `parse` and `missing_symbol`")
    assert not verify_code(str(tmp_path), "Use ../parser.py `parse`")


def test_backticked_source_path_is_a_citation_and_can_seed_a_verified_claim(tmp_path):
    from unimem.history_import import verify_code

    (tmp_path / "parser.py").write_text("def parse():\n    return 1\n")
    evidence = verify_code(str(tmp_path), "Use `parser.py` and verify `parse` before running tests.")
    assert evidence == [
        {
            "path": "parser.py",
            "sha256": hashlib.sha256((tmp_path / "parser.py").read_bytes()).hexdigest(),
            "method": "path-and-quoted-symbol",
        }
    ]

    source = tmp_path / "history.json"
    source.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Always run `parse` in `parser.py` before changing its boundary behavior.",
                    }
                ]
            }
        )
    )
    result = import_history(str(tmp_path), str(source), agent="codex", scope={"agent": "a"})
    assert result["claims"][0]["code_verified"]
    assert result["lessons_seeded"] == 1


def test_host_import_revalidates_project_attribution_from_the_imported_bytes(tmp_path):
    source = tmp_path / "rollout.jsonl"
    source.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/other/project"}})
        + "\n"
        + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": "secret"}})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not identify this project"):
        import_history(
            str(tmp_path),
            str(source),
            agent="codex",
            scope={"agent": "codex"},
            expected_project=str(tmp_path / "repo"),
        )
