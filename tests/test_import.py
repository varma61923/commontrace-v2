import io
import json
import os

import pytest

from commontrace import import_data
from commontrace.cli import main
from commontrace.import_data import FieldMapping


class TestParseJsonl:
    def test_parses_valid_rows(self):
        lines = [
            '{"title": "t1", "context": "c1", "solution": "s1", "tags": "a,b"}',
            '{"title": "t2", "context": "c2", "solution": "s2", "tags": ["x", "y"]}',
        ]
        imported, skipped = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert len(imported) == 2
        assert skipped == []
        assert imported[0].tags == ["a", "b"]
        assert imported[1].tags == ["x", "y"]

    def test_skips_missing_required_fields(self):
        lines = ['{"title": "t1", "context": "", "solution": "s1"}']
        imported, skipped = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert imported == []
        assert len(skipped) == 1
        assert "context" in skipped[0].reason

    def test_skips_invalid_json_without_crashing(self):
        lines = ["not json at all", '{"title": "t1", "context": "c1", "solution": "s1"}']
        imported, skipped = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert len(imported) == 1
        assert len(skipped) == 1
        assert "invalid JSON" in skipped[0].reason

    def test_skips_non_object_json_line(self):
        lines = ["[1, 2, 3]"]
        imported, skipped = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert imported == []
        assert "expected a JSON object" in skipped[0].reason

    def test_blank_lines_are_ignored_not_skipped(self):
        lines = ["", "   ", '{"title": "t1", "context": "c1", "solution": "s1"}']
        imported, skipped = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert len(imported) == 1
        assert skipped == []

    def test_extracts_outcome_fields(self):
        lines = [
            '{"title": "t1", "context": "c1", "solution": "s1", '
            '"resolved": "true", "escalated": "false", "tokens_used": "150"}'
        ]
        imported, _ = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert imported[0].outcome == {"resolved": True, "escalated": False, "tokens_used": 150}

    def test_extracts_nested_outcome_fields(self):
        lines = [
            '{"title": "t1", "context": "c1", "solution": "s1", '
            '"outcome": {"resolved": true, "baseline": true, "tokens_used": 150}}'
        ]
        imported, _ = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert imported[0].outcome == {"resolved": True, "baseline": True, "tokens_used": 150}

    def test_top_level_outcome_field_wins_over_nested(self):
        lines = [
            '{"title": "t1", "context": "c1", "solution": "s1", '
            '"resolved": false, "outcome": {"resolved": true}}'
        ]
        imported, _ = import_data.parse_jsonl(iter(lines), FieldMapping())
        assert imported[0].outcome == {"resolved": False}

    def test_custom_field_mapping(self):
        lines = ['{"summary": "t1", "body": "c1", "fix": "s1"}']
        mapping = FieldMapping(title="summary", context="body", solution="fix")
        imported, skipped = import_data.parse_jsonl(iter(lines), mapping)
        assert len(imported) == 1
        assert imported[0].title == "t1"


class TestParseCsv:
    def test_parses_csv_rows(self):
        csv_text = "title,context,solution,tags\nt1,c1,s1,\"a;b\"\n"
        imported, skipped = import_data.parse_csv(io.StringIO(csv_text), FieldMapping())
        assert len(imported) == 1
        assert skipped == []
        assert imported[0].tags == ["a", "b"]

    def test_csv_line_numbers_account_for_header(self):
        csv_text = "title,context,solution\nt1,,s1\n"
        imported, skipped = import_data.parse_csv(io.StringIO(csv_text), FieldMapping())
        assert imported == []
        assert skipped[0].line_no == 2


class TestBoolParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("true", True), ("True", True), ("1", True), ("yes", True),
        ("false", False), ("0", False), ("no", False),
        ("", None), ("maybe", None),
    ])
    def test_parse_bool(self, raw, expected):
        assert import_data._parse_bool(raw) == expected


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestImportCommand:
    def test_imports_jsonl_into_traces_dir(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        jsonl_path.write_text(
            '{"title": "Refund confusion", "context": "customer confused", "solution": "clarify policy", '
            '"tags": "refunds", "id": "TICKET-1"}\n',
            encoding="utf-8",
        )
        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dest", str(store)])
        assert rc == 0

        traces_dir = store / "memory" / "traces"
        written = [f for f in os.listdir(traces_dir) if f != "README.md"]
        assert len(written) == 1
        content = (traces_dir / written[0]).read_text(encoding="utf-8")
        assert "Refund confusion" in content
        assert "TICKET-1" in content

    def test_dry_run_writes_nothing(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        jsonl_path.write_text('{"title": "t1", "context": "c1", "solution": "s1"}\n', encoding="utf-8")
        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dry-run", "--dest", str(store)])
        assert rc == 0
        traces_dir = store / "memory" / "traces"
        written = [f for f in os.listdir(traces_dir) if f != "README.md"]
        assert written == []
        assert "--dry-run" in capsys.readouterr().out

    def test_every_row_skipped_returns_nonzero_not_success(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        jsonl_path.write_text('{"unrelated_field": "no usable columns here"}\n', encoding="utf-8")
        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dest", str(store)])
        assert rc == 1
        traces_dir = store / "memory" / "traces"
        assert [f for f in os.listdir(traces_dir) if f != "README.md"] == []

    def test_every_row_skipped_in_dry_run_also_returns_nonzero(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        jsonl_path.write_text('{"unrelated_field": "no usable columns here"}\n', encoding="utf-8")
        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dry-run", "--dest", str(store)])
        assert rc == 1

    def test_dry_run_reports_a_schema_invalid_row_as_rejected_not_written(
        self, store, tmp_path, capsys
    ):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        jsonl_path.write_text(
            '{"title": "t1", "context": "c1", "solution": "s1", "tokens_used": -5}\n',
            encoding="utf-8",
        )
        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dry-run", "--dest", str(store)])
        captured = capsys.readouterr()
        assert rc == 1
        assert "0 trace(s) would be created" in captured.out
        assert "would be rejected as schema-invalid" in captured.err

        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dest", str(store)])
        assert rc == 1
        traces_dir = store / "memory" / "traces"
        assert [f for f in os.listdir(traces_dir) if f != "README.md"] == []

    def test_missing_file_fails_cleanly(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["import", "/no/such/file.jsonl", "--agent-type", "support", "--dest", str(store)])
        assert rc == 1
        assert "no such file" in capsys.readouterr().err

    def test_csv_format_inferred_from_extension(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        csv_path = tmp_path / "export.csv"
        csv_path.write_text("title,context,solution\nt1,c1,s1\n", encoding="utf-8")
        capsys.readouterr()
        rc = main(["import", str(csv_path), "--agent-type", "support", "--dest", str(store)])
        assert rc == 0
        traces_dir = store / "memory" / "traces"
        assert len([f for f in os.listdir(traces_dir) if f != "README.md"]) == 1

    def test_imported_traces_pass_schema_validation(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        jsonl_path.write_text(
            '{"title": "t1", "context": "c1", "solution": "s1", "resolved": "true"}\n', encoding="utf-8"
        )
        capsys.readouterr()
        main(["import", str(jsonl_path), "--agent-type", "code", "--dest", str(store)])
        capsys.readouterr()
        assert main(["trace", "validate", "--dest", str(store)]) == 0


class TestSourceIdCannotInjectMarkdownSections:
    def test_a_newline_in_source_id_cannot_inject_a_context_section(self, store, tmp_path, capsys):
        from commontrace import trace_io

        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        payload = {
            "title": "t1", "context": "real context", "solution": "real solution",
            "id": "TICKET-1\n## Context\nINJECTED FAKE CONTEXT\n## Solution\nINJECTED FAKE SOLUTION",
        }
        jsonl_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        capsys.readouterr()
        rc = main(["import", str(jsonl_path), "--agent-type", "support", "--dest", str(store)])
        assert rc == 0

        traces_dir = store / "memory" / "traces"
        written = [f for f in os.listdir(traces_dir) if f != "README.md"]
        assert len(written) == 1
        instance, _ = trace_io.read(str(traces_dir / written[0]))
        assert instance["context_text"] == "real context"
        assert instance["solution_text"] == "real solution"
        assert "INJECTED FAKE" not in instance["context_text"]
        assert "INJECTED FAKE" not in instance["solution_text"]

    def test_source_id_survives_as_a_single_line_provenance_comment(self, store, tmp_path, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        jsonl_path = tmp_path / "export.jsonl"
        payload = {
            "title": "t1", "context": "c1", "solution": "s1",
            "id": "TICKET-1\nsneaky-second-line",
        }
        jsonl_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        capsys.readouterr()
        main(["import", str(jsonl_path), "--agent-type", "support", "--dest", str(store)])
        traces_dir = store / "memory" / "traces"
        written = [f for f in os.listdir(traces_dir) if f != "README.md"]
        content = (traces_dir / written[0]).read_text(encoding="utf-8")
        assert "TICKET-1 sneaky-second-line" in content
        assert "TICKET-1\nsneaky-second-line" not in content


def _one(row, mapping=None):
    return import_data.parse_jsonl(iter([json.dumps(row)]), mapping or FieldMapping())


def test_protocol_field_names_import_without_flags():
    parsed, skipped = _one({"title": "t", "context_text": "ctx", "solution_text": "sol", "tags": ["a"]})
    assert skipped == []
    assert parsed[0].context_text == "ctx" and parsed[0].solution_text == "sol"


def test_legacy_field_names_still_import():
    parsed, skipped = _one({"title": "t", "context": "ctx", "solution": "sol"})
    assert skipped == [] and parsed[0].context_text == "ctx"


def test_an_explicit_mapping_still_wins():
    parsed, skipped = _one({"title": "t", "body": "ctx", "fix": "sol"},
                           FieldMapping(context="body", solution="fix"))
    assert skipped == [] and parsed[0].context_text == "ctx"


def test_a_truly_missing_field_names_both_accepted_spellings():
    _, skipped = _one({"title": "only a title"})
    assert "context/context_text" in skipped[0].reason
