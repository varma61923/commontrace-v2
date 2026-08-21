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
