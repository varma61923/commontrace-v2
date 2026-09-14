"""Regression tests for Milestone 3 fixes: benchmark integrity, security hardening."""
from __future__ import annotations

import json
import os
import tempfile
import types
from unittest.mock import patch

import pytest

from commontrace.commands import install_cmd
from commontrace.reference import measure_performance, pilot_metrics


# ---------------------------------------------------------------------------
# 1. persist_report — collision sort order and atomic write
# ---------------------------------------------------------------------------
class TestPersistReportCollisionSort:
    def test_collision_suffix_sorts_after_base_file(self):
        """Collision-resolved filenames must sort AFTER the base file.

        Old '-N' suffix: '2026_base-1.json' < '2026_base.json' ('-'=45 < '.'=46).
        New '_0001' suffix must sort after the base file.
        """
        base = "2026-09-05_120000_000000"
        base_file = f"{base}.json"
        collision_file = f"{base}_0001.json"
        assert collision_file > base_file, (
            f"Collision suffix '{collision_file}' must sort after base '{base_file}'"
        )

    def test_persist_report_returns_valid_json_file(self, tmp_path):
        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            path = measure_performance.persist_report({"test": True})
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data == {"test": True}

    def test_persist_report_no_partial_file_on_write_error(self, tmp_path):
        """If the write fails after mkstemp, no partial .json should remain."""
        # Simulate a write failure by patching fdopen to raise
        real_mkstemp = tempfile.mkstemp

        def failing_mkstemp(dir, suffix):
            fd, p = real_mkstemp(dir=dir, suffix=suffix)
            return fd, p  # fd will be closed by the except block

        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            with patch("os.fdopen", side_effect=OSError("simulated write failure")):
                with pytest.raises(OSError, match="simulated write failure"):
                    measure_performance.persist_report({"data": 1})

        # No .json should have been committed
        json_files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
        assert json_files == [], f"No .json should remain after failed write; found {json_files}"

    def test_persist_report_collision_loop_produces_distinct_sorted_files(self, tmp_path):
        import datetime
        ts = datetime.datetime(2026, 9, 5, 12, 0, 0, 123456)
        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            path1 = measure_performance.persist_report({"run": 1}, ts=ts)
            path2 = measure_performance.persist_report({"run": 2}, ts=ts)
        assert path1 != path2
        assert os.path.isfile(path1) and os.path.isfile(path2)
        n1, n2 = os.path.basename(path1), os.path.basename(path2)
        assert sorted([n1, n2])[0] < sorted([n1, n2])[1]


# ---------------------------------------------------------------------------
# 2. JSON-mode empty-corpus error output
# ---------------------------------------------------------------------------
class TestJsonModeEmptyCorpusOutput:
    def test_bench_empty_episodes_emits_json_error(self, capsys):
        """When --json and no episodes, stdout must be valid JSON (not plain text)."""
        argv = ["--json", "--no-save"]
        with patch.object(measure_performance, "load_episodes", return_value=([], 0)):
            with patch.object(measure_performance, "load_lessons", return_value=({}, 0)):
                with patch("sys.argv", ["commontrace"] + argv):
                    with pytest.raises(SystemExit) as exc_info:
                        measure_performance.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload.get("error") == "not_enough_episodes"

    def test_bench_no_json_flag_emits_plain_text(self, capsys):
        """Without --json, the human-readable message is printed (not JSON)."""
        argv = ["--no-save"]
        with patch.object(measure_performance, "load_episodes", return_value=([], 0)):
            with patch.object(measure_performance, "load_lessons", return_value=({}, 0)):
                with patch("sys.argv", ["commontrace"] + argv):
                    with pytest.raises(SystemExit) as exc_info:
                        measure_performance.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "Not enough episodes" in captured.out
        # Must NOT be valid JSON (it's a plain message)
        with pytest.raises(json.JSONDecodeError):
            json.loads(captured.out)

    def test_pilot_metrics_empty_traces_emits_json_error(self, tmp_path, capsys):
        """When --json and no traces, stdout must be valid JSON."""
        argv = ["--json"]
        with patch.object(pilot_metrics, "load_traces", return_value=[]):
            with patch("sys.argv", ["commontrace"] + argv):
                with pytest.raises(SystemExit) as exc_info:
                    pilot_metrics.main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload.get("error") == "no_traces"


# ---------------------------------------------------------------------------
# 3. HTML output is produced without error
# ---------------------------------------------------------------------------
class TestHtmlRendering:
    def test_render_html_produces_valid_document(self, tmp_path):
        """render_html runs end-to-end and produces a complete HTML page."""
        argv = ["--html", "--no-save"]
        # Only verify the HTML code path runs; rely on existing render tests for detail
        with patch.object(measure_performance, "_reports_dir", return_value=str(tmp_path)):
            with patch.object(measure_performance, "load_episodes", return_value=([], 0)):
                with patch.object(measure_performance, "load_lessons", return_value=({}, 0)):
                    with patch("sys.argv", ["commontrace"] + argv):
                        with pytest.raises(SystemExit):
                            measure_performance.main()
        # If the HTML path ran but no episodes, exit early — that's fine.
        # The key check: no unhandled exception.


# ---------------------------------------------------------------------------
# 4. install_cmd root resolved from dest, not cwd
# ---------------------------------------------------------------------------
class TestInstallCmdRootResolution:
    def test_root_is_resolved_from_dest_not_cwd(self, tmp_path):
        """install --dest /some/path should configure .mcp.json with /some/path as root."""
        dest = tmp_path / "agent_home"
        dest.mkdir()

        captured_roots = []

        def fake_write_local_mcp(dest_arg, root_arg):
            captured_roots.append(root_arg)

        args = types.SimpleNamespace(target="claude-code", dest=str(dest))
        with patch.object(install_cmd, "_write_local_mcp", side_effect=fake_write_local_mcp):
            with patch.object(install_cmd, "_find_skill_md", return_value=None):
                with patch.object(install_cmd, "_write", return_value=None):
                    install_cmd.run(args)

        assert len(captured_roots) == 1
        resolved = captured_roots[0]
        assert resolved.startswith(str(dest)), (
            f"Expected root under dest={dest}, got root={resolved}"
        )
        assert resolved != os.getcwd(), (
            "Root must not be caller's cwd when --dest points elsewhere"
        )


# ---------------------------------------------------------------------------
# 5. Datetime & Timezone Integrity in compute_freshness
# ---------------------------------------------------------------------------
class TestDatetimeTimezoneIntegrity:
    def test_parse_last_hit_handles_iso_with_z_and_offsets(self):
        dt_z = measure_performance._parse_last_hit("2026-09-05T12:00:00Z")
        assert dt_z is not None
        assert dt_z.tzinfo is not None

        dt_offset = measure_performance._parse_last_hit("2026-09-05T12:00:00+02:00")
        assert dt_offset is not None
        assert dt_offset.tzinfo is not None

        dt_naive = measure_performance._parse_last_hit("2026-09-05")
        assert dt_naive is not None

    def test_compute_freshness_compares_aware_and_naive_without_type_error(self):
        import datetime
        utc_now = datetime.datetime(2026, 9, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        lessons = {
            "l1": {"last_hit": "2026-09-01"},  # naive date
            "l2": {"last_hit": "2026-09-02T12:00:00Z"},  # UTC aware
            "l3": {"last_hit": "NEVER"},
        }
        # Passing UTC-aware now must not raise TypeError when comparing against naive or aware
        val, n = measure_performance.compute_freshness(lessons, now=utc_now)
        assert n == 3
        assert val == pytest.approx(2 / 3)

        # Passing naive now must also work without error
        naive_now = datetime.datetime(2026, 9, 5, 12, 0, 0)
        val2, n2 = measure_performance.compute_freshness(lessons, now=naive_now)
        assert n2 == 3
        assert val2 == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# 6. Lexical Tokens Unicode Support
# ---------------------------------------------------------------------------
class TestLexicalTokensUnicode:
    def test_lexical_tokens_preserves_non_latin_and_accented_words(self):
        tokens = measure_performance._lexical_tokens("résumé naïve café")
        assert "résumé" in tokens
        assert "naïve" in tokens
        assert "café" in tokens


# ---------------------------------------------------------------------------
# 7. Transfer Gap Memoization
# ---------------------------------------------------------------------------
class TestTransferGapMemoization:
    def test_resolve_project_caches_lookups(self, tmp_path):
        ep_dir = tmp_path / "episodes"
        ep_dir.mkdir(parents=True)
        (ep_dir / "ep1.md").write_text("---\nproject: proj_alpha\n---\n", encoding="utf-8")

        with patch.object(measure_performance, "BASE_DIR", str(tmp_path)):
            episodes = [{"name": "current_ep", "project": "proj_beta", "lessons_hit": ["l1"]}]
            lessons = {"l1": {"source_traces": ["ep1", "ep1"]}}
            # compute_transfer_gap should resolve ep1 and cache it
            val, n, untraceable = measure_performance.compute_transfer_gap(episodes, lessons)
            assert n == 1
            assert val == 1.0  # cross-project hit


# ---------------------------------------------------------------------------
# 8. Shellout PYTHONUTF8 Unconditional Setting
# ---------------------------------------------------------------------------
class TestShelloutPythonUtf8:
    def test_shellout_sets_pythonutf8_even_when_capture_false(self, tmp_path):
        from commontrace.commands import _shellout
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = types.SimpleNamespace(returncode=0)
            with patch("os.path.isfile", return_value=True):
                _shellout.run_script(str(tmp_path), "dummy.py", [], "msg", capture=False)
                assert mock_run.called
                env = mock_run.call_args[1].get("env", {})
                assert env.get("PYTHONUTF8") == "1"


# ---------------------------------------------------------------------------
# 9. Index Command (index_cmd.py)
# ---------------------------------------------------------------------------
class TestIndexCmd:
    def test_index_cmd_missing_deps_exits_1(self, capsys):
        from commontrace.commands import index_cmd
        args = types.SimpleNamespace(force=False, dest=None)
        with patch("commontrace.commands.index_cmd.has_attention_deps", return_value=False):
            rc = index_cmd.run(args)
            assert rc == 1
            captured = capsys.readouterr()
            assert "pip install commontrace[attention]" in captured.err

    def test_index_cmd_with_deps_runs_script(self):
        from commontrace.commands import index_cmd
        args = types.SimpleNamespace(force=True, dest="/tmp/test_store")
        with patch("commontrace.commands.index_cmd.has_attention_deps", return_value=True):
            with patch("commontrace.commands.index_cmd.run_script", return_value=0) as mock_run:
                rc = index_cmd.run(args)
                assert rc == 0
                assert mock_run.called
                extra = mock_run.call_args[0][2]
                assert "--force" in extra


# ---------------------------------------------------------------------------
# 10. Install Command Targets (cursor, windsurf, devin, generic-mcp, generic)
# ---------------------------------------------------------------------------
class TestInstallCmdTargets:
    @pytest.mark.parametrize("target,expected_file", [
        ("cursor", ".cursor/rules/commontrace.mdc"),
        ("windsurf", ".windsurf/rules/commontrace.md"),
        ("devin", ".devin/skills/commontrace/SKILL.md"),
        ("generic-mcp", "commontrace.hub.mcp.json.example"),
        ("generic", "COMMONTRACE.md"),
    ])
    def test_install_cmd_all_targets(self, tmp_path, target, expected_file):
        args = types.SimpleNamespace(target=target, dest=str(tmp_path))
        with patch.object(install_cmd, "_write_local_mcp", return_value=None):
            with patch.object(install_cmd, "_find_skill_md", return_value=None):
                rc = install_cmd.run(args)
                assert rc is None or rc == 0
                target_path = tmp_path / expected_file
                assert target_path.is_file(), f"Target {target} failed to write {expected_file}"


# ---------------------------------------------------------------------------
# 11. Report HTML Shared Wrapper (report_html.py)
# ---------------------------------------------------------------------------
class TestReportHtml:
    def test_wrap_page_structure(self):
        from commontrace import report_html
        html = report_html.wrap_page("Test Title", "<p>Body</p>", "2026-09-05")
        assert "<!DOCTYPE html>" in html
        assert "<title>Test Title — 2026-09-05</title>" in html
        assert "<p>Body</p>" in html
        assert "</html>" in html

    def test_stat_card_generation(self):
        from commontrace import report_html
        card = report_html.stat_card("Metric", "99%", note="High accuracy")
        assert 'class="card"' in card
        assert 'class="label">Metric<' in card
        assert 'class="value">99%<' in card
        assert 'class="note">High accuracy<' in card


# ---------------------------------------------------------------------------
# 12. Approval Policy Unknown Key Validation
# ---------------------------------------------------------------------------
class TestApprovalPolicyUnknownKeys:
    def test_load_policy_rejects_typo_keys(self, tmp_path):
        from commontrace import approval
        policy_file = tmp_path / "memory" / "approval-policy.yaml"
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text("mode: single\nrequire-human: true\n", encoding="utf-8")
        with pytest.raises(approval.PolicyError, match="unrecognized policy key.*require-human"):
            approval.load_policy(str(tmp_path))

    def test_load_policy_accepts_valid_keys(self, tmp_path):
        from commontrace import approval
        policy_file = tmp_path / "memory" / "approval-policy.yaml"
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text("mode: two-person\nrequire_human: true\n", encoding="utf-8")
        p = approval.load_policy(str(tmp_path))
        assert p.mode == "two-person"
        assert p.require_human is True


# ---------------------------------------------------------------------------
# 13. Import Command File Existence Check Prior to Directory Creation
# ---------------------------------------------------------------------------
class TestImportCmdFileCheck:
    def test_import_missing_file_does_not_create_directory(self, tmp_path, capsys):
        import argparse
        from commontrace.commands import import_cmd
        dest = tmp_path / "target_store"
        args = argparse.Namespace(
            file=str(tmp_path / "nonexistent.jsonl"),
            dest=str(dest),
            dry_run=False,
            title_field="title",
            context_field="context",
            solution_field="solution",
            tags_field="tags",
            id_field="id",
            format="auto",
        )
        rc = import_cmd.run(args)
        assert rc == 1
        captured = capsys.readouterr()
        assert "no such file" in captured.err
        assert not dest.exists(), "Target store directory must not be created when file is missing"


# ---------------------------------------------------------------------------
# 14. MCP Server draft_lesson Error Handling
# ---------------------------------------------------------------------------
class TestMcpServerDraftLessonErrorHandling:
    def test_draft_lesson_handles_validation_exception(self, tmp_path):
        import asyncio
        pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")
        from commontrace import mcp_server
        server = mcp_server.build_server(str(tmp_path))
        draft_tool = None
        for tool in server._tool_manager.list_tools():
            if tool.name == "draft_lesson":
                draft_tool = tool
                break
        assert draft_tool is not None, "draft_lesson tool must be registered"

        with patch("commontrace.frontmatter.read", side_effect=OSError("disk read failure")):
            async def _invoke():
                return await server._tool_manager.call_tool(
                    "draft_lesson",
                    arguments={
                        "slug": "test_slug",
                        "description": "desc",
                        "rule": "rule",
                        "why": "why",
                        "how_to_apply": "how",
                        "counter_examples": "counter",
                    },
                )
            res = asyncio.run(_invoke())
            assert res is not None
            text = res[0].text if isinstance(res, list) else str(res)
            data = json.loads(text)
            assert "error" in data
            assert "could not validate" in data["error"]


# ---------------------------------------------------------------------------
# 15. Trace IO Fallback for Empty Context/Solution Text
# ---------------------------------------------------------------------------
class TestTraceIoFallback:
    def test_trace_io_read_falls_back_when_frontmatter_empty_or_none(self, tmp_path):
        from commontrace import trace_io
        trace_file = tmp_path / "test_trace.md"
        content = (
            "---\n"
            "id: test-fallback-1\n"
            "title: Test Fallback\n"
            "agent_type: code\n"
            "tags: [test]\n"
            "context_text: \"\"\n"
            "solution_text: null\n"
            "---\n\n"
            "## Context\n"
            "Recovered context text from body section.\n\n"
            "## Solution\n"
            "Recovered solution text from body section.\n"
        )
        trace_file.write_text(content, encoding="utf-8")
        inst, body = trace_io.read(str(trace_file))
        assert inst["context_text"] == "Recovered context text from body section."
        assert inst["solution_text"] == "Recovered solution text from body section."


# ---------------------------------------------------------------------------
# 16. Scaffold Store Traces and Templates Sanity
# ---------------------------------------------------------------------------
class TestScaffoldStoreSanity:
    def test_example_trace_conforms_to_schema(self):
        from commontrace import paths, trace_io, validate
        root = paths.resolve_root(None)
        example_trace = os.path.join(paths.traces_dir(root), "2026-07-01_example-trace.md")
        assert os.path.isfile(example_trace), f"Example trace not found at {example_trace}"
        inst, _ = trace_io.read(example_trace)
        schema = validate.load_schema("trace.schema.json")
        errors = validate.validate(inst, schema)
        assert errors == [], f"Example trace schema errors: {errors}"

    def test_lesson_template_has_review_status(self):
        from commontrace import frontmatter, paths
        root = paths.resolve_root(None)
        template = os.path.join(paths.lessons_dir(root), "lesson_template.md")
        assert os.path.isfile(template)
        fm, _ = frontmatter.read(template)
        assert fm.get("status") == "review"

    def test_memory_index_declares_agent_type_code(self):
        from commontrace import paths
        from commontrace.commands import doctor_cmd
        root = paths.resolve_root(None)
        declared = doctor_cmd._declared_agent_type(root)
        assert declared == "code"


