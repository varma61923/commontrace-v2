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

    @pytest.mark.asyncio
    async def test_a_lesson_with_a_hub_trace_id_is_not_recontributed(self, tmp_path, monkeypatch):
        from commontrace import hub_client

        calls = []

        async def _fake_call_tool(hub_url, api_key, tool, args):
            calls.append((tool, args))
            return {"id": "newly-minted-id"}

        monkeypatch.setattr(hub_client, "_call_tool", _fake_call_tool)
        self._lesson(tmp_path, "lesson_already_pushed", "existing-uuid")

        results = await hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path))

        assert calls == [], "contribute_trace must not be called for an already-pushed lesson"
        assert len(results) == 1
        assert results[0].skipped is True
        assert results[0].hub_trace_id == "existing-uuid"

    @pytest.mark.asyncio
    async def test_a_never_pushed_lesson_is_contributed_with_an_idempotency_key(
        self, tmp_path, monkeypatch
    ):
        """The key is what makes a lost response safe to retry -- the Hub
        already supports it (hub/crud.py:contribute_trace); this call site
        was simply not sending one."""
        from commontrace import hub_client

        calls = []

        async def _fake_call_tool(hub_url, api_key, tool, args):
            calls.append((tool, args))
            return {"id": "minted-id"}

        monkeypatch.setattr(hub_client, "_call_tool", _fake_call_tool)
        self._lesson(tmp_path, "lesson_fresh", None)

        results = await hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path))

        assert len(calls) == 1
        tool, args = calls[0]
        assert tool == "contribute_trace"
        assert args["idempotency_key"] == "lesson:lesson_fresh"
        assert results[0].skipped is False
        assert results[0].hub_trace_id == "minted-id"

    @pytest.mark.asyncio
    async def test_pushing_twice_contributes_exactly_once(self, tmp_path, monkeypatch):
        """The end-to-end property: run push, run it again, and the Hub is
        called once in total rather than once per run."""
        from commontrace import hub_client

        calls = []

        async def _fake_call_tool(hub_url, api_key, tool, args):
            calls.append((tool, args))
            return {"id": "minted-id"}

        monkeypatch.setattr(hub_client, "_call_tool", _fake_call_tool)
        self._lesson(tmp_path, "lesson_pushed_twice", None)

        await hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path))
        await hub_client.push_active_lessons("http://h/mcp", "k", str(tmp_path))

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


def trace_io_read(path):
    from commontrace import trace_io

    return trace_io.read(path)
