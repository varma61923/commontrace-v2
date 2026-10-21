import hashlib
import json
import os
import re

from unimem.cli import main
from unimem.memory_journal import Journal


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _claude_path(project, home):
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(project.resolve()))
    return home / ".claude" / "projects" / encoded


def _claude_row(cwd, message, uuid):
    return {
        "type": "user",
        "cwd": str(cwd),
        "uuid": uuid,
        "message": {"role": "user", "content": message},
    }


def _codex_rows(cwd, message, session_id):
    return [
        {"type": "session_meta", "payload": {"cwd": str(cwd), "id": session_id}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": message}],
            },
        },
    ]


def _run_up(root, *, import_agent_history, capsys):
    args = ["up", "--dest", str(root), "--setup-only"]
    if import_agent_history:
        args.append("--import-agent-history")
    assert main(args) == 0
    output = capsys.readouterr().out
    result_start = output.rfind('{\n  "first_lesson"')
    assert result_start >= 0
    return json.loads(output[result_start:])


def test_up_keeps_private_history_opt_in_and_imports_only_cwd_attributed_sessions(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    sibling = tmp_path / "repo-other"
    root.mkdir()
    sibling.mkdir()
    home.mkdir()
    (root / "parser.py").write_text("def parse():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    claude_dir = _claude_path(root, home)
    _write_jsonl(
        claude_dir / "project-session.jsonl",
        [_claude_row(root, "Always run `parse` in `parser.py` before editing.", "claude-user-1")],
    )
    # Claude replaces every non-alphanumeric character with '-', so this name
    # also matches the project-prefix search. The transcript cwd must reject it.
    _write_jsonl(
        _claude_path(root.parent / "repo-other", home) / "sibling-session.jsonl",
        [_claude_row(sibling, "Use the sibling parser.", "claude-sibling")],
    )
    codex_root = home / ".codex" / "sessions" / "2026" / "09" / "02"
    _write_jsonl(
        codex_root / "rollout-project.jsonl",
        _codex_rows(root, "Prefer `parse` in `parser.py` for this project.", "codex-project"),
    )
    _write_jsonl(
        codex_root / "rollout-other.jsonl",
        _codex_rows(sibling, "Do not import this other project.", "codex-other"),
    )

    default_result = _run_up(root, import_agent_history=False, capsys=capsys)
    assert default_result["history"] == []
    assert default_result["history_discovery"] == []
    assert Journal(str(root)).search(
        "Always run parse", kinds=("episode",), statuses=("quarantined",), limit=10
    ) == []

    result = _run_up(root, import_agent_history=True, capsys=capsys)
    imported = {(item["agent"], item["messages"]) for item in result["history"]}
    assert imported == {("claude", 1), ("codex", 1)}
    reports = {item["agent"]: item for item in result["history_discovery"]}
    assert set(reports) == {"claude", "codex", "cursor", "gemini"}
    assert reports["claude"]["candidate_files"] == 2
    assert reports["claude"]["out_of_scope_files"] == 1
    assert reports["claude"]["imported_files"] == 1
    assert reports["codex"]["candidate_files"] == 2
    assert reports["codex"]["out_of_scope_files"] == 1
    assert reports["codex"]["imported_files"] == 1
    assert reports["claude"]["status"] == "bounded-scan-complete"
    assert reports["codex"]["status"] == "bounded-scan-complete"
    for agent in ("cursor", "gemini"):
        report = reports[agent]
        assert report["status"] == "unsupported-project-attribution"
        assert report["scan_performed"] is False
        assert report["candidate_files"] is None
        assert report["candidate_count_known"] is False
        assert report["blocker"]
        coverage = Journal(str(root)).latest(report["coverage_id"])
        assert coverage.payload["status"] == "unsupported-project-attribution"
        assert coverage.payload["blocker"] == report["blocker"]
        assert coverage.payload["counts"]["candidate_files"] is None

    repo_id = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    scope = {"agent": "claude-" + repo_id, "repo": repo_id}
    claims = Journal(str(root)).search(
        "Always run parse", scope=scope, kinds=("episode",), statuses=("quarantined",), limit=10
    )
    assert len(claims) == 1
    claim = claims[0]
    assert claim.payload["author"] == "user"
    assert claim.payload["status"] == "unreviewed"
    assert claim.payload["code_verified"] is True
    assert claim.payload["citation"]["message_index"] == 0
    assert claim.payload["citation"]["source_locator"].startswith(".claude/projects/")
    assert claim.status == "quarantined"

    scan = Journal(str(root)).latest(reports["claude"]["coverage_id"])
    assert scan.payload["subtype"] == "coverage-ledger"
    assert scan.payload["counts"]["imported_files"] == 1
    assert scan.payload["source_coverage_ids"] == reports["claude"]["source_coverage_ids"]


def test_agent_history_discovery_reports_bounded_partial_coverage(tmp_path):
    from unimem.history_import import discover_agent_history

    root = tmp_path / "repo"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    session_dir = home / ".codex" / "sessions" / "2026" / "09" / "02"
    for index in range(5):
        _write_jsonl(
            session_dir / f"rollout-{index}.jsonl",
            _codex_rows(root, f"message {index}", f"session-{index}"),
        )

    sources, reports = discover_agent_history(str(root), home=str(home), max_entries=6, max_files=1)

    codex = [source for source in sources if source["agent"] == "codex"]
    assert len(codex) == 1
    assert reports["codex"]["selected_files"] == 1
    assert reports["codex"]["truncated"] is True


def test_agent_history_total_byte_cap_is_reported(tmp_path, monkeypatch):
    import unimem.history_import as history_import

    root = tmp_path / "repo"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    session_dir = home / ".codex" / "sessions" / "2026" / "09" / "02"
    files = []
    for index in range(2):
        filename = session_dir / f"rollout-{index}.jsonl"
        _write_jsonl(filename, _codex_rows(root, "bounded message", f"session-{index}"))
        files.append(filename)
    monkeypatch.setattr(history_import, "MAX_DISCOVERY_BYTES", files[0].stat().st_size)

    sources, reports = history_import.discover_agent_history(
        str(root), home=str(home), max_entries=100, max_files=10
    )

    assert len([source for source in sources if source["agent"] == "codex"]) == 1
    assert reports["codex"]["selected_bytes"] == files[0].stat().st_size
    assert reports["codex"]["total_bytes_limit_reached"] is True
    assert reports["codex"]["truncated"] is True


def test_agent_history_aggregate_probe_cap_persists_partial_coverage(tmp_path, monkeypatch):
    import unimem.history_import as history_import

    root = tmp_path / "repo"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    session_dir = home / ".codex" / "sessions" / "2026" / "09" / "02"
    for index in range(20):
        _write_jsonl(
            session_dir / f"rollout-{index:02}.jsonl",
            _codex_rows(root, "probe bounded message", f"session-{index:02}"),
        )
    probe_budget = 1024
    monkeypatch.setattr(history_import, "MAX_HEADER_BYTES", 4096)
    monkeypatch.setattr(history_import, "MAX_DISCOVERY_PROBE_BYTES", probe_budget)

    sources, reports = history_import.discover_agent_history(
        str(root), home=str(home), max_entries=100, max_files=20
    )
    report = reports["codex"]
    coverage_id = history_import.record_discovery_coverage(
        str(root), agent="codex", scope={"repo": "fixture"}, report=report
    )

    assert report["probe_bytes"] <= probe_budget
    assert report["probe_limit_reached"] is True
    assert report["candidate_files_unprobed"] > 0
    assert report["truncated"] is True
    assert report["status"] == "partial-limit-reached"
    assert Journal(str(root)).latest(coverage_id).payload["status"] == "partial-limit-reached"
    assert len([source for source in sources if source["agent"] == "codex"]) < 20


def test_cursor_and_gemini_history_discovery_fail_closed_without_project_cwd(tmp_path):
    from unimem.history_import import discover_agent_history, record_discovery_coverage

    root = tmp_path / "repo"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()

    sources, reports = discover_agent_history(str(root), home=str(home))

    assert sources == []
    for agent in ("cursor", "gemini"):
        report = reports[agent]
        assert report["scan_performed"] is False
        assert report["candidate_files"] is None
        assert report["candidate_count_known"] is False
        assert "recorded session cwd" in report["blocker"]
        coverage_id = record_discovery_coverage(
            str(root), agent=agent, scope={"repo": "fixture"}, report=report
        )
        coverage = Journal(str(root)).latest(coverage_id)
        assert coverage.payload["status"] == "unsupported-project-attribution"
        assert coverage.payload["scan_performed"] is False
        assert coverage.payload["blocker"] == report["blocker"]
