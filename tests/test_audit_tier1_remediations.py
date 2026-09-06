"""Regression tests for the Tier 1 findings of the 2026-08-21 external audit.

Each test here failed before its fix and was reproduced by hand first, so
what is pinned is the observable behaviour an operator would hit, not the
shape of the patch.

The five: an `install` that wrote through a symlink, a validator that
rejected the repository's own example lesson, an importer that silently
dropped a documented schema field, `capture`/`import` writing traces
non-atomically under a racy filename, and `sync --push` minting a duplicate
Hub trace on every run.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from commontrace import frontmatter, import_data, validate

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- PROTO-01: unquoted YAML dates must stay strings --------------------


class TestYamlDatesLoadAsStrings:
    """`lesson.schema.json` declares `last_hit` as a string, and
    `trace.schema.json` says the same for `created_at`/`review_after`. YAML
    1.1 resolved an unquoted `2026-07-01` to a `datetime.date`, so the
    obvious way to write the file failed this project's own validator."""

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
        """The date fix must not have collaterally broken the bool fix that
        shares the same resolver table."""
        p = tmp_path / "b.md"
        p.write_text("---\na: true\nb: false\nc: NO\nd: on\n---\n\nbody\n", encoding="utf-8")
        fm, _ = frontmatter.read(str(p))
        assert fm["a"] is True and fm["b"] is False
        assert fm["c"] == "NO" and fm["d"] == "on"

    def test_the_shipped_example_lessons_validate(self):
        """The regression that started this: `commontrace lesson validate`
        rejected a file this repository ships as a correctness example."""
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


# --- PROTO-04: the importer must not drop a documented outcome field ----


class TestImportKeepsEveryOutcomeField:
    def test_baseline_survives_import(self):
        """Dropping `baseline` imported historical control rows as active
        traces, so `bench --pilot` compared against a control set that no
        longer existed -- a silently wrong number, not an error."""
        out = import_data._extract_outcome(
            {"title": "T", "resolved": "true", "baseline": "true"}
        )
        assert out.get("baseline") is True

    def test_importer_handles_every_outcome_field_the_schema_declares(self):
        """The real defect was a list that drifted from the schema. Pin the
        parity rather than the one field that happened to be missing."""
        schema = validate.load_schema("trace.schema.json")
        declared = set(schema["properties"]["outcome"]["properties"])
        handled = set(import_data._OUTCOME_BOOL_FIELDS) | set(import_data._OUTCOME_INT_FIELDS)
        assert declared - handled == set(), f"importer silently drops: {sorted(declared - handled)}"


# --- SEC-01: install must not write through a symlink -------------------


class TestInstallDoesNotFollowSymlinks:
    def test_a_symlinked_destination_is_replaced_not_followed(self, tmp_path):
        """`open(path,"w")` and `shutil.copyfile` both follow symlinks, and
        `install` writes to fixed, guessable paths inside someone else's
        workspace. Reproduced before the fix: the link target's contents
        were replaced and the link was left in place, so nothing in the
        output showed what had happened."""
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


# --- SEC-02 / PROTO-02: atomic writes, collision-free names -------------


class TestCaptureWritesAtomicallyAndUniquely:
    def _capture(self, dest, title):
        return subprocess.run(
            [sys.executable, "-m", "commontrace", "capture",
             "--title", title, "--context", "c", "--solution", "s",
             "--agent-type", "code", "--dest", str(dest)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )

    def test_same_titled_captures_do_not_overwrite_each_other(self, tmp_path):
        """The old name was `<date>_<slug>.md` with an `os.path.exists`
        fallback -- check-then-act, so two agents capturing the same title
        in the same second both saw False and one write was lost."""
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
        """Atomic write must not have changed the on-disk format."""
        subprocess.run(
            [sys.executable, "-m", "commontrace", "init",
             "--agent-type", "code", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        r = self._capture(tmp_path, "round trip")
        fm, body = frontmatter.read(r.stdout.strip())
        assert fm["title"] == "round trip"
        assert "## Context" in body and "## Solution" in body


# --- PROTO-03: sync --push must not duplicate an already-pushed lesson --


class TestPushSkipsAlreadyPushedLessons:
    """`contribute_trace` mints a NEW trace every call, so without a
    `hub_trace_id` guard every `sync --push` added one duplicate per active
    lesson per run and overwrote the local id with the newest copy."""

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
        """contribute_trace mints a new trace every call, so it must never
        be reachable for a lesson that already has a hub_trace_id --
        updating one is amend_trace's job. This lesson has no stored
        hub_pushed_fingerprint (as any lesson pushed before that field
        existed would not), so it's treated as possibly-changed and
        amended -- never re-contributed as a duplicate. See
        tests/test_hub_client.py's TestPushPropagatesEdits for the
        fingerprint-matches-so-skip-entirely case this split off from."""
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
        """The key is what makes a lost response safe to retry -- the Hub
        already supports it (hub/crud.py:contribute_trace); this call site
        was simply not sending one."""
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
        """The end-to-end property: run push, run it again, and the Hub is
        called once in total rather than once per run."""
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


# ======================================================================
# Tier 2 findings from the same audit.
# ======================================================================


# --- PROTO-05: Unicode-aware tokenization -------------------------------


class TestTokenizersHandleNonAscii:
    """`[a-z0-9]+` matched ASCII only, so any non-English fleet got zero
    lexical retrieval, zero distill clustering, and no error explaining
    why."""

    def test_cjk_and_cyrillic_produce_tokens(self):
        from commontrace import distill, retrieval

        assert retrieval._tokenize("数据库连接失败") != []
        assert distill._tokenize("Ошибка подключения") != set()

    def test_accented_latin_is_not_mutilated(self):
        """`résumé` tokenized to ['sum'] -- the accents split the word and
        the fragments fell under the len>1 filter."""
        from commontrace import retrieval

        assert retrieval._tokenize("résumé") == ["résumé"]

    def test_ascii_behaviour_is_unchanged(self):
        """The fix must not have altered scoring for existing English
        stores, which is every store that exists today."""
        from commontrace import retrieval

        assert retrieval._tokenize("The database connection failed") == [
            "database", "connection", "failed",
        ]


# --- SEC-04 / PROTO-08: section parsing ---------------------------------


class TestSectionParsing:
    def test_first_occurrence_wins_not_last(self):
        """A dict comprehension kept the LAST match, so a heading quoted
        later in the body -- inside a fenced block the regex cannot see
        into -- replaced the real section."""
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
        """`## How to Apply` is the spelling a human naturally writes."""
        from commontrace import hub_client

        sections = hub_client._lesson_sections("## Rule\nr\n\n## How To Apply\nsteps here\n")
        assert sections.get("how to apply") == "steps here"


# --- MEM-04: benchmark must survive hand-authored values ----------------


class TestBenchmarkSurvivesBadUsesValues:
    def test_safe_int_coerces_without_raising(self):
        from commontrace.reference.measure_performance import _safe_int

        assert _safe_int(1) == 1
        assert _safe_int("2") == 2
        assert _safe_int(None) == 0
        assert _safe_int("nonsense") == 0

    def test_booleans_do_not_count_as_uses(self):
        """bool is an int subclass, so `uses: true` would silently count as
        one use -- a wrong number rather than a caught error."""
        from commontrace.reference.measure_performance import _safe_int

        assert _safe_int(True) == 0

    def test_mixed_types_do_not_crash_the_benchmark(self):
        """One hand-edited lesson used to take the whole run down with
        `'<' not supported between instances of 'int' and 'str'`."""
        from commontrace.reference.measure_performance import compute_extras

        lessons = {"a": {"uses": 3}, "b": {"uses": "2"}, "c": {"uses": None}, "d": {"uses": True}}
        compute_extras([], lessons)  # not raising is the assertion


# --- SEC-05 / PROTO-10: operational errors are messages, not tracebacks --


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
        """PROTO-10: FleetSignature.from_dict indexes required keys directly,
        so a truncated export raised a bare KeyError through the CLI."""
        import json as _json

        sig = tmp_path / "sig.json"
        sig.write_text(_json.dumps({"nope": 1}), encoding="utf-8")
        r = self._run("overlap", "report", "--ours", str(sig), "--theirs", str(sig))
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "fleet_label" in r.stderr

    def test_a_genuine_bug_still_raises(self):
        """The handler is narrow on purpose -- catching everything would
        turn defects into silent exit-1s."""
        import commontrace.cli as cli

        src = __import__("inspect").getsource(cli.main)
        assert "except Exception" not in src
        assert "except BaseException" not in src


# --- PROTO-07: the causal experiment loop must actually close -----------


class TestExperimentLoopCloses:
    """`query --experiment` logs an arm under an occasion id, and
    `experiment` joins that to an outcome via the trace's `id`. `capture`
    had no way to set the id, so nothing ever joined and every assignment
    was reported as having no recorded outcome -- the randomized-holdout
    measurement, which is the strongest claim this product makes, could not
    be run end to end at all.

    trace.schema.json permits this directly: id is any non-empty string,
    "Locally-only traces MAY use a temporary local id"."""

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
        """The property that was broken: an assignment logged under an
        occasion must find that occasion's recorded outcome."""
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
        """The pre-fix behaviour, pinned so a regression is visible as a
        behaviour change rather than as a silently empty report."""
        self._store_with_lesson(tmp_path)
        self._run("query", "paginating a large table", "--experiment",
                  "--occasion-id", "task-200", dest=tmp_path)
        self._run("capture", "--title", "t", "--context", "large table", "--solution", "s",
                  "--agent-type", "code", "--resolved", dest=tmp_path)
        r = self._run("experiment", dest=tmp_path)
        # The diagnostic goes to stderr, so check both streams rather than
        # assuming which one carries it.
        assert "none have a matching" in (r.stdout + r.stderr)

    def test_an_empty_occasion_id_is_refused(self, tmp_path):
        self._store_with_lesson(tmp_path)
        r = self._run("capture", "--title", "t", "--context", "c", "--solution", "s",
                      "--agent-type", "code", "--occasion-id", "   ", dest=tmp_path)
        assert r.returncode == 1
        assert "cannot be empty" in r.stderr

    def test_an_unsafe_occasion_id_cannot_escape_the_traces_directory(self, tmp_path):
        """A ticket system can emit anything. The id inside the file must be
        preserved verbatim so the join still works, while the FILENAME
        fragment derived from it must stay confined."""
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
    """Re-capturing an existing occasion used to replace title/context/
    solution wholesale with whatever was passed on the second call --
    --title/--context/--solution are still required on every `capture`
    invocation, so a second call meant to attach an outcome (--resolved,
    hours after the task actually ran) silently discarded the original
    narrative the moment its text differed even slightly from the first
    call's."""

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
        """The existing merge test above only ever sets ONE outcome field
        across the two calls (the first call sets none at all), so
        "the result has that field" passed whether or not the code
        actually merged anything -- there was nothing to lose. This
        reproduces the real failure: a first call that already recorded
        tokens_used/tags/agent_id, followed by a second call that only
        adds --resolved. Before the fix, tokens_used/tags/agent_id and the
        original created_at were silently wiped by the second call's
        (empty/default) values instead of merged."""
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
        """When the second call DOES pass --tags/--agent-id, those values
        win outright (they are not appended to/merged with the old ones --
        only a call that OMITS them falls back to preserving the original)."""
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


# --- SEC-06 / PROTO-06: the holdout log is the experiment's evidence ----


class TestHoldoutLogIntegrity:
    """The log is the raw evidence for the causal number. A lost line is not
    a smaller sample -- it removes one arm's data point from a randomized
    comparison, biasing the effect size, invisibly."""

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
        """Eight processes appending at once. Every line must still parse --
        an unlocked buffered append can flush mid-line under exactly this
        load, which is a fleet retrieving concurrently, i.e. normal use."""
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
            _json.loads(ln)  # a raise here is the test failing

    def test_a_corrupt_line_is_reported_not_silently_dropped(self, tmp_path):
        """Previously `except json.JSONDecodeError: continue` discarded it
        with no trace, so a biased number looked like a clean one."""
        self._store(tmp_path)
        subprocess.run(
            [sys.executable, "-m", "commontrace", "query", "paginating a large table",
             "--experiment", "--occasion-id", "occ-1", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        log = tmp_path / "memory" / "holdout_log.jsonl"
        with open(log, "a", encoding="utf-8") as fh:
            fh.write('{"occasion_id": "occ-2", "lesso\n')  # truncated, as a torn flush would be

        r = subprocess.run(
            [sys.executable, "-m", "commontrace", "experiment", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert "unparseable line" in r.stderr
        assert "unreliable" in r.stderr

    def test_a_clean_log_produces_no_warning(self, tmp_path):
        """The warning must mean something when it appears."""
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


# ======================================================================
# Tier 3 findings that touch the pilot path.
# ======================================================================


class TestDoctorReportsFailuresInItsExitCode:
    """`doctor` returned 0 unconditionally, so a CI gate or container health
    check could not act on a missing store, no lessons, or an unsupported
    Python -- it looked like a pass to everything except a human reading
    the output."""

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
        """_info marks things normal for a clean client install. If those
        counted as failures, every `pip install` user's pipeline would go
        red on a working setup."""
        r = self._run()
        assert r.returncode == 0
        assert "[INFO]" in r.stdout

    def test_a_fresh_store_with_no_lessons_is_not_a_failure(self, tmp_path):
        """Only CRITICAL checks affect the exit code. A store you just
        created legitimately has zero lessons -- exiting non-zero for that
        would make day one of every install look broken, which is how a
        health check gets ignored and then stops being read at all. The
        pre-existing tests/test_doctor.py caught this when the first
        version of the fix counted every [WARN]."""
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
        """Draft 2020-12 allows `items` to be an array. This validator only
        implements the single-subschema form, and the gate that exists to
        say so let it through -- so it surfaced later as
        `AttributeError: 'list' object has no attribute 'get'`."""
        from commontrace import validate

        schema = {"type": "object",
                  "properties": {"tags": {"type": "array", "items": [{"type": "string"}]}}}
        with pytest.raises(validate.UnsupportedSchemaError):
            validate.assert_supported_schema(schema)

    def test_the_supported_form_still_passes(self):
        from commontrace import validate

        schema = {"type": "object",
                  "properties": {"tags": {"type": "array", "items": {"type": "string"}}}}
        validate.assert_supported_schema(schema)  # not raising is the assertion

    def test_the_shipped_schemas_still_load(self):
        from commontrace import validate

        for name in ("trace.schema.json", "lesson.schema.json"):
            validate.assert_supported_schema(validate.load_schema(name))


class TestReportKeepsPlaceholderRows:
    def test_a_dash_only_data_row_is_not_mistaken_for_a_separator(self):
        """`| - | - |` is a not-recorded placeholder, and it was being
        dropped from the HTML report a customer reads."""
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
    """--threshold-lexical/-freshness/-composite were accepted, forwarded,
    and read by nothing: a fleet could set a quality gate, watch it never
    fire, and conclude quality was fine. bench_cmd.py warned about that in
    so many words, with the note "implement or delete them deliberately".
    They are now implemented, so what is pinned is the effect, not the
    apology for its absence."""

    def test_no_threshold_flag_reports_itself_as_unimplemented(self):
        for flag in ("--threshold-lexical=0.99", "--threshold-freshness=0.5",
                     "--threshold-composite=0.7", "--threshold-semantic=0.99"):
            r = subprocess.run(
                [sys.executable, "-m", "commontrace", "bench", flag],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            assert "not implemented" not in r.stderr, flag

    def test_lexical_duplicates_are_actually_detected(self):
        """Two lessons saying the same thing split the retrieval signal
        between them; this is the check that finds them, and it needs no
        optional dependency (unlike --threshold-semantic)."""
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
        """Distinct from the never-hit ratio: a corpus can have every lesson
        hit at some point and still be entirely stale."""
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
        """A single number whose inputs are unstated is exactly the kind of
        metric that gets quoted and then cannot be defended."""
        from commontrace.reference import measure_performance as mp

        composite = mp.compute_composite({
            "lesson_quality": {"value": 0.8},
            "implicit_retrieval": {"strict": 0.6},
            "n_lessons": 10,
            "extras": {"never_hit": ["x", "y"]},   # coverage 0.8
            "freshness": {"value": 1.0},
        })
        assert composite["value"] == pytest.approx((0.8 + 0.6 + 0.8 + 1.0) / 4)
        assert set(composite["components"]) == {
            "lesson_quality", "implicit_retrieval", "lesson_coverage", "freshness"
        }

    def test_a_threshold_that_is_not_passed_raises_no_alert(self):
        """All three are opt-in: an existing run's alert list is unchanged."""
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
        """measure_performance.py and memory/attention/*.py all read
        JUSTDOIT_ROOT as a documented legacy fallback. paths.resolve_root
        did not, so a legacy-configured store had the CLI and the scripts
        operating on different directories."""
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


# --- The pilot runbook must not drift from the code ---------------------


class TestPilotRunbookMatchesTheCode:
    """PILOT.md publishes sample sizes an operator plans a pilot around. If
    the power calculation changes and the table does not, someone runs an
    underpowered pilot and reads UNDERPOWERED as 'no effect'."""

    def _runbook(self) -> str:
        return (Path(REPO_ROOT) / "PILOT.md").read_text(encoding="utf-8")

    def test_the_runbook_exists_and_is_linked_from_the_readme(self):
        assert (Path(REPO_ROOT) / "PILOT.md").exists()
        readme = (Path(REPO_ROOT) / "README.md").read_text(encoding="utf-8")
        assert "PILOT.md" in readme

    def test_the_published_sample_sizes_match_the_shipped_calculation(self):
        import re

        from commontrace.experiment import minimum_detectable_effect

        # Parse the PUBLISHED table out of the runbook and compare each cell
        # against the live calculation. Asserting against hardcoded expected
        # values would pass even if someone edited PILOT.md, which is the
        # drift this test exists to catch.
        text = self._runbook()
        columns = [25, 50, 100, 200, 400, 800]
        rows = re.findall(r"^\| (\d+)% \|(.+)\|\s*$", text, re.M)
        assert len(rows) >= 3, f"could not parse the sample-size table, got {rows}"

        for pct, rest in rows:
            baseline = int(pct) / 100
            cells = [c.strip() for c in rest.split("|") if c.strip()]
            assert len(cells) == len(columns), f"row {pct}% has {len(cells)} cells"
            for n, cell in zip(columns, cells):
                published = int(cell.replace("pp", ""))
                got = minimum_detectable_effect(n, baseline)
                assert got is not None, f"no MDE for baseline={baseline} n={n}"
                assert round(got * 100) == published, (
                    f"PILOT.md publishes {published}pp for baseline={pct}% n={n}, "
                    f"but the code computes {got * 100:.0f}pp -- update the table"
                )

    def test_the_runbook_documents_the_join_step(self):
        """The step that was impossible before `capture --occasion-id`, and
        the single most likely thing to get wrong."""
        text = self._runbook()
        assert "--occasion-id" in text
        assert "capture" in text

    def test_the_runbook_states_the_cost_and_the_limits(self):
        """A runbook that omits what the experiment costs, or overstates
        what it proves, is how a bounded trade becomes a surprise."""
        text = self._runbook()
        assert "holdout-rate 0" in text, "the opt-out must be documented"
        assert "UNDERPOWERED" in text
        assert "says nothing about" in text, "the limits of the result must be stated"


class TestCoreSuiteNeedsNoHubDependencies:
    """`tests/` must run on a CORE install: `pip install commontrace` with no
    extras, which is what three of CI's six core jobs do.

    This has now bitten twice. The CHANGELOG records an unconditional
    `import numpy` breaking collection the same way, and this file added
    `@pytest.mark.asyncio` tests that pass locally -- because this sandbox
    has the Hub's dependencies installed -- and fail in CI with "async def
    functions are not natively supported". Local green is not evidence here;
    the absence of the dependency is the thing under test.
    """

    def test_no_test_in_this_suite_requires_pytest_asyncio(self):
        """pytest-asyncio is a Hub test dependency, not a core one. An async
        test here is silently skipped-or-failed depending on the runner,
        which is worse than not having the test."""
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
        """A core install has no SQLAlchemy, asyncpg or alembic, so an
        unguarded `from hub import ...` breaks collection for the whole
        file. Importing Hub code from tests/ is legitimate -- one test
        checks that the client and the Hub sign identically, which is only
        meaningful if both are present -- but it must be behind
        `pytest.importorskip`, so a core install SKIPS rather than fails.
        That is the pattern tests/test_commons_cmd.py already uses."""
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


# ======================================================================
# The six findings I initially reported as "cosmetic Tier 3" and skipped.
# Two of them (SEC-03, MEM-03) were not cosmetic; verified and fixed here.
# ======================================================================


class TestHttpsEnforcedForRemoteHub:
    """Covered more thoroughly in tests/test_hub_client.py -- this is the
    end-to-end shape: a fleet that points COMMONTRACE_HUB_URL at a real
    remote host over plain http gets a clear refusal, not a silent
    plaintext Bearer-token send."""

    def test_remote_http_is_refused(self):
        from commontrace import hub_client

        with pytest.raises(hub_client.HubConnectionError, match="plaintext http"):
            hub_client._validate_hub_url("http://external-untrusted-server.com/mcp")

    def test_https_is_unaffected(self):
        from commontrace import hub_client

        hub_client._validate_hub_url("https://hub.example.com/mcp")  # must not raise


class TestSemanticQueryDetectsIndexMismatch:
    """query.py's `embeddings @ q_emb` previously ran unguarded, so an index
    built by a different model (different embedding width) crashed with
    numpy's own matmul error, from deep inside a matrix multiply, with no
    mention of build_index.py or how to fix it."""

    def _module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_query_probe", Path(REPO_ROOT) / "commontrace" / "reference" / "query.py",
        )
        module = importlib.util.module_from_spec(spec)
        return module, spec

    def test_dimension_mismatch_is_a_clear_error_not_a_numpy_traceback(self, tmp_path, monkeypatch, capsys):
        """Previously only asserted the literal `384 != 768` -- true no
        matter what query.py itself does with a mismatched index, so this
        passed identically before query.py's guard existed and after.
        Rewritten to actually run query.py's main() against a mismatched
        index and check ITS behavior: a clean exit code 1 and an
        actionable stderr message, not a raw numpy matmul traceback."""
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
                return np.zeros(768, dtype=np.float32)  # a different model's width

        monkeypatch.setattr(module, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", "task description"])

        rc = module.main()

        assert rc == 1
        err = capsys.readouterr().err
        assert "embedding dimension" in err
        assert "build_index.py" in err

    def test_row_count_mismatch_is_detectable_before_indexing(self, tmp_path, monkeypatch, capsys):
        """Previously only asserted the literal `2 != 3`, exercising
        nothing in query.py itself. Rewritten to run main() against a
        truncated index (fewer embedding rows than slugs) and check it
        reports a clean, actionable error instead of an IndexError out of
        `slugs[idx]`."""
        pytest.importorskip("numpy", reason="attention extra not installed in this env")
        pytest.importorskip("sentence_transformers", reason="attention extra not installed in this env")
        import numpy as np

        module, spec = self._module()
        spec.loader.exec_module(module)

        index_path = tmp_path / "index.npz"
        # 3 slugs on record, but only 2 embedding rows -- a truncated write.
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
                return np.zeros(384, dtype=np.float32)  # matches the index's own width

        monkeypatch.setattr(module, "SentenceTransformer", lambda name: FakeModel())
        monkeypatch.setattr(sys, "argv", ["query.py", "task description"])

        rc = module.main()

        assert rc == 1
        err = capsys.readouterr().err
        assert "embedding row" in err
        assert "truncated" in err


class TestTemplateHeadingsMatchWhatIsGenerated:
    def test_a_store_with_episodes_indexes_episodes_not_traces(self):
        """A store that scaffolds memory/episodes/ (the code-review profile,
        init_cmd.EPISODE_PROFILES) previously generated an index pointing at
        the wrong directory.

        Keyed on whether the store HAS episodes rather than on
        `agent_type == "code"`: episodes come from the profile, and any fleet
        may run that profile -- while a `code` fleet that doesn't run it has
        no episodes to index.
        """
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
        """`--agent-type code --profile ''` captures into memory/traces/, so
        that is what its own index must point at."""
        from commontrace import templates

        idx = templates.index_md("code", has_episodes=False)
        assert "#### Traces" in idx
        assert "#### Episodes" not in idx

    def test_usage_convention_matches_the_generated_heading_level(self):
        """The instructions said to add `## <Domain>`; the actual generated
        sections are `### <Domain>`. Following the written instruction
        produced a section one level above every real one."""
        from commontrace import templates

        idx = templates.index_md("code")
        assert "### <Domain>" in idx
        assert "## <Domain>" not in idx.replace("### <Domain>", "")


class TestBuildIndexTempFileIsUniquePerProcess:
    """Two processes rebuilding the index concurrently previously wrote to
    the exact same hardcoded `.tmp.npz` path."""

    def test_source_no_longer_hardcodes_the_tmp_path(self):
        text = (Path(REPO_ROOT) / "commontrace" / "reference" / "build_index.py").read_text(encoding="utf-8")
        assert 'INDEX_PATH + ".tmp.npz"' not in text
        assert "tempfile.mkstemp" in text


class TestImportStreamsRatherThanBuffering:
    """SEC-08: parse_jsonl/parse_csv collected every row into memory before
    writing a single trace file. import_cmd now consumes iter_jsonl/
    iter_csv, a single pass, with only counts (not full row content)
    growing unboundedly."""

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
        """Beyond the first 20, only a running count is kept -- the fix for
        the OTHER half of the memory issue: an export with many bad rows
        accumulating one detail string per row indefinitely."""
        rows = ["not valid json"] * 30
        r = self._run_import(tmp_path, rows)
        assert "30 skipped" in r.stdout
        assert r.stderr.count("[SKIP]") == 20
        assert "10 more skipped" in r.stderr

    def test_iter_jsonl_never_holds_more_than_one_row(self):
        """The actual streaming property, not just the CLI's summary
        output: iter_jsonl is a generator, so nothing forces the whole
        file into memory before the first row is available."""
        import inspect

        from commontrace import import_data

        assert inspect.isgeneratorfunction(import_data.iter_jsonl)
        assert inspect.isgeneratorfunction(import_data.iter_csv)

    def test_list_collecting_wrappers_still_work_for_small_inputs(self):
        """parse_jsonl/parse_csv stay available and behave exactly as
        before -- this repo's own tests (test_import.py) call them
        directly, and they are a reasonable convenience for small inputs."""
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
    """SEC-09: NamedTemporaryFile creates its file at 0600 on POSIX
    regardless of the process umask, and os.replace carries that mode
    straight through -- so every rewrite of an existing lesson or trace
    silently tightened its permissions to owner-only, locking a team member
    or CI checkout out of a file they could read a moment ago."""

    def test_rewriting_an_existing_file_preserves_its_mode(self, tmp_path):
        from commontrace import frontmatter

        p = tmp_path / "existing.md"
        p.write_text("---\na: 1\n---\n\nbody\n", encoding="utf-8")
        os.chmod(p, 0o644)

        fm, body = frontmatter.read(str(p))
        frontmatter.write(str(p), fm, body)

        assert oct(os.stat(p).st_mode & 0o777) == "0o644"

    def test_rewriting_a_group_writable_file_preserves_that_too(self, tmp_path):
        """Not just "preserves 644" -- preserves whatever it was, including
        a shared-repo mode more permissive than NamedTemporaryFile's 0600."""
        from commontrace import frontmatter

        p = tmp_path / "shared.md"
        p.write_text("---\na: 1\n---\n\nbody\n", encoding="utf-8")
        os.chmod(p, 0o664)

        fm, body = frontmatter.read(str(p))
        frontmatter.write(str(p), fm, body)

        assert oct(os.stat(p).st_mode & 0o777) == "0o664"

    def test_a_brand_new_file_respects_the_process_umask(self, tmp_path, monkeypatch):
        """A file that does not exist yet is not "restoring" anything --
        it should behave like a plain `open(path, "w")` would under the
        current umask, same as before this fix."""
        from commontrace import frontmatter

        old_umask = os.umask(0o022)
        try:
            p = tmp_path / "new.md"
            frontmatter.write(str(p), {"a": 1}, "body\n")
            assert oct(os.stat(p).st_mode & 0o777) == "0o644"
        finally:
            os.umask(old_umask)
