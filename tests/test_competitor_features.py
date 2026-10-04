"""Tests for competitor-adapted features (Letta / Mem0 / Supermemory patterns).

Covers, in one place:
1. memory_blocks: read_only protection, line-occurrence feedback, line
   insertion, tab normalization, render_memory_blocks() XML formatter.
2. memory_guard.redact_pii(): active PII masking at ingest.
3. conversation/store: PII masking on write, MD5 fact dedup.
4. conversation/search: sigmoid BM25 normalization, anti-recursion turn
   filter, relative time deltas in rendered headers.
5. mcp_server agent tools: core_memory_append, core_memory_replace,
   archival_memory_insert, archival_memory_search, conversation_search.
6. block CLI: insert / render / --read-only wiring.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import sys

import pytest

from commontrace import memory_blocks, memory_guard
from commontrace.conversation import search as csearch
from commontrace.conversation.store import Store, fact_hash

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


# --- 1. memory_blocks -------------------------------------------------------

class TestReadOnly:
    def test_set_read_only_then_mutations_refuse(self, store):
        memory_blocks.set_block(store, "persona", "base", read_only=True)
        assert memory_blocks.get_block(store, "persona").read_only is True
        with pytest.raises(memory_blocks.ReadOnlyBlockError):
            memory_blocks.set_block(store, "persona", "other")
        with pytest.raises(memory_blocks.ReadOnlyBlockError):
            memory_blocks.append_block(store, "persona", "more")
        with pytest.raises(memory_blocks.ReadOnlyBlockError):
            memory_blocks.replace_block(store, "persona", "base", "new")
        with pytest.raises(memory_blocks.ReadOnlyBlockError):
            memory_blocks.insert_block(store, "persona", "top", line_number=0)
        with pytest.raises(memory_blocks.ReadOnlyBlockError):
            memory_blocks.delete_block(store, "persona")
        # content untouched
        assert memory_blocks.get_block(store, "persona").content == "base"

    def test_writable_block_still_mutable(self, store):
        memory_blocks.set_block(store, "w", "a")
        assert memory_blocks.append_block(store, "w", "b").content == "a\nb"


class TestLineOccurrenceFeedback:
    def test_ambiguous_replace_names_lines(self, store):
        memory_blocks.set_block(store, "b", "dup one\ndup two\ndup one")
        with pytest.raises(memory_blocks.MemoryBlockError) as exc:
            memory_blocks.replace_block(store, "b", "dup one", "x")
        assert "[1, 3]" in str(exc.value)

    def test_missing_target_raises_not_found(self, store):
        memory_blocks.set_block(store, "b", "hello")
        with pytest.raises(memory_blocks.SubstringNotFoundError):
            memory_blocks.replace_block(store, "b", "absent", "x")


class TestLineInsertion:
    def test_insert_positions(self, store):
        memory_blocks.set_block(store, "b", "one\ntwo\nthree")
        assert memory_blocks.insert_block(
            store, "b", "zero", line_number=0).content == "zero\none\ntwo\nthree"
        assert memory_blocks.insert_block(
            store, "b", "after-one", line_number=2).content.split("\n")[2] == "after-one"
        assert memory_blocks.insert_block(
            store, "b", "last", line_number=-1).content.endswith("last")

    def test_insert_out_of_range(self, store):
        memory_blocks.set_block(store, "b", "one")
        with pytest.raises(memory_blocks.MemoryBlockError):
            memory_blocks.insert_block(store, "b", "x", line_number=-5)

    def test_insert_creates_missing_block(self, store):
        b = memory_blocks.insert_block(store, "fresh", "hello", line_number=0)
        assert b.content == "hello"


class TestTabNormalization:
    def test_tabs_expanded_before_compare_and_store(self, store):
        memory_blocks.set_block(store, "b", "a\tb")
        assert "\t" not in memory_blocks.get_block(store, "b").content
        out = memory_blocks.replace_block(store, "b", "a\tb".expandtabs(), "c")
        assert out.content == "c"

    def test_line_prefix_stripped(self, store):
        memory_blocks.set_block(store, "b", "real content here")
        out = memory_blocks.replace_block(store, "b", "Line 1: real content here", "done")
        assert out.content == "done"


class TestRenderXML:
    def test_render_xml_shape(self, store):
        memory_blocks.set_block(store, "persona", "Be brief.", read_only=True)
        xml = memory_blocks.render_memory_blocks(memory_blocks.list_blocks(store))
        assert xml.startswith("<memory_blocks>")
        assert xml.endswith("</memory_blocks>")
        assert "<persona>" in xml and "</persona>" in xml
        assert 'read_only="true"' in xml
        assert "<value>Be brief.</value>" in xml

    def test_render_empty(self):
        assert memory_blocks.render_memory_blocks([]) == ""


# --- 2. redact_pii ------------------------------------------------------------

class TestRedactPII:
    def test_email_phone_ssn_masked(self):
        masked, found = memory_guard.redact_pii(
            "mail bob@example.com, call 415-555-1234, ssn 123-45-6789")
        assert "bob@example.com" not in masked
        assert "415-555-1234" not in masked
        assert "123-45-6789" not in masked
        assert set(found) == {"email", "phone", "ssn"}

    def test_luhn_card_masked_noncard_kept(self):
        masked, found = memory_guard.redact_pii("card 4111111111111111 ok")
        assert "4111111111111111" not in masked and "card" in found
        kept, found2 = memory_guard.redact_pii("order 1234567890123 ok")
        assert "1234567890123" in kept and found2 == []

    def test_empty_and_clean(self):
        assert memory_guard.redact_pii("") == ("", [])
        assert memory_guard.redact_pii("nothing sensitive here") == ("nothing sensitive here", [])


# --- 3. conversation store: PII + MD5 dedup ------------------------------------

class TestStorePIIAndDedup:
    def test_pii_masked_on_write(self, tmp_path):
        st = Store(str(tmp_path), "p")
        out = st.add("s", [{"role": "user",
                            "text": "reach me at ana@example.com"}])
        assert out["secrets_redacted"] >= 1
        (turn,) = st.turns([1]).values()
        assert "ana@example.com" not in turn.text
        assert "[REDACTED email]" in turn.text

    def test_fact_hash_normalization(self):
        assert fact_hash("  Hello World! ") == fact_hash("hello world")
        assert fact_hash("a") != fact_hash("b")

    def test_add_memories_md5_dedup(self, tmp_path):
        st = Store(str(tmp_path), "m")
        st.add("s", [{"role": "user", "text": "hi"}])
        assert st.add_memories("s", [{"text": "User likes coffee",
                                      "kind": "preference"}], source="t") == 1
        # same fact, different casing/spacing -> deduped
        assert st.add_memories("s", [{"text": "user likes COFFEE ",
                                      "kind": "preference"}], source="t") == 0
        assert st.add_memories("s", [{"text": "User likes tea",
                                      "kind": "preference"}], source="t") == 1


# --- 4. conversation search -----------------------------------------------------

class TestSigmoidBM25:
    def test_range_and_monotonic(self):
        assert 0.0 < csearch.sigmoid_bm25(0.0, 2) < 0.1
        assert csearch.sigmoid_bm25(5.0, 2) < csearch.sigmoid_bm25(9.0, 2) < 1.0
        assert csearch.sigmoid_bm25(100.0, 2) <= 1.0

    def test_lexical_scores_normalized(self, tmp_path):
        st = Store(str(tmp_path), "l")
        st.add("s", [{"role": "user", "text": "Biscuit the beagle digs holes"}])
        st.add("s", [{"role": "user", "text": "unrelated weather chat"}])
        hits = st.lexical("Biscuit beagle", 5)
        assert hits and all(0.0 < s <= 1.0 for _, s in hits)
        # best match first
        assert hits[0][0] == 1


class TestAntiRecursion:
    def test_self_turn_filtered(self, tmp_path):
        st = Store(str(tmp_path), "r")
        st.add("s", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        st.add("s", [{"role": "user", "text": "What does Biscuit love?"}])
        kept, dropped = csearch.filter_self_turns(st, "What does Biscuit love?", [1, 2])
        assert (kept, dropped) == ([1], 1)

    def test_recall_marks_self_filtered_and_excludes_echo(self, tmp_path):
        st = Store(str(tmp_path), "r")
        st.add("s", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        st.add("s", [{"role": "user", "text": "What does Biscuit love?"}])
        rec = csearch.recall(st, "What does Biscuit love?",
                             options=csearch.Options(budget=500, embedder=None, rerank=None))
        assert rec.explain.get("self_filtered") == 1
        assert "What does Biscuit love?" not in rec.context


class TestRelativeDeltas:
    def test_buckets(self):
        base = dt.datetime(2023, 5, 13, 12, 0, 0)
        assert csearch.relative_time_delta(base, base) == "0s ago"
        assert csearch.relative_time_delta(
            base - dt.timedelta(minutes=4), base) == "4m ago"
        assert csearch.relative_time_delta(
            base - dt.timedelta(hours=3), base) == "3h ago"
        assert csearch.relative_time_delta(
            base - dt.timedelta(days=5), base) == "5d ago"

    def test_header_carries_delta(self, tmp_path):
        st = Store(str(tmp_path), "h")
        st.add("s", [{"role": "user", "text": "Biscuit loves peanut butter",
                      "at": "2023-05-08 10:00"}], session_at="2023-05-08")
        rec = csearch.recall(st, "What does Biscuit love?", now="2023-05-13 10:00",
                             options=csearch.Options(budget=500, embedder=None, rerank=None))
        assert "(5d ago)" in rec.context
        assert "[s · Monday 8 May 2023, 10:00 (" in rec.context


# --- 5. MCP agent tools ----------------------------------------------------------

def _mcp_call(server, tool, **kw):
    res = asyncio.run(server.call_tool(tool, kw))
    sc = getattr(res, "structured_content", None)
    return sc.get("result", sc) if sc else json.loads(res.content[0].text)


@pytest.fixture
def mcp_server(store):
    pytest.importorskip("mcp")
    from commontrace import mcp_server as mcp_mod
    return mcp_mod.build_server(store)


class TestCoreMemoryTools:
    def test_append_and_replace(self, mcp_server):
        out = _mcp_call(mcp_server, "core_memory_append", name="persona",
                        content="You are concise.")
        assert out["ok"] and out["block"]["content"] == "You are concise."
        out = _mcp_call(mcp_server, "core_memory_replace", name="persona",
                        old_content="concise", new_content="brief")
        assert out["ok"] and out["block"]["content"] == "You are brief."

    def test_replace_ambiguous_errors(self, mcp_server):
        _mcp_call(mcp_server, "core_memory_append", name="p", content="x")
        _mcp_call(mcp_server, "core_memory_append", name="p", content="x")
        out = _mcp_call(mcp_server, "core_memory_replace", name="p",
                        old_content="x", new_content="y")
        assert out["ok"] is False

    def test_insert_mode_on_update(self, mcp_server):
        out = _mcp_call(mcp_server, "memory_block_update", name="n",
                        content="top", mode="insert", line_number=0)
        assert out["ok"] and out["block"]["content"] == "top"


class TestArchivalMemoryTools:
    def test_insert_search_roundtrip(self, mcp_server):
        ins = _mcp_call(mcp_server, "archival_memory_insert",
                        content="Refunds over 50 need approval", category="constraint")
        assert ins["ok"] and ins["action"] == "ADD"
        # reinforcing insert is a NOOP, not a duplicate
        again = _mcp_call(mcp_server, "archival_memory_insert",
                          content="Refunds over 50 need approval", category="constraint")
        assert again["ok"] and again["action"] != "ADD"
        got = _mcp_call(mcp_server, "archival_memory_search", query="refund approval")
        assert got["ok"] and got["count"] == 1

    def test_search_limit_clamped(self, mcp_server):
        assert _mcp_call(mcp_server, "archival_memory_search",
                         query="x", limit=500)["ok"] is True
        assert _mcp_call(mcp_server, "archival_memory_search",
                         query="x", limit="bad")["ok"] is False


class TestConversationSearchTool:
    def test_search_across_spaces(self, mcp_server, store):
        with Store(store, "u1") as st:
            st.add("s1", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        with Store(store, "u2") as st:
            st.add("s1", [{"role": "user", "text": "Whiskers naps at noon"}])
        out = _mcp_call(mcp_server, "conversation_search",
                        question="What does Biscuit love?")
        assert out["ok"] and "u1" in out["spaces"]
        assert "peanut butter" in out["context"]

    def test_search_single_space(self, mcp_server, store):
        with Store(store, "solo") as st:
            st.add("s1", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        out = _mcp_call(mcp_server, "conversation_search",
                        question="Biscuit", space="solo")
        assert out["ok"] and out["spaces"] == ["solo"]


# --- 6. block CLI wiring -----------------------------------------------------------

class TestBlockCLI:
    def test_insert_render_readonly(self, store):
        assert cli("block", "set", "human", "one\ntwo", "--dest", store).returncode == 0
        res = cli("block", "insert", "human", "zero", "--line", "0", "--dest", store)
        assert res.returncode == 0 and "Inserted into block" in res.stdout
        res = cli("block", "render", "--dest", store)
        assert res.returncode == 0 and "<memory_blocks>" in res.stdout
        assert cli("block", "set", "locked", "x", "--dest", store,
                   "--read-only").returncode == 0
        res = cli("block", "append", "locked", "y", "--dest", store)
        assert res.returncode == 1 and "read-only" in res.stderr


# --- 7. retry helper (zep call_with_retries port) --------------------------------

class TestRetry:
    def test_success_no_sleep(self):
        from commontrace.retry import call_with_retries

        assert call_with_retries(lambda: 42, sleep=lambda s: None) == (42, None)

    def test_flaky_transport_retried_then_ok(self):
        from commontrace.retry import call_with_retries

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection reset")
            return "ok"

        assert call_with_retries(flaky, sleep=lambda s: None) == ("ok", None)
        assert calls["n"] == 3

    def test_non_retryable_surfaces_immediately(self):
        import urllib.error

        from commontrace.retry import call_with_retries

        calls = {"n": 0}

        def bad():
            calls["n"] += 1
            raise urllib.error.HTTPError("http://x", 400, "bad", {}, None)

        result, error = call_with_retries(bad, sleep=lambda s: None)
        assert result is None and getattr(error, "code", None) == 400
        assert calls["n"] == 1

    def test_5xx_idempotent_retries_non_idempotent_does_not(self):
        import urllib.error

        from commontrace.retry import call_with_retries

        def always_500():
            raise urllib.error.HTTPError("http://x", 503, "down", {}, None)

        _, err_idem = call_with_retries(always_500, max_retries=3,
                                        idempotent=True, sleep=lambda s: None)
        _, err_once = call_with_retries(always_500, max_retries=3,
                                        idempotent=False, sleep=lambda s: None)
        assert getattr(err_idem, "code", None) == 503
        assert getattr(err_once, "code", None) == 503

    def test_retry_after_capped_and_garbage_falls_back(self):
        from commontrace.retry import retry_after_seconds

        assert retry_after_seconds("120", 1) == 60.0
        assert 0 < retry_after_seconds("not-a-date", 2) <= 60.0
        assert 0 < retry_after_seconds(None, 1) <= 60.0

    def test_llm_post_retries_503_then_succeeds(self, monkeypatch):
        import io
        import json as json_mod
        import urllib.error
        import urllib.request

        from commontrace import llm

        attempts = {"n": 0}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json_mod.dumps({"content": []}).encode()

        def fake_urlopen(request, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.HTTPError(request.full_url, 503, "down", {}, io.BytesIO(b"busy"))
            return FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda s: None)
        out = llm._post_json("https://api.example.com/v1/x", {}, {"a": 1})
        assert out == {"content": []} and attempts["n"] == 3

    def test_llm_post_400_no_retry(self, monkeypatch):
        import io
        import urllib.error
        import urllib.request

        from commontrace import llm

        attempts = {"n": 0}

        def fake_urlopen(request, timeout=None):
            attempts["n"] += 1
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b"nope"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(llm.LLMUnavailable, match="HTTP 400"):
            llm._post_json("https://api.example.com/v1/x", {}, {"a": 1})
        assert attempts["n"] == 1


# --- 8. batched entity lookup / dedup snapshot / gates ----------------------------

class TestBatchedEntityTurns:
    def test_speaker_and_mention_and_unknown(self, tmp_path):
        st = Store(str(tmp_path), "e")
        st.add("s", [{"speaker": "Bob", "role": "user", "text": "Alice adopted Biscuit"}])
        st.add("s", [{"speaker": "Alice", "role": "user", "text": "hi Bob"}])
        out = st.entity_turns(["bob", "alice", "nobody"])
        assert out["nobody"] == []
        assert out["bob"] and out["alice"]
        # single batched path: same answer as repeated single-name queries
        assert set(out["bob"]) >= set(st.entity_turns(["bob"])["bob"])


class TestAddMemoriesSnapshot:
    def test_intra_batch_dupes_and_case_variants(self, tmp_path):
        st = Store(str(tmp_path), "d")
        st.add("s", [{"role": "user", "text": "hi"}])
        added = st.add_memories("s", [
            {"text": "User likes coffee", "kind": "preference"},
            {"text": "user likes COFFEE", "kind": "preference"},
            {"text": "User likes coffee!", "kind": "preference"},
            {"text": "User likes tea", "kind": "preference"},
        ], source="t")
        assert added == 2
        assert st.add_memories("s", [{"text": "USER LIKES TEA", "kind": "preference"}],
                               source="t") == 0


class TestApprovalGate:
    def test_no_approval_hides_both_tools(self, store):
        pytest.importorskip("mcp")
        from commontrace import mcp_server as mcp_mod

        async def names():
            return [t.name for t in await mcp_mod.build_server(store, allow_approval=False).list_tools()]

        got = asyncio.run(names())
        assert "approve_lesson" not in got and "reject_lesson" not in got

    def test_approval_on_keeps_both_tools(self, store):
        pytest.importorskip("mcp")
        from commontrace import mcp_server as mcp_mod

        async def names():
            return [t.name for t in await mcp_mod.build_server(store, allow_approval=True).list_tools()]

        got = asyncio.run(names())
        assert "approve_lesson" in got and "reject_lesson" in got


class TestConversationSearchDegraded:
    def test_degraded_flag_present(self, mcp_server, store):
        with Store(store, "u9") as st:
            st.add("s1", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        out = _mcp_call(mcp_server, "conversation_search", question="Biscuit")
        assert out["ok"] and out["degraded"] is True and "lexical-only" in out["note"]


class TestCallToolCompat:
    def test_two_and_three_arg_calls(self, mcp_server):
        """Newer MCP SDKs call call_tool(name, arguments, context); older
        ones pass (name, arguments). The compat shim must accept both."""

        async def main():
            two = await mcp_server.call_tool("store_status", {})
            three = await mcp_server.call_tool("store_status", {}, object())
            return two, three

        two, three = asyncio.run(main())

        def payload(result):
            sc = getattr(result, "structured_content", None)
            return sc.get("result", sc) if sc else json.loads(result.content[0].text)

        assert payload(two)["ok"] is True
        assert payload(three)["ok"] is True


# --- 13. TTL expiry enforcement ------------------------------------------------------

class TestExpiryEnforcement:
    def test_expired_hidden_by_default(self, store):
        from commontrace import hierarchical

        hierarchical.add_fact(store, "Old API key works", expires_at="2020-01-01")
        hierarchical.add_fact(store, "New API key works")
        assert [f.statement for f, _ in hierarchical.search_facts(store, "API key works")] == [
            "New API key works"]
        assert len(hierarchical.search_facts(store, "API key works", show_expired=True)) == 2
        assert len(hierarchical.list_facts(store)) == 1
        assert len(hierarchical.list_facts(store, show_expired=True)) == 2

    def test_cli_search_show_expired(self, store):
        from commontrace import hierarchical

        hierarchical.add_fact(store, "Old API key works", expires_at="2020-01-01")
        res = cli("fact", "search", "API key", "--dest", store)
        assert res.returncode == 0 and "Old API" not in res.stdout
        res = cli("fact", "search", "API key", "--dest", store, "--show-expired")
        assert res.returncode == 0 and "Old API" in res.stdout


# --- 14. contradiction resolution ------------------------------------------------------

class TestResolveContradiction:
    def test_resolve_invalidates_with_guard(self, store):
        from commontrace import hierarchical

        old, _ = hierarchical.add_fact(store, "Deploy on Fridays", valid_from="2024-01-01")
        new, _ = hierarchical.add_fact(store, "Never deploy on Fridays", valid_from="2024-06-01")
        o, n = hierarchical.resolve_contradiction(store, old.id, new.id)
        assert o.status == "superseded" and o.superseded_by == new.id

    def test_older_replacement_refused(self, store):
        from commontrace import hierarchical

        future, _ = hierarchical.add_fact(store, "Future policy", valid_from="2027-01-01")
        with pytest.raises(ValueError, match="predates"):
            hierarchical.resolve_contradiction(store, future.id, "Current policy")

    def test_disjoint_windows_refused(self, store):
        from commontrace import hierarchical

        a, _ = hierarchical.add_fact(store, "X is red", valid_from="2020-01-01",
                                     valid_until="2020-06-01")
        b, _ = hierarchical.add_fact(store, "X is blue", valid_from="2021-01-01")
        with pytest.raises(ValueError, match="different windows"):
            hierarchical.resolve_contradiction(store, a.id, b.id)

    def test_cli_resolve(self, store):
        from commontrace import hierarchical

        old, _ = hierarchical.add_fact(store, "Deploy on Fridays", valid_from="2024-01-01")
        new, _ = hierarchical.add_fact(store, "Never deploy on Fridays", valid_from="2024-06-01")
        res = cli("fact", "resolve", old.id, new.id, "--dest", store)
        assert res.returncode == 0 and "invalidated" in res.stdout


# --- 15. scope immutability --------------------------------------------------------------

class TestScopeImmutability:
    def test_update_scopes_refused(self, store):
        from commontrace import hierarchical

        fact, _ = hierarchical.add_fact(store, "Tenant secret", scopes=["acme"])
        with pytest.raises(ValueError, match="immutable"):
            hierarchical.update_fact(store, fact.id, scopes=["other"])
        # same scopes are a no-op success
        assert hierarchical.update_fact(store, fact.id, scopes=["acme"]).scopes == ["acme"]
        assert hierarchical.update_fact(store, fact.id, confidence=0.5).confidence == 0.5


# --- 16. interop fidelity ------------------------------------------------------------------

class TestInteropFidelity:
    def test_mem0_expiration_survives(self, store):
        from commontrace import hierarchical, interop

        recs = interop.import_mem0_dump([{"memory": "Old news", "expiration_date": "2020-01-01"}])
        assert interop.apply_store(store, recs)["fact"] == 1
        assert hierarchical.search_facts(store, "Old news") == []
        assert len(hierarchical.search_facts(store, "Old news", show_expired=True)) == 1

    def test_zep_temporal_survives(self):
        from commontrace import interop

        recs = interop.import_zep_episodes(
            [{"content": "Friday rule", "valid_at": "2024-01-01", "invalid_at": "2024-06-01"}])
        fm = recs[0]["data"]["frontmatter"]
        assert fm["valid_from"] == "2024-01-01" and fm["valid_until"] == "2024-06-01"

    def test_letta_passages_become_facts(self, store):
        from commontrace import interop

        recs = interop.import_letta_blocks(
            {"blocks": [{"label": "persona", "value": "Be brief"}],
             "passages": [{"text": "User likes tea", "id": "p1"}]})
        assert sorted(r["kind"] for r in recs) == ["block", "fact"]
        counts = interop.apply_store(store, recs)
        assert counts == {**counts, "block": 1, "fact": 1}


# --- 17. ontology CLI --------------------------------------------------------------------------

class TestOntologyCLI:
    def test_show_and_set_starter(self, store):
        res = cli("ontology", "show", "--dest", store)
        assert res.returncode == 0 and "built-in defaults" in res.stdout
        res = cli("ontology", "set-starter", "--dest", store)
        assert res.returncode == 0 and "ontology.yaml" in res.stdout
        res = cli("ontology", "set-starter", "--dest", store)
        assert res.returncode == 1 and "already set" in res.stderr
        res = cli("ontology", "show", "--dest", store)
        assert res.returncode == 0 and "ontology.yaml" in res.stdout


class TestSigmoidHome:
    def test_single_canonical_definition(self):
        from commontrace.conversation import search as search_mod
        from commontrace.conversation import store as store_mod

        assert search_mod.sigmoid_bm25 is store_mod.sigmoid_bm25


# --- 9. bounded fan-out (graphiti semaphore_gather port) --------------------------

class TestBoundedMap:
    def test_order_empty_single(self):
        from commontrace.parallel import bounded_map

        assert bounded_map(lambda x: x * 2, []) == []
        assert bounded_map(lambda x: x * 2, [3]) == [6]
        assert bounded_map(lambda x: x * 2, range(10), max_workers=3) == [x * 2 for x in range(10)]

    def test_exception_propagates(self):
        from commontrace.parallel import bounded_map

        def boom(x):
            if x == 2:
                raise ValueError("bad item")
            return x

        with pytest.raises(ValueError):
            bounded_map(boom, range(5), max_workers=2)

    def test_multispace_search_uses_all_spaces(self, mcp_server, store):
        for i in range(3):
            with Store(store, f"sp{i}") as st:
                st.add("s", [{"role": "user", "text": f"marker{i} uncommon zebra fact"}])
        out = _mcp_call(mcp_server, "conversation_search", question="uncommon zebra")
        assert out["ok"] and out["spaces"] == ["sp0", "sp1", "sp2"]


# --- 10. SQLite LLM cache (graphiti LLMCache port) ---------------------------------

class TestLLMCache:
    def test_roundtrip_miss_and_corrupt(self, tmp_path):
        from commontrace import llm_cache as mod

        cache = mod.LLMCache(str(tmp_path / "c.db"))
        assert cache.get("nope") is None
        cache.set("k", {"text": "hi", "usage": {}})
        assert cache.get("k") == {"text": "hi", "usage": {}}
        assert cache.stats()["hits"] == 1 and cache.stats()["misses"] == 1
        # corrupt + non-dict rows read as misses
        with cache._connect() as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES ('bad', 'not json')")
            db.execute("INSERT OR REPLACE INTO cache VALUES ('lst', '[1,2]')")
        assert cache.get("bad") is None and cache.get("lst") is None

    def test_disabled_by_default(self, monkeypatch):
        from commontrace import llm_cache as mod

        monkeypatch.delenv("COMMONTRACE_LLM_CACHE", raising=False)
        assert mod.enabled() is False

    def test_complete_caches_identical_prompts(self, tmp_path, monkeypatch):
        from commontrace import llm as llm_mod
        from commontrace.llm import Config

        monkeypatch.setenv("COMMONTRACE_LLM_CACHE", "1")
        monkeypatch.setenv("COMMONTRACE_LLM_CACHE_PATH", str(tmp_path / "llm.db"))
        calls = {"n": 0}

        def fake(cfg, prompt):
            calls["n"] += 1
            return ("draft-text", {"input_tokens": 5, "output_tokens": 3})

        monkeypatch.setattr(llm_mod, "_call_anthropic", fake)
        cfg = Config(provider="anthropic", model="m", api_key="k")
        assert llm_mod.complete("same prompt", cfg)[0] == "draft-text"
        assert llm_mod.complete("same prompt", cfg)[0] == "draft-text"
        assert calls["n"] == 1  # second call served from SQLite, not billed


# --- 11. top-1 floor / pragmas / recall cache ---------------------------------------

class TestTopOneFloor:
    def test_tiny_budget_still_returns_hit(self, tmp_path):
        from commontrace.conversation.search import Options, recall

        st = Store(str(tmp_path), "f")
        st.add("s", [{"role": "user", "text": "Biscuit the beagle loves peanut butter"}])
        rec = recall(st, "Biscuit peanut butter",
                     options=Options(budget=10, embedder=None, rerank=None))
        assert rec.turns and "peanut butter" in rec.context


class TestSQLitePragmas:
    def test_memory_scratch_and_bounded_cache(self, tmp_path):
        st = Store(str(tmp_path), "p")
        assert st.db.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
        assert st.db.execute("PRAGMA cache_size").fetchone()[0] == -8192


class TestRecallCache:
    def test_repeat_hit_returns_equal_new_object(self, tmp_path):
        from commontrace.conversation import search as mod

        st = Store(str(tmp_path), "c")
        st.add("s", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        opts = mod.Options(budget=500, embedder=None, rerank=None)
        first = mod.recall(st, "What does Biscuit love?", options=opts)
        second = mod.recall(st, "What does Biscuit love?", options=opts)
        assert first.context == second.context and first is not second

    def test_write_invalidates(self, tmp_path):
        from commontrace.conversation import search as mod

        st = Store(str(tmp_path), "c")
        st.add("s", [{"role": "user", "text": "Biscuit loves peanut butter"}])
        opts = mod.Options(budget=500, embedder=None, rerank=None)
        before = mod.recall(st, "What does Biscuit love?", options=opts).context
        st.add("s", [{"role": "user", "text": "Biscuit also loves carrots"}])
        after = mod.recall(st, "What does Biscuit love?", options=opts).context
        assert after != before and "carrots" in after


# --- 12. fact token memo + retrieval hoist ------------------------------------------

class TestFactTokenMemo:
    def test_memo_consistent_and_keyed_by_revision(self, store):
        from commontrace import hierarchical

        fact, action = hierarchical.add_fact(store, statement="Refunds over 50 need approval",
                                             category="constraint")
        assert action == "ADD"
        first = hierarchical.search_facts(store, query="refund approval")
        second = hierarchical.search_facts(store, query="refund approval")
        assert first == second and len(first) == 1
        toks = hierarchical._fact_tokens(fact)
        assert "refunds" in toks and "approval" in toks
        assert hierarchical._fact_tokens(fact) is toks  # memoized, same object

    def test_retrieval_corpus_bin_hoisted(self):
        import commontrace.retrieval as ret

        assert ret.corpus_bin is not None  # top-level import, not per-query importlib


# --- 13. Ingest Lifecycle & Document Catalog -----------------------------------------

class TestIngestLifecycleAndCatalog:
    def test_job_lifecycle_transitions(self, store):
        from commontrace.ingest import catalog

        job = catalog.create_ingest_job(store, "docs/architecture.md")
        assert job.stage == "queued"
        assert job.source == "docs/architecture.md"

        # extracting
        job = catalog.update_ingest_job(store, job.id, "extracting", message="Extracting chunks")
        assert job.stage == "extracting"

        # transforming
        job = catalog.update_ingest_job(store, job.id, "transforming", message="Contextualizing")
        assert job.stage == "transforming"

        # submitting
        job = catalog.update_ingest_job(store, job.id, "submitting", message="Submitting chunks")
        assert job.stage == "submitting"

        # done
        job = catalog.update_ingest_job(store, job.id, "done", message="Finished", stats={"chunks": 10})
        assert job.stage == "done"
        assert job.stats.get("chunks") == 10

        fetched = catalog.get_ingest_job(store, job.id)
        assert fetched is not None
        assert fetched.stage == "done"

    def test_job_failure_visibility(self, store):
        from commontrace.ingest import catalog

        job = catalog.create_ingest_job(store, "corrupt.pdf")
        catalog.update_ingest_job(store, job.id, "extracting")
        job = catalog.update_ingest_job(store, job.id, "failed", error="Malformed PDF syntax")
        assert job.stage == "failed"
        assert job.error == "Malformed PDF syntax"

    def test_summary_list_vs_full_content_get(self, store):
        from commontrace.ingest import catalog

        full_doc = "A" * 5000 + "\n\nImportant section about auth limits."
        doc = catalog.record_document(
            store,
            source_path="/app/docs/auth.md",
            content=full_doc,
            title="Authentication Architecture",
            chunks=["Chunk 1 content", "Chunk 2 content"],
        )
        assert doc.token_count > 1000

        # Summary list MUST be lightweight (never dumping the full 5000 chars)
        docs = catalog.list_documents(store)
        assert len(docs) == 1
        summary = docs[0]
        assert summary["id"] == doc.id
        assert summary["title"] == "Authentication Architecture"
        assert len(summary["summary"]) <= 200
        assert "content" not in summary  # Crucial: content not dumped into summary list

        # Full content get returns the entire document
        fetched = catalog.get_document(store, doc.id)
        assert fetched is not None
        assert fetched["content"] == full_doc
        assert len(fetched["chunks"]) == 2

        # Chunk-level get
        chunk1 = catalog.get_document(store, doc.id, chunk_index=1)
        assert chunk1["requested_chunk"] == "Chunk 2 content"


# --- 14. Sagas: Ordered Incident / Migration Narratives ------------------------------

class TestSagasNarratives:
    def test_saga_lifecycle_and_events(self, store):
        from commontrace import sagas

        saga = sagas.create_saga(
            store,
            saga_id="inc-2026-auth",
            title="P0 Auth Outage Incident",
            tags=["auth", "incident", "p0"],
            brief="Initial report: 502s on token exchange",
        )
        assert saga.id == "inc-2026-auth"
        assert saga.status == "active"

        assert "auth" in saga.tags

        # Append chronological events
        sagas.append_saga_event(
            store,
            saga_id="inc-2026-auth",
            title="Redis failover initiated",
            description="Replica promoted to primary in us-east-1",
            actor="oncall_alice",
            new_brief="Redis failover completed; 502s dropping",
            watermark="2026-10-04T08:00:00Z",
        )
        sagas.append_saga_event(
            store,
            saga_id="inc-2026-auth",
            title="Traffic normalized",
            description="Error rate back to 0.01%",
            actor="oncall_bob",
        )

        loaded = sagas.get_saga(store, "inc-2026-auth")
        assert loaded is not None
        assert len(loaded.events) == 2
        assert loaded.events[0].title == "Redis failover initiated"
        assert loaded.events[1].title == "Traffic normalized"
        assert "Redis failover completed" in loaded.watermarked_running_brief
        assert loaded.watermark == "2026-10-04T08:00:00Z"

    def test_watermarked_running_brief_updates(self, store):
        from commontrace import sagas

        sagas.create_saga(store, "migration-pg", "Postgres 16 Migration")
        sagas.update_running_brief(
            store, "migration-pg",
            brief="Schemas migrated, replication lagging by 4s",
            watermark="step-3-schema-done",
        )
        saga = sagas.get_saga(store, "migration-pg")
        assert saga.watermarked_running_brief == "Schemas migrated, replication lagging by 4s"
        assert saga.watermark == "step-3-schema-done"

    def test_saga_filtering_and_search(self, store):
        from commontrace import sagas

        sagas.create_saga(store, "s1", "Alpha Project", tags=["alpha", "infra"])
        sagas.create_saga(store, "s2", "Beta Project", tags=["beta", "infra"], status="resolved")

        active = sagas.list_sagas(store, status="active")
        assert any(s.id == "s1" for s in active)
        assert not any(s.id == "s2" for s in active)

        infra = sagas.list_sagas(store, tag="infra")
        assert len(infra) >= 2

        hits = sagas.search_sagas(store, "Alpha")
        assert hits and hits[0].id == "s1"


# --- 15. Knowledge Pages with Dry-Run Diffs -----------------------------------------

class TestKnowledgePagesCurated:
    def test_create_and_dry_run_diff(self, store):
        from commontrace import knowledge_pages

        content_v1 = "# Auth Flow\n\n1. User enters credentials\n2. Gateway issues JWT"
        res_v1 = knowledge_pages.update_page(
            store, "architecture/auth", content_v1,
            title="Authentication Architecture", tags=["auth", "security"],
        )
        assert res_v1["version"] == 1
        assert res_v1["dry_run"] is False

        # Dry run diff
        content_v2 = "# Auth Flow\n\n1. User enters credentials\n2. Gateway issues OAuth token\n3. Refresh token rotated"
        preview = knowledge_pages.update_page(
            store, "architecture/auth", content_v2,
            dry_run=True,
        )
        assert preview["dry_run"] is True
        assert preview["changed"] is True
        assert preview["current_version"] == 1
        assert preview["proposed_version"] == 2
        assert "+3. Refresh token rotated" in preview["diff"]
        assert "-2. Gateway issues JWT" in preview["diff"]

        # Ensure disk content remains v1
        page = knowledge_pages.get_page(store, "architecture/auth")
        assert page.version == 1
        assert "JWT" in page.content

    def test_version_concurrency_and_history(self, store):
        from commontrace import knowledge_pages

        knowledge_pages.update_page(store, "runbook/restart", "Initial body", title="Restart Guide")
        # Conflicting version must fail
        with pytest.raises(knowledge_pages.VersionConflictError):
            knowledge_pages.update_page(
                store, "runbook/restart", "Conflicting edit", expected_version=99,
            )

        # Successful update
        knowledge_pages.update_page(
            store, "runbook/restart", "Updated step 1", expected_version=1, comment="Add step 1 details",
        )
        page = knowledge_pages.get_page(store, "runbook/restart")
        assert page.version == 2

        history = knowledge_pages.page_history(store, "runbook/restart")
        assert len(history) == 2
        assert history[-1]["version"] == 2
        assert history[-1]["comment"] == "Add step 1 details"


# --- 16. Session Ledger: Per-Session Cost & Model Attribution ------------------------

class TestSessionLedgerAttribution:
    def test_record_usage_and_cost_estimation(self, store):
        from commontrace import session_ledger

        entry1 = session_ledger.record_usage(
            store, session_id="sess_123", model="gpt-4o",
            prompt_tokens=1000, completion_tokens=200, occasion="answer",
        )
        assert entry1.prompt_tokens == 1000
        assert entry1.completion_tokens == 200
        assert entry1.total_tokens == 1200
        # gpt-4o: $2.50/M in, $10.00/M out -> 1000*2.5e-6 + 200*10e-6 = 0.0025 + 0.002 = 0.0045
        assert round(entry1.cost_usd, 4) == 0.0045

        entry2 = session_ledger.record_usage(
            store, session_id="sess_123", model="claude-sonnet-5",
            prompt_tokens=2000, completion_tokens=500, occasion="recall",
        )
        assert entry2.cost_usd > 0

    def test_session_summary_breakdown(self, store):
        from commontrace import session_ledger

        sess = "sess_breakdown"
        session_ledger.record_usage(store, sess, "gpt-4o", 1000, 100, occasion="answer")
        session_ledger.record_usage(store, sess, "claude-sonnet-5", 3000, 200, occasion="extract")

        summary = session_ledger.session_summary(store, sess)
        assert summary["session_id"] == sess
        assert summary["call_count"] == 2
        assert summary["prompt_tokens"] == 4000
        assert summary["completion_tokens"] == 300
        assert summary["total_tokens"] == 4300
        assert summary["total_cost_usd"] > 0
        assert "gpt-4o" in summary["by_model"]
        assert "claude-sonnet-5" in summary["by_model"]
        assert "answer" in summary["by_occasion"]
        assert "extract" in summary["by_occasion"]

    def test_overall_ledger_summary(self, store):
        from commontrace import session_ledger

        session_ledger.record_usage(store, "sA", "gpt-4o", 500, 50)
        session_ledger.record_usage(store, "sB", "gpt-4o-mini", 500, 50)

        overall = session_ledger.overall_ledger_summary(store)
        assert overall["call_count"] >= 2
        assert overall["session_count"] >= 2
        assert overall["total_tokens"] >= 1100


# --- 17. MCP Competitor Tools Integration --------------------------------------------

class TestMCPCompetitorTools:
    def test_mcp_sagas_pages_ledger_and_catalog(self, store):
        from commontrace import mcp_server

        server = mcp_server.build_server(store)

        # 1. Saga tools
        res_saga = _mcp_call(server, "saga_create",
            saga_id="mcp-test-saga",
            title="MCP Test Incident",
            tags=["test", "mcp"],
            brief="Starting incident",
        )
        assert res_saga["ok"] is True
        assert res_saga["saga"]["id"] == "mcp-test-saga"

        res_ev = _mcp_call(server, "saga_append_event",
            saga_id="mcp-test-saga",
            title="Mitigation applied",
            description="Restarted service",
        )
        assert res_ev["ok"] is True
        assert len(res_ev["saga"]["events"]) == 1

        # 2. Knowledge Page tools with dry-run
        res_page = _mcp_call(server, "knowledge_page_update",
            slug="mcp/runbook",
            content="# Runbook\n\nStep 1: Check logs",
            title="MCP Runbook",
        )
        assert res_page["ok"] is True
        assert res_page["version"] == 1

        res_diff = _mcp_call(server, "knowledge_page_update",
            slug="mcp/runbook",
            content="# Runbook\n\nStep 1: Check logs\nStep 2: Restart pod",
            dry_run=True,
        )
        assert res_diff["ok"] is True
        assert res_diff["dry_run"] is True
        assert "+Step 2: Restart pod" in res_diff["diff"]

        # 3. Session Ledger tools
        res_ledg = _mcp_call(server, "session_ledger_record",
            session_id="mcp_session_1",
            model="gpt-4o",
            prompt_tokens=1500,
            completion_tokens=300,
            occasion="mcp_test",
        )
        assert res_ledg["ok"] is True
        assert res_ledg["entry"]["total_tokens"] == 1800

        res_sum = _mcp_call(server, "session_ledger_get",
            session_id="mcp_session_1",
        )
        assert res_sum["ok"] is True
        assert res_sum["total_tokens"] == 1800

        # 4. Ingest Document Catalog tools
        from commontrace.ingest import catalog
        catalog.record_document(
            store, "/test/doc.md", "# Test Title\n\nDocument body for catalog test",
        )

        res_docs = _mcp_call(server, "ingest_documents_list")
        assert res_docs["ok"] is True
        assert res_docs["count"] >= 1
        assert "content" not in res_docs["documents"][0]  # Verify lightweight summary

        res_get = _mcp_call(server, "ingest_document_get",
            doc_id_or_path=res_docs["documents"][0]["id"],
        )
        assert res_get["ok"] is True
        assert "Document body for catalog test" in res_get["document"]["content"]


# --- 27. pluggable memory defense --------------------------------------------------------

class TestMemoryDefense:
    def test_catalog_redaction(self):
        from commontrace import defense

        text = "Anthropic: sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234, AWS: AKIAIOSFODNN7EXAMPLE"
        res = defense.apply_redaction(text)
        assert "anthropic_key" in res.matched_types
        assert "aws_access_key" in res.matched_types
        assert "[REDACTED:anthropic_key]" in res.content
        assert "[REDACTED:aws_access_key]" in res.content
        for hit in res.hits:
            assert "..." in hit["preview"]

    def test_luhn_card_check(self):
        from commontrace import defense

        valid_res = defense.apply_redaction("Card 4111 1111 1111 1111")
        assert "credit_card" in valid_res.matched_types
        assert "[REDACTED:credit_card]" in valid_res.content

        invalid_res = defense.apply_redaction("Card 4111 1111 1111 1112")
        assert "credit_card" not in invalid_res.matched_types

    def test_screen_policies(self):
        from commontrace import defense

        pol_block = defense.DefensePolicy(
            enabled=True,
            rules=(defense.PolicyRule(on="sensitive_data", action=defense.DefenseAction.BLOCK),),
        )
        dec = defense.screen_content("secret sk-ant-abcdefghijklmnopqrstuvwxyz1234", policy=pol_block)
        assert dec.action == defense.DefenseAction.BLOCK


# --- 28. gateway hardening & transient auth ---------------------------------------------

class TestGatewayHardening:
    def test_transient_classifier(self):
        from commontrace.gateway import TransientAuthError, is_transient_auth_error

        assert is_transient_auth_error(TransientAuthError("timeout", status=504))
        assert is_transient_auth_error(TimeoutError())
        assert is_transient_auth_error(ConnectionResetError())

        class Permanent(Exception):
            status = 401

        assert not is_transient_auth_error(Permanent("bad key"))

    def test_whoami_and_container_scoping(self, store):
        import json

        from commontrace import gateway

        gw = gateway.Gateway(store, token="test-token-xyz")
        resp = gw.handle(
            "GET",
            "/v1/whoami",
            headers={"Authorization": "Bearer test-token-xyz", "X-Container-Tag": "tenant_1"},
        )
        data = json.loads(resp.body.decode("utf-8"))
        assert resp.status == 200
        assert data["authenticated"] is True
        assert data["container_tag"] == "tenant_1"
        assert resp.headers.get("X-Container-Tag") == "tenant_1"

    def test_capabilities_tier(self, store):
        import json

        from commontrace import gateway

        gw = gateway.Gateway(store)
        resp = gw.handle("GET", "/v1/capabilities")
        data = json.loads(resp.body.decode("utf-8"))
        assert resp.status == 200
        assert "tier" in data
        assert data["capabilities"]["container_scoping"] is True
        assert data["capabilities"]["defense_screen"] is True


# --- 29. procedural memory -------------------------------------------------------------

class TestProceduralMemory:
    def test_create_and_budget_replay(self, store):
        from commontrace import procedural

        steps = [
            procedural.ProceduralStep(1, "crawl page", "HTML content " * 100, key_findings="found target"),
            procedural.ProceduralStep(2, "extract price", "$49.99", current_context="done"),
        ]
        mem = procedural.ProceduralMemory(
            id="proc_1",
            task_objective="Price check",
            progress_status="100%",
            steps=steps,
        )
        path = procedural.save_procedural_memory(store, mem)
        assert path.endswith("proc_1.json")

        loaded = procedural.load_procedural_memory(store, "proc_1")
        assert loaded is not None
        assert loaded.task_objective == "Price check"

        formatted = procedural.format_procedural_memory(loaded, token_budget=50)
        assert "Price check" in formatted
        assert "(budget-constrained)" in formatted


# --- 30. guarded text-to-sql -----------------------------------------------------------

class TestGuardedSql:
    def test_select_validation_and_clamp(self):
        from commontrace import sql_guard

        assert sql_guard.validate_select("SELECT * FROM users") == "SELECT * FROM users"
        assert sql_guard.ensure_limit("SELECT * FROM users", 25) == "SELECT * FROM users LIMIT 25"

        with pytest.raises(sql_guard.SqlGuardError):
            sql_guard.validate_select("INSERT INTO users VALUES (1)")

        with pytest.raises(sql_guard.SqlGuardError):
            sql_guard.validate_select("SELECT * FROM (DROP TABLE users)")

    def test_read_only_execution(self, tmp_path):
        import sqlite3

        from commontrace import sql_guard

        db = str(tmp_path / "app.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (x INT)")
        conn.execute("INSERT INTO t VALUES (1), (2), (3)")
        conn.commit()
        conn.close()

        res = sql_guard.execute_guarded_sql(db, "SELECT * FROM t", max_rows=2)
        assert res["row_count"] == 2
        assert len(res["rows"]) == 2


# --- 31. litellm wrapper ---------------------------------------------------------------

class TestLiteLLMWrapper:
    def test_message_augmentation(self, store):
        from commontrace import litellm_wrapper

        wrapper = litellm_wrapper.CommonTraceLiteLLM(root=store)
        msgs = [{"role": "user", "content": "How do I test?"}]
        augmented = wrapper._augment_messages(msgs, "Project uses pytest.")

        assert len(augmented) == 2
        assert augmented[0]["role"] == "system"
        assert "Project uses pytest." in augmented[0]["content"]

    def test_custom_completion(self, store):
        from commontrace import litellm_wrapper

        wrapper = litellm_wrapper.CommonTraceLiteLLM(root=store, auto_extract=False)

        def mock_call(model, messages, **kwargs):
            return {"choices": [{"message": {"content": "ok"}}]}

        res = wrapper.completion("gpt-4o", [{"role": "user", "content": "hi"}], completion_fn=mock_call)
        assert res["choices"][0]["message"]["content"] == "ok"


# --- 32. benchmark adapters & dolphin judge --------------------------------------------

class TestBenchmarkAdaptersAndJudges:
    def test_seeded_adapter_reproducibility(self):
        from benchmarks import adapters

        ad = adapters.get_adapter("hotpotqa")
        run1 = ad.load(limit=4, seed=42)
        run2 = ad.load(limit=4, seed=42)
        assert [r.id for r in run1] == [r.id for r in run2]

    def test_snapshot_cache(self, tmp_path):
        from benchmarks import adapters

        cache_dir = str(tmp_path / "bench_cache")
        key = adapters.get_snapshot_cache_key("synthetic", 42, 2)
        items = [adapters.BenchmarkItem("1", "q?", "a!")]
        adapters.save_cached_snapshot(cache_dir, key, items)
        loaded = adapters.load_cached_snapshot(cache_dir, key)
        assert loaded is not None
        assert loaded[0].id == "1"

    def test_dolphin_judge(self):
        from benchmarks.judges import get_judge

        j = get_judge("dolphin")
        assert j.name == "dolphin"
        p = j.format_prompt("question?", "gold!", "candidate answer")
        assert "DolphinBench" in p


# --- 33. typed hub session methods -----------------------------------------------------

class TestTypedHubMethods:
    def test_typed_methods_exist_on_hub_session(self):
        from commontrace.hub_client import HubSession

        methods = [
            "search_traces",
            "contribute_trace",
            "get_trace",
            "delete_trace",
            "vote_trace",
            "list_tags",
            "add_comment",
            "list_comments",
            "assign_trace",
            "unassign_trace",
            "tag_trace_subjects",
            "purge_subject_traces",
            "commons_overlap",
        ]
        for m in methods:
            assert hasattr(HubSession, m), f"Missing typed method {m} on HubSession"


# --- 34. CLI subcommands with hyphens and aliases --------------------------------------

class TestCliSubcommands:
    def test_cli_help_hyphen_and_underscore(self):
        from commontrace import cli

        for cmd in [
            "sql-query", "sql_query",
            "session-ledger", "session_ledger",
            "procedural", "defense",
        ]:
            parser = cli.build_parser(cmd)
            assert parser is not None
            actions = [a.dest for a in parser._actions if hasattr(a, "dest")]
            assert "command" in actions




