"""Per-lesson TTL expiry + fact forget/undo (T2)."""
from __future__ import annotations

import json
import os

import pytest

from commontrace import frontmatter, hierarchical, lesson_cache, paths, ttl
from commontrace.cli import main


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_lesson(root, slug, description="force-push shared branch guidance",
                  expires=None, status="active"):
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    path = os.path.join(ldir, f"{slug}.md")
    fm = {
        "name": slug, "description": description,
        "applies_when": "force-pushing shared history",
        "tags": ["git", "safety"], "domain": "other", "importance": 3,
        "uses": 0, "status": status, "agent_type": "code",
        "last_hit": "2026-07-01",
    }
    if expires is not None:
        fm["expires"] = expires
    frontmatter.write(path, fm, "## Rule\nSomething.\n")
    return path


@pytest.fixture
def store(tmp_path, capsys):
    root = str(tmp_path / "fleet")
    assert main(["init", "--agent-type", "code", "--dest", root]) == 0
    capsys.readouterr()
    return root


def _result_slugs(out):
    return [ln for ln in out.splitlines() if "rel=" in ln]


# ---------------------------------------------------------------------------
# lesson `expires`: frontmatter validation + ttl helpers
# ---------------------------------------------------------------------------

class TestExpiresValidation:
    def test_validate_expires_accepts_date_and_iso(self):
        assert frontmatter.validate_expires("2026-12-31").startswith("2026-12-31")
        assert "2026-12-31" in frontmatter.validate_expires("2026-12-31T12:00:00Z")

    def test_validate_expires_rejects_garbage(self):
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.validate_expires("not-a-date")
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.validate_expires("")

    def test_parse_expiry_boundary_instant(self):
        at = ttl.parse_expiry("2026-04-30T12:00:00Z")
        assert ttl.lesson_is_expired({"expires": "2026-04-30T12:00:00Z"}, at) is True

    def test_lesson_without_expires_never_expires(self):
        assert ttl.lesson_is_expired({}, "2100-01-01") is False
        assert ttl.lesson_is_expired({"expires": ""}, "2100-01-01") is False

    def test_count_partition_summarize_agree(self):
        lessons = [
            ("a", {"expires": "2000-01-01"}),
            ("b", {"expires": "2100-01-01"}),
            ("c", {}),
        ]
        assert ttl.count_expired(lessons) == 1
        live, expired = ttl.partition_expired(lessons)
        assert [p for p, _ in live] == ["b", "c"]
        assert [p for p, _ in expired] == ["a"]
        summary = ttl.summarize(lessons, [])
        assert summary["expired_lessons"] == 1
        assert summary["total_lessons"] == 3
        assert "hidden by TTL" in ttl.format_notice(1)


# ---------------------------------------------------------------------------
# filter_eligible: hide-by-default + --show-expired semantics
# ---------------------------------------------------------------------------

class TestFilterEligibleExpiry:
    def test_expiry_boundary_exact_instant(self):
        lessons = [("p", {"expires": "2026-04-30T12:00:00Z"})]
        # valid_from <= as_of < expires: one second before is still eligible
        assert len(lesson_cache.filter_eligible(
            lessons, as_of="2026-04-30T11:59:59Z")) == 1
        # moment >= expires: expired (exclusive bound, like valid_until)
        assert lesson_cache.filter_eligible(
            lessons, as_of="2026-04-30T12:00:00Z") == []
        # ... unless explicitly requested
        assert len(lesson_cache.filter_eligible(
            lessons, as_of="2026-04-30T12:00:00Z",
            show_expired=True)) == 1

    def test_expired_hidden_by_default_against_now(self):
        lessons = [
            ("old", {"expires": "2000-01-01"}),
            ("new", {"expires": "2100-01-01"}),
            ("plain", {}),
        ]
        assert [p for p, _ in lesson_cache.filter_eligible(lessons)] == ["new", "plain"]
        assert len(lesson_cache.filter_eligible(lessons, show_expired=True)) == 3

    def test_invalid_expires_is_fail_closed_but_recoverable(self):
        lessons = [("p", {"expires": "not-a-date"})]
        assert lesson_cache.filter_eligible(lessons) == []
        assert len(lesson_cache.filter_eligible(lessons, show_expired=True)) == 1


# ---------------------------------------------------------------------------
# query CLI: hide-by-default, --show-expired passthrough, notice
# ---------------------------------------------------------------------------

class TestQueryShowExpired:
    def test_expired_hidden_with_notice_by_default(self, store, capsys):
        _write_lesson(store, "lesson_live_ttl",
                      description="Never force-push a shared branch")
        _write_lesson(store, "lesson_old_ttl",
                      description="Never force-push a shared branch, legacy wording",
                      expires="2000-01-01")
        capsys.readouterr()
        rc = main(["query", "force-push a shared branch", "--lexical",
                   "--dest", store])
        assert rc == 0
        out = capsys.readouterr().out
        slugs = _result_slugs(out)
        assert any("lesson_live_ttl" in ln for ln in slugs)
        assert not any("lesson_old_ttl" in ln for ln in slugs)
        assert "hidden by TTL" in out
        assert "--show-expired" in out

    def test_show_expired_includes_everything_without_notice(self, store, capsys):
        _write_lesson(store, "lesson_live_ttl",
                      description="Never force-push a shared branch")
        _write_lesson(store, "lesson_old_ttl",
                      description="Never force-push a shared branch, legacy wording",
                      expires="2000-01-01")
        capsys.readouterr()
        rc = main(["query", "force-push a shared branch", "--lexical",
                   "--show-expired", "--dest", store])
        assert rc == 0
        out = capsys.readouterr().out
        slugs = _result_slugs(out)
        assert any("lesson_live_ttl" in ln for ln in slugs)
        assert any("lesson_old_ttl" in ln for ln in slugs)
        assert "hidden by TTL" not in out

    def test_no_notice_when_nothing_expired(self, store, capsys):
        _write_lesson(store, "lesson_live_ttl",
                      description="Never force-push a shared branch")
        capsys.readouterr()
        rc = main(["query", "force-push a shared branch", "--lexical",
                   "--dest", store])
        assert rc == 0
        assert "hidden by TTL" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# facts: expires_at + forgotten
# ---------------------------------------------------------------------------

class TestFactExpiry:
    def test_expires_at_stored_and_validated(self, store):
        fact, action = hierarchical.add_fact(
            store, "Cache TTL for sessions is 300 seconds.",
            expires_at="2030-01-01",
        )
        assert action == "ADD"
        assert fact.expires_at is not None and "2030" in fact.expires_at
        assert fact.forgotten is False
        with pytest.raises(ValueError):
            hierarchical.add_fact(store, "Bad TTL fact.", expires_at="not-a-date")

    def test_fact_is_expired_boundary(self, store):
        fact, _ = hierarchical.add_fact(
            store, "Staging flag flips on 2026-05-01.",
            valid_from="2026-01-01T00:00:00Z",
            expires_at="2026-05-01T00:00:00Z",
        )
        assert ttl.fact_is_expired(fact, "2026-04-30T23:59:59Z") is False
        assert ttl.fact_is_expired(fact, "2026-05-01T00:00:00Z") is True
        assert ttl.fact_is_expired({"expires_at": None}) is False

    def test_update_fact_expires_at_set_and_clear(self, store):
        fact, _ = hierarchical.add_fact(store, "Mutable TTL fact.")
        updated = hierarchical.update_fact(
            store, fact.id, expires_at="2031-06-01")
        assert updated.expires_at is not None and "2031" in updated.expires_at
        cleared = hierarchical.update_fact(store, fact.id, expires_at=None)
        assert cleared.expires_at is None
        with pytest.raises(ValueError):
            hierarchical.update_fact(store, fact.id, expires_at="garbage")


class TestForgetUndo:
    def test_forget_undo_round_trip(self, store):
        fact, _ = hierarchical.add_fact(store, "Ephemeral debug flag is on.")
        rev = fact.revision
        forgotten = hierarchical.forget_fact(store, fact.id)
        assert forgotten.forgotten is True
        assert forgotten.revision != rev
        # hidden from default listings, all statuses, even point-in-time
        assert hierarchical.list_facts(store) == []
        assert hierarchical.list_facts(store, as_of="2026-02-01T00:00:00Z",
                                       status="") == []
        shown = hierarchical.list_facts(store, include_forgotten=True)
        assert [f.id for f in shown] == [fact.id]
        # search hides forgotten too
        assert hierarchical.search_facts(store, "debug flag") == []
        # undo restores
        restored = hierarchical.forget_fact(store, fact.id, undo=True)
        assert restored.forgotten is False
        assert [f.id for f in hierarchical.list_facts(store)] == [fact.id]

    def test_forget_missing_fact_raises(self, store):
        with pytest.raises(KeyError):
            hierarchical.forget_fact(store, "fact-does-not-exist")

    def test_forget_preserves_status(self, store):
        fact, _ = hierarchical.add_fact(store, "Status stays active.")
        hierarchical.forget_fact(store, fact.id)
        loaded = hierarchical.load_facts(store)[fact.id]
        assert loaded.status == "active"
        assert loaded.forgotten is True

    def test_fact_cli_forget_undo_and_marker(self, store, capsys):
        assert main(["fact", "add", "Ephemeral CLI flag is set",
                     "--dest", store]) == 0
        capsys.readouterr()
        assert main(["fact", "list", "--dest", store]) == 0
        out = capsys.readouterr().out
        assert "Ephemeral CLI flag" in out
        fid = hierarchical.list_facts(store)[0].id

        assert main(["fact", "forget", fid, "--dest", store]) == 0
        out = capsys.readouterr().out
        assert "Forgot fact" in out

        assert main(["fact", "list", "--dest", store]) == 0
        assert "Ephemeral CLI flag" not in capsys.readouterr().out

        assert main(["fact", "list", "--include-forgotten",
                     "--dest", store]) == 0
        out = capsys.readouterr().out
        assert "Ephemeral CLI flag" in out
        assert "[forgotten]" in out

        assert main(["fact", "forget", fid, "--undo", "--dest", store]) == 0
        assert "Restored fact" in capsys.readouterr().out
        assert main(["fact", "list", "--dest", store]) == 0
        assert "Ephemeral CLI flag" in capsys.readouterr().out

    def test_fact_cli_forget_missing_is_error(self, store, capsys):
        rc = main(["fact", "forget", "fact-nope", "--dest", store])
        assert rc == 1
        assert "not found" in capsys.readouterr().err


class TestFactBackCompat:
    def test_old_jsonl_without_new_fields_loads_with_defaults(self, store):
        legacy = {
            "id": "fact-legacy0001",
            "statement": "Legacy row predates TTL fields.",
            "category": "general",
            "scopes": [],
            "confidence": 0.7,
            "confirmations": 1,
            "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_until": None,
            "source_traces": [],
            "status": "active",
            "superseded_by": None,
            "revision": "r" * 16,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        fpath = os.path.join(paths.memory_dir(store), "facts", "facts.jsonl")
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({**legacy, "some_future_key": "ignored"}) + "\n")
        facts = hierarchical.load_facts(store)
        assert facts["fact-legacy0001"].expires_at is None
        assert facts["fact-legacy0001"].forgotten is False
        # ... and it still lists like any other active fact
        assert any(f.id == "fact-legacy0001"
                   for f in hierarchical.list_facts(store))

    def test_new_row_round_trips_through_jsonl(self, store):
        fact, _ = hierarchical.add_fact(
            store, "Round-trip TTL fact.", expires_at="2030-05-05")
        hierarchical.forget_fact(store, fact.id)
        reloaded = hierarchical.load_facts(store)[fact.id]
        assert reloaded.expires_at == fact.expires_at
        assert reloaded.forgotten is True
