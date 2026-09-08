"""Regression tests for Security Remediations across M1:
- SEC-01: Path traversal protection in hub_client.py:pull_search_results
- SEC-02: Path traversal & schema loading security in validate.py:load_schema
- SEC-03: Subprocess search path isolation in _shellout.find_reference_script
- SEC-06: Tag type confusion handling in retrieval.py and reliability.py
- SEC-07: Root dict type guards in validate.py:validate
- SEC-08: API key handling and guidance in sync_cmd.py
"""
import argparse
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from commontrace import frontmatter, hub_client, paths, reliability, retrieval, validate
from commontrace.commands import _shellout, sync_cmd


# ==============================================================================
# SEC-01: Path Traversal in hub_client.py:pull_search_results
# ==============================================================================
class TestHubClientPathTraversalProtection:
    """SEC-01: Ensure pull_search_results neutralizes path traversal payloads."""

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\evil.exe",
            "/absolute/unix/path/id",
            "C:\\Windows\\System32\\cmd.exe",
            "id_with_\x00_null_byte",
            "special!@#$%^&*()_+={}|:<>?,./chars",
            "....//....//nested_traversal",
        ],
    )
    def test_pull_search_results_sanitizes_malicious_trace_ids(
        self, tmp_path, malicious_id
    ):
        traces_dir = paths.traces_dir(str(tmp_path))
        os.makedirs(traces_dir, exist_ok=True)
        tdir_abs = os.path.abspath(traces_dir)

        mock_payload = {
            "traces": [
                {
                    "id": malicious_id,
                    "title": "Secret Trace",
                    "agent_type": "code",
                    "tags": ["security", "test"],
                    "context_text": "context",
                    "solution_text": "solution",
                }
            ]
        }

        async def _run():
            with patch.object(hub_client, "_call_tool", AsyncMock(return_value=mock_payload)):
                return await hub_client.pull_search_results(
                    "http://hub.example.com", "fake_key", str(tmp_path)
                )

        result = asyncio.run(_run())

        assert len(result.written_paths) == 1
        written = result.written_paths[0]
        written_abs = os.path.abspath(written)

        # Strict containment check: must be strictly inside tdir_abs
        assert written_abs.startswith(tdir_abs + os.sep), (
            f"File {written_abs} escaped directory {tdir_abs}"
        )
        assert os.path.exists(written_abs)

        # Metadata check: original raw id preserved in frontmatter hub_trace_id
        fm, body = frontmatter.read(written_abs)
        assert fm["hub_trace_id"] == malicious_id
        assert "## Context" in body

    def test_pull_search_results_handles_missing_and_none_fields(self, tmp_path):
        """SEC-01 & Defensiveness: trace with None / null / missing fields does not crash."""
        traces_dir = paths.traces_dir(str(tmp_path))
        os.makedirs(traces_dir, exist_ok=True)
        tdir_abs = os.path.abspath(traces_dir)

        mock_payload = {
            "traces": [
                {
                    "id": None,
                    "title": None,
                    "agent_type": None,
                    "tags": None,
                    "profile": None,
                    "outcome": None,
                    "context_text": None,
                    "solution_text": None,
                },
                {
                    # completely empty object
                },
            ]
        }

        async def _run():
            with patch.object(hub_client, "_call_tool", AsyncMock(return_value=mock_payload)):
                return await hub_client.pull_search_results(
                    "http://hub.example.com", "fake_key", str(tmp_path)
                )

        result = asyncio.run(_run())

        assert len(result.written_paths) >= 1
        for p in result.written_paths:
            assert os.path.abspath(p).startswith(tdir_abs + os.sep)
            fm, _ = frontmatter.read(p)
            assert isinstance(fm, dict)


# ==============================================================================
# SEC-02: Path Traversal & Schema Loading in validate.py:load_schema
# ==============================================================================
class TestLoadSchemaSecurity:
    """SEC-02: Strict validation of schema file names to prevent directory traversal."""

    @pytest.mark.parametrize(
        "bad_schema_name",
        [
            "../trace.schema.json",
            "../../etc/passwd.schema.json",
            "..\\..\\windows\\win.ini.json",
            "sub/trace.schema.json",
            "sub\\trace.schema.json",
            "/trace.schema.json",
            "C:\\trace.schema.json",
            "trace.py",
            "cli.py",
            "something.txt",
            "schema_without_extension",
            "",
            None,
            123,
            ["trace.schema.json"],
            {"name": "trace.schema.json"},
        ],
    )
    def test_load_schema_rejects_unsafe_and_invalid_names(self, bad_schema_name):
        with pytest.raises(ValueError, match="(Invalid or unsafe|Path traversal)"):
            validate.load_schema(bad_schema_name)

    def test_load_schema_raises_file_not_found_for_missing_valid_name(self):
        with pytest.raises(FileNotFoundError):
            validate.load_schema("nonexistent_test_12345.schema.json")

    def test_load_schema_loads_valid_canonical_schemas(self):
        trace_schema = validate.load_schema("trace.schema.json")
        assert isinstance(trace_schema, dict)
        assert "Trace" in trace_schema.get("title", "")
        assert "properties" in trace_schema

        lesson_schema = validate.load_schema("lesson.schema.json")
        assert isinstance(lesson_schema, dict)
        assert "Lesson" in lesson_schema.get("title", "")
        assert "properties" in lesson_schema


# ==============================================================================
# SEC-03: Subprocess Search Path Isolation in _shellout.py
# ==============================================================================
class TestSubprocessScriptLookupIsolation:
    """SEC-03: Reference scripts must not be resolved or executed from untrusted CWD."""

    def test_find_reference_script_ignores_untrusted_cwd(self, tmp_path, monkeypatch):
        # Create a malicious script in CWD
        cwd_dir = tmp_path / "untrusted_cwd"
        cwd_dir.mkdir()
        fake_relative = os.path.join("bench", "fake_script.py")
        fake_script = cwd_dir / fake_relative
        fake_script.parent.mkdir(parents=True, exist_ok=True)
        fake_script.write_text("# Malicious script in CWD\n", encoding="utf-8")

        monkeypatch.chdir(cwd_dir)

        # Separate clean root without the script
        clean_root = tmp_path / "clean_root"
        clean_root.mkdir()

        resolved = _shellout.find_reference_script(str(clean_root), fake_relative)
        # Must NOT find the script from CWD
        assert resolved is None, f"Expected None, but resolved untrusted CWD script: {resolved}"

    def test_find_reference_script_finds_script_in_repo_root(self, tmp_path, monkeypatch):
        # Store-root scripts run only with explicit opt-in
        # (COMMONTRACE_ALLOW_STORE_SCRIPTS=1): by default an untrusted clone
        # must not be able to plant an executable script the victim runs.
        monkeypatch.setenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", "1")
        clean_root = tmp_path / "repo_root"
        clean_root.mkdir()
        script_path = clean_root / "benchmark" / "my_script.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("# Valid repo script\n", encoding="utf-8")

        resolved = _shellout.find_reference_script(
            str(clean_root), os.path.join("benchmark", "my_script.py")
        )
        assert resolved is not None
        assert os.path.abspath(resolved) == os.path.abspath(str(script_path))

    def test_find_reference_script_resolves_packaged_reference_scripts(self, tmp_path):
        clean_root = tmp_path / "repo_root"
        clean_root.mkdir()

        resolved = _shellout.find_reference_script(
            str(clean_root), os.path.join("benchmark", "measure_performance.py")
        )
        assert resolved is not None
        assert os.path.exists(resolved)
        assert os.path.basename(resolved) == "measure_performance.py"

    def test_find_reference_script_ignores_store_root_by_default(self, tmp_path, monkeypatch):
        # Without opt-in, a store-root script must not resolve even when it
        # exists -- this is the untrusted-clone RCE guard. Packaged scripts
        # still resolve (previous test); only the store-root candidate is gated.
        monkeypatch.delenv("COMMONTRACE_ALLOW_STORE_SCRIPTS", raising=False)
        clean_root = tmp_path / "repo_root"
        clean_root.mkdir()
        script_path = clean_root / "benchmark" / "my_script.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text("# Planted script\n", encoding="utf-8")

        resolved = _shellout.find_reference_script(
            str(clean_root), os.path.join("benchmark", "my_script.py")
        )
        assert resolved is None


# ==============================================================================
# SEC-06: Tag Type Confusion in retrieval.py and reliability.py
# ==============================================================================
class TestTagTypeConfusionResilience:
    """SEC-06: Frontmatter with non-list or mixed-type tags must not raise TypeError."""

    @pytest.mark.parametrize(
        "malformed_tags",
        [
            None,
            123,
            True,
            False,
            3.14,
            "single-string-tag",
            {"key": "value"},
            ["valid_tag", 42, None, True, {"nested": "obj"}, ["nested_list"]],
            [None, None],
        ],
    )
    def test_retrieval_lesson_text_weighted_handles_arbitrary_tag_types(self, malformed_tags):
        fm = {
            "name": "test_lesson",
            "domain": "testing",
            "tags": malformed_tags,
            "applies_when": "when testing",
            "description": "test description",
        }
        # Must not raise TypeError
        sections = retrieval._lesson_text_weighted(fm)
        assert isinstance(sections, list)
        # Tag section is index 2 in _lesson_text_weighted
        tag_text, weight = sections[2]
        assert isinstance(tag_text, str)
        assert weight == 2.0

    @pytest.mark.parametrize(
        "malformed_tags",
        [
            None,
            100,
            False,
            "tag_string",
            {"dict": 1},
            ["alpha", 99, None, False],
        ],
    )
    def test_reliability_find_contradictions_handles_arbitrary_tag_types(
        self, malformed_tags
    ):
        fm1 = {
            "name": "lesson_1",
            "domain": "refactor",
            "tags": malformed_tags,
            "applies_when": "When refactoring code",
            "description": "Always check before delete",
            "status": "active",
        }
        fm2 = {
            "name": "lesson_2",
            "domain": "refactor",
            "tags": malformed_tags,
            "applies_when": "When refactoring code",
            "description": "Never check before delete",
            "status": "active",
        }

        # Must run minhash and contradiction search without TypeError
        contradictions = reliability.find_contradictions([fm1, fm2])
        assert isinstance(contradictions, list)


# ==============================================================================
# SEC-07: Root Dict Type Guards in validate.py:validate
# ==============================================================================
class TestValidateRootTypeGuard:
    """SEC-07: Non-dictionary instances passed to validate() must return descriptive errors."""

    @pytest.mark.parametrize(
        "non_dict_instance",
        [
            None,
            123,
            45.67,
            "a plain string",
            [1, 2, 3],
            True,
            False,
            set(),
            (1, 2, 3),
        ],
    )
    def test_validate_returns_error_on_non_dict(self, non_dict_instance):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        errors = validate.validate(non_dict_instance, schema)
        assert errors == ["Instance must be a dictionary / JSON object"]

    def test_validate_proceeds_normally_on_dict(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        assert validate.validate({"name": "valid"}, schema) == []
        assert validate.validate({}, schema) == ["missing required field 'name'"]


# ==============================================================================
# SEC-08: API Key Handling & Guidance in sync_cmd.py
# ==============================================================================
class TestSyncApiKeyHandling:
    """SEC-08: Sync command API key security and error messaging."""

    def test_sync_parser_help_mentions_env_var_preference(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        sync_cmd.add_parser(subparsers)

        help_text = subparsers.choices["sync"].format_help()
        assert "COMMONTRACE_HUB_API_KEY" in help_text
        assert "process table" in help_text or "environment variable" in help_text

    def test_sync_pull_prints_guidance_without_api_key(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("COMMONTRACE_HUB_API_KEY", raising=False)
        monkeypatch.delenv("COMMONTRACE_HUB_URL", raising=False)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        sync_cmd.add_parser(subparsers)

        args = parser.parse_args(["sync", "--pull", "--dest", str(tmp_path)])
        rc = args.func(args)
        assert rc == 0

        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "COMMONTRACE_HUB_API_KEY" in output
        assert "COMMONTRACE_HUB_URL" in output

    def test_sync_push_prints_guidance_without_api_key(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("COMMONTRACE_HUB_API_KEY", raising=False)
        monkeypatch.delenv("COMMONTRACE_HUB_URL", raising=False)

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="cmd")
        sync_cmd.add_parser(subparsers)

        args = parser.parse_args(["sync", "--push", "--dest", str(tmp_path)])
        rc = args.func(args)
        assert rc == 0

        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "COMMONTRACE_HUB_API_KEY" in output
        assert "COMMONTRACE_HUB_URL" in output
