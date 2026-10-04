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
