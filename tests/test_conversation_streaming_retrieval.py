"""Independent scoring oracle and durable pagination regression coverage."""
import math
import random
import re

from commontrace._stem import stem
from commontrace.conversation import profile
from commontrace.conversation import store as storage


def _reference_bm25(units, query, limit):
    """Pre-streaming list/count scorer, intentionally independent of Counters."""
    def terms(text):
        return [stem(t) for t in re.findall(r"[a-z0-9]+", text.lower())
                if t not in profile.STOPWORDS]

    q = set(terms(query))
    docs = [(uid, terms(body)) for uid, _turn, body, _hash in units]
    if not docs or not q:
        return []
    avg = sum(len(doc) for _uid, doc in docs) / len(docs)
    df = {term: sum(term in doc for _uid, doc in docs) for term in q}
    scores = []
    for uid, doc in docs:
        score = 0.0
        for term in q:
            frequency = doc.count(term)
            if frequency:
                idf = math.log(1 + (len(docs) - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * frequency * 2.2 / (
                    frequency + 1.2 * (0.25 + 0.75 * len(doc) / avg))
        if score > 0:
            scores.append((uid, storage.sigmoid_bm25(score, len(q))))
    return sorted(scores, key=lambda item: -item[1])[:limit]


def test_streamed_bm25_matches_materialized_scoring_and_stable_ties():
    rng = random.Random(923)
    vocabulary = ["cat", "cats", "go", "paris", "london", "the", "", "123"]
    for _case in range(80):
        units = [(uid, uid, " ".join(rng.choices(vocabulary, k=rng.randrange(30))), str(uid))
                 for uid in range(rng.randrange(60))]
        query = " ".join(rng.choices(vocabulary, k=rng.randrange(10)))
        limit = rng.randrange(20)
        assert storage._python_bm25(iter(units), query, limit) == _reference_bm25(units, query, limit)
    tied = [(uid, uid, "Paris cats", str(uid)) for uid in [9, 2, 7, 4]]
    assert [uid for uid, _score in storage._python_bm25(iter(tied), "Paris", 2)] == [9, 2]


def test_scoped_fallback_streams_only_eligible_units(tmp_path, monkeypatch):
    with storage.Store(str(tmp_path), "scoped") as store:
        store.add("allowed", [{"role": "user", "content": "Paris cats travel"}], extract_profile=False)
        store.add("excluded", [{"role": "user", "content": "Paris Paris cats"}], extract_profile=False)
        units = store.units()
        allowed = {units[0][1]}
        eligible = [unit for unit in units if unit[1] in allowed]
        monkeypatch.setattr(storage, "FTS5", False)

        def no_materialization(*args, **kwargs):
            raise AssertionError("fallback must stream batches instead of loading all source bodies")

        monkeypatch.setattr(store, "units", no_materialization)
        assert store.lexical("Paris cats", 10, allowed=allowed) == _reference_bm25(eligible, "Paris cats", 10)
        assert store.lexical("Paris", 10, allowed=set()) == []


def test_unit_and_timeline_keyset_pages_preserve_order_and_gaps(tmp_path):
    with storage.Store(str(tmp_path), "pages") as store:
        for index in range(7):
            store.add(f"session-{index}", [{"role": "user", "content": f"Travel event {index}"}],
                      session_at=f"2024-01-{7 - index:02d}", extract_profile=False)
        # Gapped IDs distinguish a keyset cursor from an offset.
        store.db.execute("DELETE FROM units WHERE id IN (2, 5)")
        store.db.commit()
        expected_units = store.units()
        actual_units, cursor = [], None
        while page := store.units(after_id=cursor, limit=2):
            actual_units.extend(page)
            cursor = page[-1][0]
        assert actual_units == expected_units
        assert store.units(limit=0) == []

        expected_sessions = store.timeline()
        actual_sessions, cursor = [], None
        while page := store.timeline(limit=2, after_seq=cursor):
            actual_sessions.extend(page)
            cursor = page[-1]["seq"]
        assert actual_sessions == expected_sessions
        assert [row["session"] for row in actual_sessions] == [f"session-{index}" for index in range(7)]
        assert store.timeline(limit=0) == []
