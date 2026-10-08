"""Canonical-store Unicode retrieval, frozen compatibility and mutation fencing."""
from __future__ import annotations

import concurrent.futures
import sqlite3

import pytest

from commontrace.conversation import Options, Store, recall, unicode_index
from commontrace.conversation import store as store_module
from commontrace.conversation.search import subqueries
from commontrace.conversation.store import write_txn


@pytest.mark.parametrize("body,query", [
    ("北辰缓存容量是五百条记录。", "北辰缓存"),
    ("オーロラキャッシュ容量は五百件です。", "キャッシュ容量"),
    ("북극캐시용량은오백개입니다.", "캐시용량"),
    ("Le café préféré est près du marché.", "café marché"),
    ("Проект хранит память пользователя.", "память"),
    ("ذاكرة المشروع تحتفظ بالمعلومات.", "ذاكرة"),
    ("北辰缓存", "北"),
    ("La caféothèque conserve les dossiers.", "cafe\u0301othe\u0300que"),
])
def test_unicode_substrings_and_words_reach_real_source_units(tmp_path, body, query):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"speaker": "Agent", "text": body}], extract_profile=False)
        if unicode_index.has_cjk(query) or query in ("память", "ذاكرة"):
            assert store._ascii_lexical(query, 10) == []
        hits = store.lexical(query, 10)
        assert [uid for uid, _score in hits] == [1]
        assert all(0 < score <= 1 for _, score in hits)
        assert store.get_meta("unicode_postings") == unicode_index.VERSION


def test_ascii_queries_do_not_install_or_mutate_unicode_index(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "cache capacity", "speaker": "Agent"},
                             {"text": "capacity cache duration"}, {"text": "北辰缓存"}], extract_profile=False)
        for query in ("cache", "capacity duration", '" OR cache NOT * --'):
            assert store.lexical(query, 10) == store._ascii_lexical(query, 10)
        assert store.get_meta("unicode_postings") is None
        assert not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='unicode_postings'").fetchone()
        before = store.get_meta("units_revision")
        hashes = store.units()
        store.prepare_lexical("北辰")
        assert store.units() == hashes and store.get_meta("units_revision") == before
        for query in ("cache", "capacity duration"):
            assert store.lexical(query, 10) == store._ascii_lexical(query, 10)


def test_accented_query_cannot_fuse_unrelated_ascii_fragments(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "The Moire display pattern is unrelated."},
                             {"text": "Les détails sont dans la mémoire."}], extract_profile=False)
        assert [uid for uid, _score in store._ascii_lexical("mémoire", 1)] == [1]
        assert [uid for uid, _score in store.lexical("mémoire", 1)] == [2]
        result = recall(store, "mémoire", options=Options(
            pool=1, budget=100, embedder=None, rerank=None, profile_facts=0, instructions=0,
            summaries=False, neighbours_before=0, neighbours_after=0))
        assert result.ranked == result.turns == [2]
        assert "mémoire" in result.context and "Moire" not in result.context
        assert unicode_index.ascii_query("mémoire CT-492 cache_size 北辰512") == "CT 492 cache_size"


def test_multi_character_cjk_query_cannot_match_single_shared_character(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "北方公交已到达。"}], extract_profile=False)
        assert store.lexical("北辰缓存", 10) == []
        assert store.lexical("北", 10)
        with Store(str(tmp_path), "global", read_only=True) as frozen:
            assert frozen.lexical("北辰缓存", 10) == []
        with write_txn(store.db):
            store.db.execute("UPDATE units SET body='北方公交已到达。' WHERE id=1")
        with store.read_snapshot():
            assert store.lexical("北辰缓存", 10) == []


@pytest.mark.parametrize("query", ["mémoire: détails", "mémoire, détails, capacité"])
def test_unicode_aspect_expansion_preserves_actual_words(tmp_path, query):
    expanded = subqueries(query)
    assert not any(word in ("moire", "tails", "capacit") for part in expanded for word in part.split())
    assert "mémoire" in " ".join(expanded) and "détails" in " ".join(expanded)
    with Store(str(tmp_path), "global") as store:
        store.add("noise", [{"text": "The Moire tails pattern is unrelated."}], extract_profile=False)
        store.add("source", [{"text": "Les détails sont dans la mémoire."}], extract_profile=False)
        result = recall(store, query, options=Options(
            pool=1, budget=20, primary_hits=1, embedder=None, rerank=None, profile_facts=0,
            instructions=0, summaries=False, neighbours_before=0, neighbours_after=0))
        assert result.ranked == result.turns == [2]
        assert "mémoire" in result.context and "Moire" not in result.context


@pytest.mark.parametrize("limit", [1, 2, 50])
def test_scope_statistics_and_limits_cannot_be_crowded_out(tmp_path, limit):
    with Store(str(tmp_path), "global") as store:
        store.add("own", [{"text": "北辰缓存容量信息。"}], extract_profile=False)
        baseline = store.lexical("北辰缓存", limit, allowed={1})
        for i in range(20):
            store.add(f"other{i}", [{"text": "北辰缓存北辰缓存"}], extract_profile=False)
        assert store.lexical("北辰缓存", limit, allowed={1}) == baseline
        assert store.lexical("北辰缓存", limit, allowed=set()) == []
        assert store.lexical("北辰缓存", 0) == []
        assert store.lexical("北辰缓存", limit, allowed={99999}) == []


def test_legacy_read_only_bank_falls_back_without_writing(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "北辰缓存容量信息"}], extract_profile=False)
        assert store.get_meta("unicode_postings") is None
    with Store(str(tmp_path), "global", read_only=True) as frozen:
        before = frozen.db.total_changes
        fallback = frozen.lexical("北辰缓存", 10)
        assert fallback and frozen.db.total_changes == before
        assert frozen.get_meta("unicode_postings") is None
    with Store(str(tmp_path), "global") as upgraded:
        assert upgraded.lexical("北辰缓存", 10) == fallback


def test_external_updates_and_dirty_frozen_snapshots_use_current_source(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "北辰缓存容量信息"}], extract_profile=False)
        assert store.lexical("北辰缓存", 10)
        with sqlite3.connect(store.path) as external:
            external.execute("UPDATE units SET body='新月索引容量信息',hash='external' WHERE id=1")
        with Store(str(tmp_path), "global", read_only=True) as frozen:
            changes = frozen.db.total_changes
            assert frozen.lexical("北辰缓存", 10) == []
            fallback = frozen.lexical("新月索引", 10)
            assert fallback and frozen.db.total_changes == changes
        assert store.lexical("新月索引", 10) == fallback
        with sqlite3.connect(store.path) as external:
            # Moving a unit identity must erase all postings for the old id.
            external.execute("UPDATE units SET id=77 WHERE id=1")
        assert [uid for uid, _ in store.lexical("新月索引", 10)] == [77]
        with sqlite3.connect(store.path) as external:
            external.execute("UPDATE units SET body='English only' WHERE id=77")
        assert store.lexical("新月索引", 10) == []
        assert store.db.execute("SELECT COUNT(*) FROM unicode_postings").fetchone()[0] == 0


def test_dirty_read_transaction_falls_back_without_upgrade(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("first", [{"text": "北辰缓存容量信息"}], extract_profile=False)
        store.prepare_lexical("北辰缓存")
        store.add("second", [{"text": "北辰缓存容量补充"}], extract_profile=False)
        with store.read_snapshot():
            before = store.db.total_changes
            hits = store.lexical("北辰缓存", 10)
            assert len(hits) == 2 and store.db.total_changes == before
        assert store.lexical("北辰缓存", 10) == hits


def test_index_and_streaming_bm25_are_exactly_equivalent(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": text} for text in [
            "北辰缓存容量信息", "新月索引容量信息", "北辰缓存北辰缓存", "English only",
            "café déjà marché", "café marché marché", "北辰キャッシュ用량"
        ]], extract_profile=False)
        store.prepare_lexical("北辰")
        for query in ("北辰缓存", "容量", "café marché", "キャッシュ", "不存在", "北"):
            for allowed in (None, {1, 3, 5}, set()):
                units = (u for batch in store.unit_batches(allowed=allowed) for u in batch)
                assert unicode_index.rank(store.db, query, 3, allowed) == \
                    unicode_index.stream_rank(units, query, 3)


def test_deletion_rollback_and_erasure_are_transactional(tmp_path):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "北辰缓存容量信息"}], extract_profile=False)
        expected = store.lexical("北辰缓存", 10)
        with pytest.raises(RuntimeError), write_txn(store.db):
            store.db.execute("DELETE FROM units")
            raise RuntimeError("abort")
        assert store.lexical("北辰缓存", 10) == expected
        assert store.delete_session("source") == 1
        assert store.lexical("北辰缓存", 10) == []
        for table in ("unicode_documents", "unicode_postings", "unicode_dirty"):
            assert store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_concurrent_connections_and_incremental_ingestion(tmp_path):
    root = str(tmp_path)
    with Store(root, "global") as initial:
        initial.add("first", [{"text": "北辰缓存"}], extract_profile=False)
        initial.prepare_lexical("北辰缓存")

    def ingest(i):
        with Store(root, "global") as store:
            store.add(f"session{i}", [{"text": f"北辰缓存容量第{i}版"}], extract_profile=False)
            return store.lexical("北辰缓存", 30)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(ingest, range(12)))
    with Store(root, "global") as store:
        assert len(store.lexical("北辰缓存", 30)) == 13
        assert not unicode_index.dirty(store.db)


def test_no_fts_dependency_and_operator_characters_are_data(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "FTS5", False)
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": "北辰缓存 café"}], extract_profile=False)
        assert store.lexical('北辰缓存"; DROP TABLE units; --', 10)
        assert store.lexical("cache", 10) == []
        assert store.lexical("café", 10)
        assert len(store.units()) == 1


def test_query_term_bound_and_streamed_backfill(tmp_path):
    query = " ".join(chr(0x4E00 + i) for i in range(400))
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"text": query}], extract_profile=False)
        assert store.lexical(query, 3)
        assert len(unicode_index.terms(query)) > unicode_index.MAX_QUERY_TERMS
        indexed = unicode_index.rank(store.db, query, 3, None)
        units = (u for batch in store.unit_batches() for u in batch)
        assert indexed == unicode_index.stream_rank(units, query, 3)


@pytest.mark.parametrize("strategy", ["legacy", "coverage-v1"])
@pytest.mark.parametrize("filler,answer,question", [
    ("日常维护记录", "北辰缓存容量是512条记录。", "北辰缓存容量"),
    ("運用情報", "アカツキキャッシュ容量は512件です。", "アカツキキャッシュ容量"),
    ("일반운영기록", "북극캐시용량은512개입니다.", "북극캐시용량"),
    ("Cafe storage notes. ", "La caféothèque contient 512 dossiers.", "cafe\u0301othe\u0300que"),
])
def test_end_to_end_recall_returns_grounded_multilingual_tail(tmp_path, strategy, filler, answer, question):
    with Store(str(tmp_path), "global") as store:
        store.add("source", [{"speaker": "Ada", "text": filler * 100 + answer}], extract_profile=False)
        options = Options(budget=100, excerpt_tokens=40, embedder=None, rerank=None,
                          profile_facts=0, instructions=0, summaries=False,
                          neighbours_before=0, neighbours_after=0, context_strategy=strategy)
        result = recall(store, question, options=options)
        assert result.ranked == result.turns == [1]
        assert answer in result.context and result.tokens <= 100
        assert result.explain["coverage"]["confidence"] > 0
