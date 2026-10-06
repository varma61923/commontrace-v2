from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from commontrace import frontmatter, import_data, validate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestYamlDatesLoadAsStrings:
    def test_unquoted_date_is_a_string(self, tmp_path):
        p = tmp_path / "l.md"
        p.write_text("---\nname: x\nlast_hit: 2026-07-01\n---\n\nbody\n", encoding="utf-8")
        fm, _ = frontmatter.read(str(p))
        assert fm["last_hit"] == "2026-07-01"
        assert isinstance(fm["last_hit"], str)

    def test_unquoted_timestamp_is_a_string(self, tmp_path):
        p = tmp_path / "t.md"
        p.write_text("---\ncreated_at: 2026-07-01 12:30:00\n---\n\nbody\n", encoding="utf-8")
        fm, _ = frontmatter.read(str(p))
        assert isinstance(fm["created_at"], str)

    def test_real_booleans_still_parse_as_booleans(self, tmp_path):
        p = tmp_path / "b.md"
        p.write_text("---\na: true\nb: false\nc: NO\nd: on\n---\n\nbody\n", encoding="utf-8")
        fm, _ = frontmatter.read(str(p))
        assert fm["a"] is True and fm["b"] is False
        assert fm["c"] == "NO" and fm["d"] == "on"

    def test_the_shipped_example_lessons_validate(self):
        lessons_dir = os.path.join(REPO_ROOT, "memory", "lessons")
        schema = validate.load_schema("lesson.schema.json")
        checked = 0
        for name in os.listdir(lessons_dir):
            if not name.startswith("lesson_example_"):
                continue
            fm, _ = frontmatter.read(os.path.join(lessons_dir, name))
            assert validate.validate(fm, schema) == [], f"{name} fails its own schema"
            checked += 1
        assert checked >= 1, "no example lessons found to check"


class TestImportKeepsEveryOutcomeField:
    def test_baseline_survives_import(self):
        out = import_data._extract_outcome(
            {"title": "T", "resolved": "true", "baseline": "true"}
        )
        assert out.get("baseline") is True

    def test_importer_handles_every_outcome_field_the_schema_declares(self):
        schema = validate.load_schema("trace.schema.json")
        declared = set(schema["properties"]["outcome"]["properties"])
        handled = set(import_data._OUTCOME_BOOL_FIELDS) | set(import_data._OUTCOME_INT_FIELDS)
        assert declared - handled == set(), f"importer silently drops: {sorted(declared - handled)}"


class TestInstallDoesNotFollowSymlinks:
    def test_a_symlinked_destination_is_replaced_not_followed(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET_PAYLOAD", encoding="utf-8")
        dest = tmp_path / "proj"
        skill_dir = dest / ".claude" / "skills" / "commontrace"
        skill_dir.mkdir(parents=True)
        link = skill_dir / "SKILL.md"
        os.symlink(secret, link)

        subprocess.run(
            [sys.executable, "-m", "commontrace", "install",
             "--target", "claude-code", "--dest", str(dest)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )

        assert secret.read_text(encoding="utf-8") == "SECRET_PAYLOAD"
        assert not os.path.islink(link), "the symlink should have been replaced by a real file"


class TestCaptureWritesAtomicallyAndUniquely:
    def _capture(self, dest, title):
        return subprocess.run(
            [sys.executable, "-m", "commontrace", "capture",
             "--title", title, "--context", "c", "--solution", "s",
             "--agent-type", "code", "--dest", str(dest)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_same_titled_captures_do_not_overwrite_each_other(self, tmp_path):
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init",
             "--agent-type", "code", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        for _ in range(3):
            r = self._capture(tmp_path, "same title every time")
            assert r.returncode == 0, r.stderr

        traces = os.listdir(tmp_path / "memory" / "traces")
        written = [t for t in traces if t.endswith(".md") and t != "README.md"]
        assert len(written) == 3, f"expected 3 distinct traces, got {written}"

    def test_the_filename_carries_the_trace_id(self, tmp_path):
        date = datetime.date.today().isoformat()
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init",
             "--agent-type", "code", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        r = self._capture(tmp_path, "unique naming")
        out_path = r.stdout.strip()
        name = os.path.basename(out_path)
        assert name.startswith(f"{date}_unique-naming_")
        assert len(name) > len(f"{date}_unique-naming_.md")

    def test_the_written_trace_round_trips(self, tmp_path):
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init",
             "--agent-type", "code", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        r = self._capture(tmp_path, "round trip")
        fm, body = frontmatter.read(r.stdout.strip())
        assert fm["title"] == "round trip"
        assert "## Context" in body and "## Solution" in body


class TestPushSkipsAlreadyPushedLessons:
    def _lesson(self, tmp_path, slug, hub_trace_id):
        d = tmp_path / "memory" / "lessons"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{slug}.md"
        fm = {
            "name": slug, "description": "d", "tags": [], "agent_type": "code",
            "status": "active", "applies_when": "w", "hub_trace_id": hub_trace_id,
        }
        frontmatter.write(str(p), fm, "## Rule\nr\n\n## How to apply\nh\n")
        return p

    def test_a_lesson_with_a_hub_trace_id_is_never_recontributed(self, tmp_path, monkeypatch):
        import asyncio

        from commontrace import hub_client

        calls = []

        async def _fake_call_tool(hub_url, api_key, tool, args, **kw):
            calls.append((tool, args))
            return {"id": "newly-minted-id"}

        monkeypatch.setattr(hub_client, "_call_tool", _fake_call_tool)
        self._lesson(tmp_path, "lesson_already_pushed", "existing-uuid")

        results = asyncio.run(hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path)))

        assert all(tool == "amend_trace" for tool, _args in calls), (
            "contribute_trace must never be called for an already-pushed lesson"
        )
        assert len(results) == 1
        assert results[0].hub_trace_id == "newly-minted-id"

    def test_a_never_pushed_lesson_is_contributed_with_an_idempotency_key(
        self, tmp_path, monkeypatch
    ):
        import asyncio

        from commontrace import hub_client

        calls = []

        async def _fake_call_tool(hub_url, api_key, tool, args, **kw):
            calls.append((tool, args))
            return {"id": "minted-id"}

        monkeypatch.setattr(hub_client, "_call_tool", _fake_call_tool)
        self._lesson(tmp_path, "lesson_fresh", None)

        results = asyncio.run(hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path)))

        assert len(calls) == 1
        tool, args = calls[0]
        assert tool == "contribute_trace"
        assert args["idempotency_key"] == "lesson:lesson_fresh"
        assert results[0].skipped is False
        assert results[0].hub_trace_id == "minted-id"

    def test_pushing_twice_contributes_exactly_once(self, tmp_path, monkeypatch):
        import asyncio

        from commontrace import hub_client

        calls = []

        async def _fake_call_tool(hub_url, api_key, tool, args, **kw):
            calls.append((tool, args))
            return {"id": "minted-id"}

        monkeypatch.setattr(hub_client, "_call_tool", _fake_call_tool)
        self._lesson(tmp_path, "lesson_pushed_twice", None)

        asyncio.run(hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path)))
        asyncio.run(hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path)))

        assert len(calls) == 1, f"expected one contribute_trace across two runs, got {len(calls)}"


class TestTokenizersHandleNonAscii:
    def test_cjk_and_cyrillic_produce_tokens(self):
        from commontrace import distill, retrieval

        assert retrieval._tokenize("数据库连接失败") != []
        assert distill._tokenize("Ошибка подключения") != set()

    def test_accented_latin_is_not_mutilated(self):
        from commontrace import retrieval

        assert retrieval._tokenize("résumé") == ["résumé"]

    def test_ascii_behaviour_is_unchanged(self):
        from commontrace import retrieval

        assert retrieval._tokenize("The database connection failed") == [
            "database", "connection", "failed",
        ]


class TestSectionParsing:
    def test_first_occurrence_wins_not_last(self):
        from commontrace import hub_client

        body = "## Rule\nReal rule\n\n## Why\nDocs say:\n```md\n## Rule\nfrom the docs\n```\n"
        assert hub_client._lesson_sections(body)["rule"] == "Real rule"

    def test_trace_sections_are_case_insensitive(self, tmp_path):
        from commontrace import trace_io

        p = tmp_path / "t.md"
        p.write_text(
            "---\nid: x\ntitle: T\n---\n\n## context\nlower ctx\n\n## solution\nlower sol\n",
            encoding="utf-8",
        )
        inst, _ = trace_io.read(str(p))
        assert inst["context_text"] == "lower ctx"
        assert inst["solution_text"] == "lower sol"

    def test_title_case_lesson_headings_match(self):
        from commontrace import hub_client

        sections = hub_client._lesson_sections("## Rule\nr\n\n## How To Apply\nsteps here\n")
        assert sections.get("how to apply") == "steps here"


class TestBenchmarkSurvivesBadUsesValues:
    def test_safe_int_coerces_without_raising(self):
        from commontrace.reference.measure_performance import _safe_int

        assert _safe_int(1) == 1
        assert _safe_int("2") == 2
        assert _safe_int(None) == 0
        assert _safe_int("nonsense") == 0

    def test_booleans_do_not_count_as_uses(self):
        from commontrace.reference.measure_performance import _safe_int

        assert _safe_int(True) == 0

    def test_mixed_types_do_not_crash_the_benchmark(self):
        from commontrace.reference.measure_performance import compute_extras

        lessons = {"a": {"uses": 3}, "b": {"uses": "2"}, "c": {"uses": None}, "d": {"uses": True}}
        compute_extras([], lessons)


class TestCliReportsOperationalErrorsCleanly:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "commontrace", *args],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_malformed_json_is_a_message_not_a_traceback(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json at all", encoding="utf-8")
        r = self._run("overlap", "report", "--ours", str(bad), "--theirs", str(bad))
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "[commontrace] error:" in r.stderr

    def test_a_signature_file_missing_required_keys_is_reported(self, tmp_path):
        import json as _json

        sig = tmp_path / "sig.json"
        sig.write_text(_json.dumps({"nope": 1}), encoding="utf-8")
        r = self._run("overlap", "report", "--ours", str(sig), "--theirs", str(sig))
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "fleet_label" in r.stderr

    def test_a_genuine_bug_still_raises(self):
        import commontrace.cli as cli

        src = __import__("inspect").getsource(cli.main)
        assert "except Exception" not in src
        assert "except BaseException" not in src


class TestExperimentLoopCloses:
    def _run(self, *args, dest):
        return subprocess.run(
            [sys.executable, "-m", "commontrace", *args, "--dest", str(dest)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def _store_with_lesson(self, tmp_path):
        self._run("init", "--agent-type", "code", dest=tmp_path)
        ldir = tmp_path / "memory" / "lessons"
        ldir.mkdir(parents=True, exist_ok=True)
        frontmatter.write(
            str(ldir / "lesson_pagination.md"),
            {
                "name": "lesson_pagination", "description": "Use keyset pagination",
                "tags": ["pagination"], "agent_type": "code", "domain": "databases",
                "importance": 4, "applies_when": "paginating a large table",
                "do_not_apply_when": "small sets", "uses": 0, "last_hit": "NEVER",
                "status": "active",
            },
            "## Rule\nUse keyset pagination.\n\n## How to apply\nPage on an immutable key.\n",
        )
        return tmp_path

    def test_capture_accepts_an_occasion_id_and_uses_it_as_the_trace_id(self, tmp_path):
        self._store_with_lesson(tmp_path)
        r = self._run("capture", "--title", "t", "--context", "c", "--solution", "s",
                      "--agent-type", "code", "--resolved",
                      "--occasion-id", "task-100", dest=tmp_path)
        assert r.returncode == 0, r.stderr
        inst, _ = trace_io_read(r.stdout.strip())
        assert inst["id"] == "task-100"

    def test_the_arm_joins_to_the_outcome(self, tmp_path):
        self._store_with_lesson(tmp_path)
        self._run("query", "paginating a large table", "--experiment",
                  "--occasion-id", "task-100", dest=tmp_path)
        self._run("capture", "--title", "t", "--context", "large table", "--solution", "s",
                  "--agent-type", "code", "--resolved",
                  "--occasion-id", "task-100", dest=tmp_path)
        r = self._run("experiment", dest=tmp_path)
        combined = r.stdout + r.stderr
        assert "none have a matching" not in combined, "the arm failed to join to its outcome"

    def test_without_an_occasion_id_nothing_joins(self, tmp_path):
        self._store_with_lesson(tmp_path)
        self._run("query", "paginating a large table", "--experiment",
                  "--occasion-id", "task-200", dest=tmp_path)
        self._run("capture", "--title", "t", "--context", "large table", "--solution", "s",
                  "--agent-type", "code", "--resolved", dest=tmp_path)
        r = self._run("experiment", dest=tmp_path)
        assert "none have a matching" in (r.stdout + r.stderr)

    def test_an_empty_occasion_id_is_refused(self, tmp_path):
        self._store_with_lesson(tmp_path)
        r = self._run("capture", "--title", "t", "--context", "c", "--solution", "s",
                      "--agent-type", "code", "--occasion-id", "   ", dest=tmp_path)
        assert r.returncode == 1
        assert "cannot be empty" in r.stderr

    def test_an_unsafe_occasion_id_cannot_escape_the_traces_directory(self, tmp_path):
        self._store_with_lesson(tmp_path)
        r = self._run("capture", "--title", "t", "--context", "c", "--solution", "s",
                      "--agent-type", "code", "--resolved",
                      "--occasion-id", "../../etc/pwned", dest=tmp_path)
        assert r.returncode == 0, r.stderr
        out_path = os.path.realpath(r.stdout.strip())
        traces_dir = os.path.realpath(str(tmp_path / "memory" / "traces"))
        assert out_path.startswith(traces_dir), f"escaped to {out_path}"
        inst, _ = trace_io_read(out_path)
        assert inst["id"] == "../../etc/pwned", "the id itself must not be rewritten"


class TestCaptureOccasionIdMergesRatherThanOverwrites:
    def _run(self, *args, dest):
        return subprocess.run(
            [sys.executable, "-m", "commontrace", *args, "--dest", str(dest)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_recapture_preserves_original_context_and_solution_by_default(self, tmp_path):
        self._run("init", "--agent-type", "code", dest=tmp_path)
        first = self._run(
            "capture", "--title", "original title", "--context", "original context",
            "--solution", "original solution", "--agent-type", "code",
            "--occasion-id", "task-1", dest=tmp_path,
        )
        assert first.returncode == 0, first.stderr
        out_path = first.stdout.strip()

        second = self._run(
            "capture", "--title", "placeholder", "--context", "placeholder",
            "--solution", "placeholder", "--agent-type", "code", "--resolved",
            "--occasion-id", "task-1", dest=tmp_path,
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == out_path, "must update the SAME file, not write a second one"

        inst, _ = trace_io_read(out_path)
        assert inst["title"] == "original title"
        assert inst["context_text"] == "original context"
        assert inst["solution_text"] == "original solution"
        assert inst["outcome"]["resolved"] is True, "the outcome must still merge in"

    def test_recapture_merges_outcome_and_preserves_tags_agent_id_and_created_at(self, tmp_path):
        self._run("init", "--agent-type", "code", dest=tmp_path)
        first = self._run(
            "capture", "--title", "t", "--context", "c", "--solution", "s",
            "--agent-type", "code", "--tags", "linux,gcc", "--tokens-used", "4200",
            "--agent-id", "worker-9", "--occasion-id", "task-3", dest=tmp_path,
        )
        assert first.returncode == 0, first.stderr
        out_path = first.stdout.strip()
        original = trace_io_read(out_path)[0]

        second = self._run(
            "capture", "--title", "placeholder", "--context", "placeholder",
            "--solution", "placeholder", "--agent-type", "code", "--resolved",
            "--occasion-id", "task-3", dest=tmp_path,
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == out_path

        inst, _ = trace_io_read(out_path)
        assert inst["outcome"]["tokens_used"] == 4200, "tokens_used from the first call was dropped"
        assert inst["outcome"]["resolved"] is True, "resolved from the second call did not merge in"
        assert inst["tags"] == ["linux", "gcc"], "tags from the first call were dropped"
        assert inst["agent_id"] == "worker-9", "agent_id from the first call was dropped"
        assert inst["created_at"] == original["created_at"], "created_at must not change on re-capture"

    def test_recapture_new_tags_and_agent_id_replace_rather_than_merge(self, tmp_path):
        self._run("init", "--agent-type", "code", dest=tmp_path)
        first = self._run(
            "capture", "--title", "t", "--context", "c", "--solution", "s",
            "--agent-type", "code", "--tags", "linux,gcc", "--agent-id", "worker-9",
            "--occasion-id", "task-4", dest=tmp_path,
        )
        out_path = first.stdout.strip()

        self._run(
            "capture", "--title", "t", "--context", "c", "--solution", "s",
            "--agent-type", "code", "--tags", "billing", "--agent-id", "worker-10",
            "--occasion-id", "task-4", dest=tmp_path,
        )

        inst, _ = trace_io_read(out_path)
        assert inst["tags"] == ["billing"]
        assert inst["agent_id"] == "worker-10"

    def test_overwrite_flag_replaces_the_narrative(self, tmp_path):
        self._run("init", "--agent-type", "code", dest=tmp_path)
        first = self._run(
            "capture", "--title", "original title", "--context", "original context",
            "--solution", "original solution", "--agent-type", "code",
            "--occasion-id", "task-2", dest=tmp_path,
        )
        out_path = first.stdout.strip()

        self._run(
            "capture", "--title", "corrected title", "--context", "corrected context",
            "--solution", "corrected solution", "--agent-type", "code", "--resolved",
            "--occasion-id", "task-2", "--overwrite", dest=tmp_path,
        )

        inst, _ = trace_io_read(out_path)
        assert inst["title"] == "corrected title"
        assert inst["context_text"] == "corrected context"
        assert inst["solution_text"] == "corrected solution"
        assert inst["outcome"]["resolved"] is True


def trace_io_read(path):
    from commontrace import trace_io

    return trace_io.read(path)


class TestHoldoutLogIntegrity:
    def _store(self, tmp_path):
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init", "--agent-type", "code",
             "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        ldir = tmp_path / "memory" / "lessons"
        ldir.mkdir(parents=True, exist_ok=True)
        for i in range(6):
            frontmatter.write(
                str(ldir / f"lesson_topic{i}.md"),
                {"name": f"lesson_topic{i}", "description": f"desc {i}",
                 "tags": ["pagination"], "agent_type": "code", "domain": "databases",
                 "importance": 3, "applies_when": "paginating a large table",
                 "do_not_apply_when": "n/a", "uses": 0, "last_hit": "NEVER",
                 "status": "active"},
                "## Rule\nr\n\n## How to apply\nh\n",
            )
        return tmp_path

    def test_concurrent_writers_produce_only_parseable_lines(self, tmp_path):
        import concurrent.futures
        import json as _json

        self._store(tmp_path)

        def one(n):
            return subprocess.run(
                [sys.executable, "-m", "commontrace", "query", "paginating a large table",
                 "--experiment", "--occasion-id", f"occ-{n}", "--dest", str(tmp_path)],
                cwd=REPO_ROOT, capture_output=True, text=True,
            ).returncode

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            assert all(rc == 0 for rc in pool.map(one, range(8)))

        log = tmp_path / "memory" / "holdout_log.jsonl"
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines, "nothing was logged"
        for ln in lines:
            _json.loads(ln)

    def test_a_corrupt_line_is_reported_not_silently_dropped(self, tmp_path):
        self._store(tmp_path)
        subprocess.run(
            [sys.executable, "-m", "commontrace", "query", "paginating a large table",
             "--experiment", "--occasion-id", "occ-1", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        log = tmp_path / "memory" / "holdout_log.jsonl"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write('{"occasion_id": "occ-2", "lesso\n')

        r = subprocess.run(
            [sys.executable, "-m", "commontrace", "experiment", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert "unparseable line" in r.stderr
        assert "unreliable" in r.stderr

    def test_a_clean_log_produces_no_warning(self, tmp_path):
        self._store(tmp_path)
        subprocess.run(
            [sys.executable, "-m", "commontrace", "query", "paginating a large table",
             "--experiment", "--occasion-id", "occ-1", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        r = subprocess.run(
            [sys.executable, "-m", "commontrace", "experiment", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert "unparseable line" not in r.stderr


class TestDoctorReportsFailuresInItsExitCode:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "commontrace", "doctor", *args],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_a_missing_store_exits_non_zero(self, tmp_path):
        r = self._run("--dest", str(tmp_path / "nope"))
        assert r.returncode == 1
        assert "critical check(s) failed" in r.stdout

    def test_a_healthy_store_exits_zero(self):
        assert self._run().returncode == 0

    def test_info_conditions_do_not_fail_the_run(self):
        r = self._run()
        assert r.returncode == 0
        assert "critical check(s) failed" not in r.stdout

    def test_info_never_registers_a_failure(self, capsys):
        from commontrace.commands import doctor_cmd

        before = list(doctor_cmd._FAILURES)
        try:
            doctor_cmd._info("an optional thing", "not installed; optional")
            assert doctor_cmd._FAILURES == before
        finally:
            doctor_cmd._FAILURES[:] = before
        assert "[INFO] an optional thing" in capsys.readouterr().out

    def test_a_fresh_store_with_no_lessons_is_not_a_failure(self, tmp_path):
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init", "--agent-type", "code",
             "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        r = self._run("--dest", str(tmp_path))
        assert r.returncode == 0
        assert "[WARN]" in r.stdout, "a fresh store should still SHOW warnings"


class TestValidatorRejectsTupleFormItems:
    def test_list_typed_items_raises_unsupported_not_attribute_error(self):
        from commontrace import validate

        schema = {"type": "object",
                  "properties": {"tags": {"type": "array", "items": [{"type": "string"}]}}}
        with pytest.raises(validate.UnsupportedSchemaError):
            validate.assert_supported_schema(schema)

    def test_the_supported_form_still_passes(self):
        from commontrace import validate

        schema = {"type": "object",
                  "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
        validate.assert_supported_schema(schema)

    def test_the_shipped_schemas_still_load(self):
        from commontrace import validate

        for name in ("trace.schema.json", "lesson.schema.json"):
            validate.assert_supported_schema(validate.load_schema(name))


class TestReportKeepsPlaceholderRows:
    def test_a_dash_only_data_row_is_not_mistaken_for_a_separator(self):
        from commontrace.reference.measure_performance import _md_to_html_fragment

        html = _md_to_html_fragment(
            "| Metric | Value |\n|---|---|\n| lexical | 0.42 |\n| - | - |\n| fresh | 0.9 |\n"
        )
        assert html.count("<tr>") - 1 == 3, "a data row was dropped"
        assert "<td>-</td>" in html

    def test_real_separators_are_still_skipped(self):
        from commontrace.reference.measure_performance import _md_to_html_fragment

        html = _md_to_html_fragment("| A | B |\n|---|---|\n| 1 | 2 |\n")
        assert "<td>---</td>" not in html


class TestEveryThresholdFlagIsActuallyImplemented:
    def test_no_threshold_flag_reports_itself_as_unimplemented(self):
        for flag in ("--threshold-lexical=0.99", "--threshold-freshness=0.5",
                     "--threshold-composite=0.7", "--threshold-semantic=0.99"):
            r = subprocess.run(
                [sys.executable, "-m", "commontrace", "bench", flag],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            assert "not implemented" not in r.stderr, flag

    def test_lexical_duplicates_are_actually_detected(self):
        from commontrace.reference import measure_performance as mp

        lessons = {
            "lesson_a": {"description": "Reuse the gateway idempotency key on a refund retry",
                         "applies_when": "refund retry returns 409"},
            "lesson_b": {"description": "On a refund retry reuse the gateway idempotency key",
                         "applies_when": "refund retry returns 409"},
            "lesson_c": {"description": "Escalate billing disputes to the finance queue",
                         "applies_when": "customer disputes a charge"},
        }
        result = mp.compute_lexical_duplicates(lessons, 0.6)
        assert [{p["a"], p["b"]} for p in result["pairs"]] == [{"lesson_a", "lesson_b"}]

    def test_freshness_measures_recent_hits_not_merely_any_hit(self):
        import datetime

        from commontrace.reference import measure_performance as mp

        now = datetime.datetime(2026, 6, 1)
        recent = (now - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        old = (now - datetime.timedelta(days=mp.FRESHNESS_WINDOW_DAYS + 30)).strftime("%Y-%m-%d")
        value, n = mp.compute_freshness(
            {"a": {"last_hit": recent}, "b": {"last_hit": old},
             "c": {"last_hit": "NEVER"}, "d": {"last_hit": recent}},
            now=now,
        )
        assert n == 4
        assert value == 0.5

    def test_the_composite_score_names_its_own_components(self):
        from commontrace.reference import measure_performance as mp

        composite = mp.compute_composite({
            "lesson_quality": {"value": 0.8},
            "implicit_retrieval": {"strict": 0.6},
            "n_lessons": 10,
            "extras": {"never_hit": ["x", "y"]},
            "freshness": {"value": 1.0},
        })
        assert composite["value"] == pytest.approx((0.8 + 0.6 + 0.8 + 1.0) / 4)
        assert set(composite["components"]) == {
            "lesson_quality", "implicit_retrieval", "lesson_coverage", "freshness"
        }

    def test_a_threshold_that_is_not_passed_raises_no_alert(self):
        from commontrace.reference import measure_performance as mp

        report = {
            "lesson_quality": {"value": 0.9}, "implicit_retrieval": {"strict": 0.9},
            "n_lessons": 1, "extras": {"never_hit": [], "importance_lessons": {}},
            "freshness": {"value": 0.0, "n": 1, "window_days": 90},
            "composite": {"value": 0.0, "components": {}},
            "lexical_duplicates": {"pairs": [{"a": "x", "b": "y", "score": 1.0}], "n_lessons": 2},
        }
        thresholds = {"quality": 0.7, "retrieval": 0.5, "never_hit": 0.3, "unimodal": 0.95,
                      "lexical": None, "freshness": None, "composite": None}
        assert mp.compute_alerts(report, thresholds) == []

    def test_each_threshold_fires_when_it_is_breached(self):
        from commontrace.reference import measure_performance as mp

        report = {
            "lesson_quality": {"value": 0.9}, "implicit_retrieval": {"strict": 0.9},
            "n_lessons": 1, "extras": {"never_hit": [], "importance_lessons": {}},
            "freshness": {"value": 0.1, "n": 10, "window_days": 90},
            "composite": {"value": 0.2, "components": {"freshness": 0.1}},
            "lexical_duplicates": {"pairs": [{"a": "x", "b": "y", "score": 1.0}], "n_lessons": 2},
        }
        alerts = mp.compute_alerts(
            report,
            {"quality": 0.7, "retrieval": 0.5, "never_hit": 0.3, "unimodal": 0.95,
             "lexical": 0.6, "freshness": 0.5, "composite": 0.7},
        )
        joined = " | ".join(alerts)
        assert "near-duplicate lesson pair" in joined
        assert "freshness" in joined
        assert "composite health" in joined


class TestRootResolutionMatchesTheReferenceScripts:
    def test_legacy_env_var_is_honoured(self, tmp_path, monkeypatch):
        from commontrace import paths

        monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
        monkeypatch.setenv("JUSTDOIT_ROOT", str(tmp_path))
        assert paths.resolve_root(None) == os.path.abspath(str(tmp_path))

    def test_the_modern_var_still_wins(self, tmp_path, monkeypatch):
        from commontrace import paths

        monkeypatch.setenv("COMMONTRACE_ROOT", str(tmp_path / "modern"))
        monkeypatch.setenv("JUSTDOIT_ROOT", str(tmp_path / "legacy"))
        assert paths.resolve_root(None) == os.path.abspath(str(tmp_path / "modern"))

    def test_an_explicit_dest_still_beats_both(self, tmp_path, monkeypatch):
        from commontrace import paths

        monkeypatch.setenv("COMMONTRACE_ROOT", str(tmp_path / "env"))
        assert paths.resolve_root(str(tmp_path / "explicit")) == os.path.abspath(
            str(tmp_path / "explicit")
        )


class TestCoreSuiteNeedsNoHubDependencies:
    def test_no_test_in_this_suite_requires_pytest_asyncio(self):
        import re

        offenders = []
        for path in sorted(Path(REPO_ROOT).glob("tests/test_*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*@pytest\.mark\.asyncio", text, re.M):
                offenders.append(path.name)
            if re.search(r"^\s*async def test_", text, re.M):
                offenders.append(path.name)
        assert not offenders, (
            "tests/ must run without pytest-asyncio; drive coroutines with "
            f"asyncio.run() instead. Offending files: {sorted(set(offenders))}"
        )

    def test_hub_imports_in_this_suite_are_guarded(self):
        import re

        offenders = []
        for path in sorted(Path(REPO_ROOT).glob("tests/test_*.py")):
            text = path.read_text(encoding="utf-8")
            if not re.search(r"^\s*(?:from|import)\s+hub[\s.]", text, re.M):
                continue
            if 'importorskip("hub' not in text and "importorskip('hub" not in text:
                offenders.append(path.name)
        assert not offenders, (
            "these import Hub modules without a pytest.importorskip guard, so a "
            f"core install fails collection instead of skipping: {offenders}"
        )


class TestHttpsEnforcedForRemoteHub:
    def test_remote_http_is_refused(self):
        from commontrace import hub_client

        with pytest.raises(hub_client.HubConnectionError, match="plaintext http"):
            hub_client._validate_hub_url("http://external-untrusted-server.com/mcp")

    def test_https_is_unaffected(self):
        from commontrace import hub_client

        hub_client._validate_hub_url("https://hub.example.com/mcp")


class TestSemanticQueryDetectsIndexMismatch:
    def _module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_query_probe", Path(REPO_ROOT) / "commontrace" / "reference" / "query.py",
        )
        module = importlib.util.module_from_spec(spec)
        return module, spec

    def test_dimension_mismatch_is_a_clear_error_not_a_numpy_traceback(self, tmp_path, monkeypatch, capsys):
        pytest.importorskip("numpy", reason="attention extra not installed in this env")
        pytest.importorskip("sentence_transformers", reason="attention extra not installed in this env")
        import numpy as np

        module, spec = self._module()
        spec.loader.exec_module(module)

        index_path = tmp_path / "index.npz"
        np.savez(
            str(index_path),
            slugs=np.array(["lesson_a"]),
            embeddings=np.zeros((1, 384), dtype=np.float32),
            model_name=np.array(module._TRUSTED_MODEL_NAME),
            n_lessons=np.array(1),
        )
        monkeypatch.setattr(module, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(module, "LESSONS_DIR", str(tmp_path))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(768, dtype=np.float32)

        monkeypatch.setattr(module, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", "task description"])

        rc = module.main()

        assert rc == 1
        err = capsys.readouterr().err
        assert "embedding dimension" in err
        assert "build_index.py" in err

    def test_row_count_mismatch_is_detectable_before_indexing(self, tmp_path, monkeypatch, capsys):
        pytest.importorskip("numpy", reason="attention extra not installed in this env")
        pytest.importorskip("sentence_transformers", reason="attention extra not installed in this env")
        import numpy as np

        module, spec = self._module()
        spec.loader.exec_module(module)

        index_path = tmp_path / "index.npz"
        np.savez(
            str(index_path),
            slugs=np.array(["a", "b", "c"]),
            embeddings=np.zeros((2, 384), dtype=np.float32),
            model_name=np.array(module._TRUSTED_MODEL_NAME),
            n_lessons=np.array(3),
        )
        monkeypatch.setattr(module, "INDEX_PATH", str(index_path))
        monkeypatch.setattr(module, "LESSONS_DIR", str(tmp_path))

        class FakeModel:
            def encode(self, *a, **k):
                return np.zeros(384, dtype=np.float32)

        monkeypatch.setattr(module, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", "task description"])

        rc = module.main()

        assert rc == 1
        err = capsys.readouterr().err
        assert "embedding row" in err
        assert "truncated" in err


class TestTemplateHeadingsMatchWhatIsGenerated:
    def test_a_store_with_episodes_indexes_episodes_not_traces(self):
        from commontrace import templates

        idx = templates.index_md("code", has_episodes=True)
        assert "#### Episodes" in idx
        assert "#### Traces" not in idx

    def test_a_store_without_episodes_keeps_traces(self):
        from commontrace import templates

        idx = templates.index_md("sales", has_episodes=False)
        assert "#### Traces" in idx
        assert "#### Episodes" not in idx

    def test_a_code_store_with_no_episode_profile_indexes_traces(self):
        from commontrace import templates

        idx = templates.index_md("code", has_episodes=False)
        assert "#### Traces" in idx
        assert "#### Episodes" not in idx

    def test_usage_convention_matches_the_generated_heading_level(self):
        from commontrace import templates

        idx = templates.index_md("code")
        assert "### <Domain>" in idx
        assert "## <Domain>" not in idx.replace("### <Domain>", "")


class TestBuildIndexTempFileIsUniquePerProcess:
    def test_source_no_longer_hardcodes_the_tmp_path(self):
        text = (Path(REPO_ROOT) / "commontrace" / "reference" / "build_index.py").read_text(encoding="utf-8")
        assert 'INDEX_PATH + ".tmp.npz"' not in text
        assert "tempfile.mkstemp" in text


class TestImportStreamsRatherThanBuffering:
    def _run_import(self, tmp_path, jsonl_lines):
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init", "--agent-type", "code", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        src = tmp_path / "export.jsonl"
        src.write_text("\n".join(jsonl_lines) + "\n", encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "commontrace", "import", str(src),
             "--agent-type", "code", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_a_bulk_import_still_writes_every_valid_row(self, tmp_path):
        import json as _json

        rows = [
            _json.dumps({"title": f"t{i}", "context": "c", "solution": "s"})
            for i in range(50)
        ]
        r = self._run_import(tmp_path, rows)
        assert r.returncode == 0, r.stderr
        assert "50 row(s) parseable" in r.stdout
        written = [
            p for p in (tmp_path / "memory" / "traces").iterdir()
            if p.name.endswith(".md") and p.name != "README.md"
        ]
        assert len(written) == 50

    def test_skip_detail_lines_are_capped_but_the_count_is_not(self, tmp_path):
        rows = ["not valid json"] * 30
        r = self._run_import(tmp_path, rows)
        assert "30 skipped" in r.stdout
        assert r.stderr.count("[SKIP]") == 20
        assert "10 more skipped" in r.stderr

    def test_iter_jsonl_never_holds_more_than_one_row(self):
        import inspect

        from commontrace import import_data

        assert inspect.isgeneratorfunction(import_data.iter_jsonl)
        assert inspect.isgeneratorfunction(import_data.iter_csv)

    def test_list_collecting_wrappers_still_work_for_small_inputs(self):
        from commontrace import import_data

        mapping = import_data.FieldMapping()
        imported, skipped = import_data.parse_jsonl(
            iter(['{"title": "t", "context": "c", "solution": "s"}']), mapping,
        )
        assert len(imported) == 1 and len(skipped) == 0


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits (os.chmod/os.stat st_mode) don't carry the same meaning on Windows",
)
class TestFrontmatterWritePreservesPermissions:
    def test_rewriting_an_existing_file_preserves_its_mode(self, tmp_path):
        from commontrace import frontmatter

        p = tmp_path / "existing.md"
        p.write_text("---\na: 1\n---\n\nbody\n", encoding="utf-8")
        os.chmod(p, 0o644)

        fm, body = frontmatter.read(str(p))
        frontmatter.write(str(p), fm, body)

        assert oct(os.stat(p).st_mode & 0o777) == "0o644"

    def test_rewriting_a_group_writable_file_preserves_that_too(self, tmp_path):
        from commontrace import frontmatter

        p = tmp_path / "shared.md"
        p.write_text("---\na: 1\n---\n\nbody\n", encoding="utf-8")
        os.chmod(p, 0o664)

        fm, body = frontmatter.read(str(p))
        frontmatter.write(str(p), fm, body)

        assert oct(os.stat(p).st_mode & 0o777) == "0o664"

    def test_a_brand_new_file_respects_the_process_umask(self, tmp_path, monkeypatch):
        from commontrace import frontmatter

        old_umask = os.umask(0o022)
        try:
            p = tmp_path / "new.md"
            frontmatter.write(str(p), {"a": 1}, "body\n")
            assert oct(os.stat(p).st_mode & 0o777) == "0o644"
        finally:
            os.umask(old_umask)
