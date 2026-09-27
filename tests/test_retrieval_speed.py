"""Faster retrieval that ranks exactly as before.

rank_lessons now keeps a per-store index between calls, and the lesson
cache keeps its parsed file in memory. Neither may change a single
relevance: relevance decides which lessons clear the floor, and that is the
eligibility denominator of every running experiment. These pin that the
cached path is indistinguishable from a fresh one, and that every kind of
change to the store is seen.
"""
from __future__ import annotations

import json
import os
import random

import pytest

from commontrace import lesson_cache, lesson_io, paths, retrieval

WORDS = (
    "retry backoff timeout pagination cursor offset migration rollback schema index "
    "lock deadlock payroll grievance renewal churn discount quota forecast campaign "
    "segment attribution trajectory calibration actuator waypoint indemnity liability "
    "warranty jurisdiction resets uploads failing failed retries"
).split()


def _lesson(rng, i):
    pick = lambda n: " ".join(rng.choice(WORDS) for _ in range(n))  # noqa: E731
    return {
        "name": f"lesson-{i}",
        "description": pick(rng.randint(3, 9)),
        "applies_when": pick(rng.randint(0, 5)),
        "tags": [rng.choice(WORDS) for _ in range(rng.randint(0, 3))],
        "domain": rng.choice(["", "ops", "legal"]),
        "importance": rng.randint(1, 5),
        "uses": rng.randint(0, 4),
        "status": "active",
    }


def _stamped(lessons):
    tc = lesson_cache.TermCache({p: lesson_cache.field_terms(fm) for p, fm in lessons})
    tc.stamps = {p: (1, i) for i, (p, _fm) in enumerate(lessons)}
    return tc


def _as_dicts(ranked):
    return [r.__dict__ for r in ranked]


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    retrieval._INDEX_CACHE.clear()
    yield
    retrieval._INDEX_CACHE.clear()


@pytest.mark.parametrize("scorer", list(retrieval.LEXICAL_SCORERS))
def test_the_cached_index_ranks_exactly_like_a_fresh_one(scorer):
    rng = random.Random(3)
    lessons = [(f"p{i}", _lesson(rng, i)) for i in range(120)]
    tc = _stamped(lessons)
    rel = {fm["name"]: rng.uniform(-1, 1) for _p, fm in lessons}
    for _ in range(60):
        query = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 6)))
        for kw in ({}, {"reliability_lookup": rel, "reliability_weight": 0.3}):
            fresh = retrieval.rank_lessons(query, lessons, top_k=15, scorer=scorer, **kw)
            cached = retrieval.rank_lessons(query, lessons, top_k=15, scorer=scorer,
                                            term_cache=tc, **kw)
            assert _as_dicts(cached) == _as_dicts(fresh), query
    assert retrieval._INDEX_CACHE, "the stamped path should have been cached"


def test_top_k_is_the_head_of_the_full_ranking():
    """Selecting the top k must not reorder ties differently from sorting
    everything and slicing."""
    rng = random.Random(5)
    lessons = [(f"p{i}", _lesson(rng, i)) for i in range(200)]
    for _ in range(40):
        query = " ".join(rng.choice(WORDS) for _ in range(3))
        everything = retrieval.rank_lessons(query, lessons, top_k=10_000, floor=0.0)
        for k in (1, 3, 10):
            head = retrieval.rank_lessons(query, lessons, top_k=k, floor=0.0)
            assert _as_dicts(head) == _as_dicts(everything[:k])


def test_a_changed_stamp_is_a_different_corpus():
    rng = random.Random(9)
    lessons = [(f"p{i}", _lesson(rng, i)) for i in range(30)]
    tc = _stamped(lessons)
    retrieval.rank_lessons("retry backoff", lessons, term_cache=tc)
    edited = list(lessons)
    edited[0] = ("p0", {**lessons[0][1], "description": "retry backoff retry backoff"})
    tc2 = _stamped(edited)
    tc2.stamps["p0"] = (2, 0)
    tc2["p0"] = lesson_cache.field_terms(edited[0][1])
    got = retrieval.rank_lessons("retry backoff", edited, term_cache=tc2)
    want = retrieval.rank_lessons("retry backoff", edited)
    assert _as_dicts(got) == _as_dicts(want)


def test_a_filtered_view_does_not_reuse_the_full_stores_index():
    """`exclude_shown` hands rank_lessons a subset of the snapshot with the
    same term cache. Its IDF is the subset's, and excluded lessons must not
    come back."""
    rng = random.Random(11)
    lessons = [(f"p{i}", _lesson(rng, i)) for i in range(60)]
    tc = _stamped(lessons)
    tc.lessons = lessons
    tc.fingerprint = tuple((p, tc.stamps[p]) for p, _fm in lessons)
    tc.fingerprint_hash = hash(tc.fingerprint)
    retrieval.rank_lessons("retry timeout", lessons, term_cache=tc)
    subset = lessons[::2]
    got = retrieval.rank_lessons("retry timeout", subset, top_k=50, term_cache=tc)
    assert _as_dicts(got) == _as_dicts(retrieval.rank_lessons("retry timeout", subset, top_k=50))
    assert {r.path for r in got} <= {p for p, _fm in subset}


# --- the lesson cache ----------------------------------------------------------

def _store(tmp_path, n=12):
    root = str(tmp_path)
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    rng = random.Random(1)
    for i in range(n):
        fm = _lesson(rng, i)
        lesson_io.write_lesson(os.path.join(ldir, f"lesson_lesson-{i}.md"), fm, "body",
                               root=root, actor="test", reason="fixture")
    return root


def test_an_unchanged_store_is_the_same_snapshot(tmp_path):
    root = _store(tmp_path)
    a_lessons, a_terms = lesson_cache.load_active_with_terms(root)
    b_lessons, b_terms = lesson_cache.load_active_with_terms(root)
    assert a_lessons is b_lessons and a_terms is b_terms


def test_an_edited_lesson_is_seen_on_the_next_call(tmp_path):
    root = _store(tmp_path)
    lessons, terms = lesson_cache.load_active_with_terms(root)
    retrieval.rank_lessons("jurisdiction", lessons, term_cache=terms)
    path = os.path.join(paths.lessons_dir(root), "lesson_lesson-3.md")
    fm = dict(lessons[[p for p, _ in lessons].index(path)][1])
    fm["description"] = "zebra crossing zebra"
    lesson_io.write_lesson(path, {**fm, "status": "active"}, "body", root=root,
                           actor="test", reason="edit")
    lessons2, terms2 = lesson_cache.load_active_with_terms(root)
    assert lessons2 is not lessons
    ranked = retrieval.rank_lessons("zebra", lessons2, term_cache=terms2)
    assert [r.slug for r in ranked] == ["lesson-3"]


def test_a_new_and_a_deleted_lesson_are_seen(tmp_path):
    root = _store(tmp_path)
    lesson_cache.load_active_with_terms(root)
    ldir = paths.lessons_dir(root)
    os.unlink(os.path.join(ldir, "lesson_lesson-0.md"))
    lesson_io.write_lesson(os.path.join(ldir, "lesson_new-one.md"),
                           {"name": "new-one", "description": "quasar", "status": "active"},
                           "body", root=root, actor="test", reason="new")
    lessons, terms = lesson_cache.load_active_with_terms(root)
    slugs = {fm["name"] for _p, fm in lessons}
    assert "new-one" in slugs and "lesson-0" not in slugs
    assert [r.slug for r in retrieval.rank_lessons("quasar", lessons, term_cache=terms)] == ["new-one"]


def test_a_cache_file_rewritten_by_another_process_is_reread(tmp_path):
    """The in-memory copy is keyed on the cache file's identity; a rewrite
    by someone else (a new inode) must not be served from memory."""
    root = _store(tmp_path)
    lesson_cache.load_active_with_terms(root)
    cpath = lesson_cache.cache_path(root)
    raw = json.load(open(cpath, encoding="utf-8"))
    # Corrupt one entry's terms in a replaced file: a reader that trusted the
    # memo would never notice; one that re-reads repairs it from `fm`.
    some = next(iter(raw["entries"]))
    raw["entries"][some]["terms"] = "not-a-list"
    tmp = cpath + ".other"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    os.replace(tmp, cpath)
    lessons, terms = lesson_cache.load_active_with_terms(root)
    assert isinstance(terms[some], list) and len(terms[some]) == 4


def test_ties_keep_corpus_order():
    """Equal relevance, score, importance and uses: the lesson earlier in
    the store comes first, as a stable sort of the whole ranking put it --
    so which of two identical lessons fills the last slot never changes."""
    fm = {"description": "retry backoff", "importance": 3, "uses": 1, "status": "active"}
    lessons = [(f"p{i}", {**fm, "name": f"twin-{i}"}) for i in range(6)]
    for k in (1, 3, 6):
        got = [r.slug for r in retrieval.rank_lessons("retry backoff", lessons, top_k=k)]
        assert got == [f"twin-{i}" for i in range(k)]


def test_a_cache_file_another_process_left_incomplete_is_rewritten(tmp_path):
    """The in-memory copy is only trusted while the cache FILE is the one it
    came from. If another process replaced it with one missing an entry, the
    next call must write the complete cache back, not assume the file still
    matches memory."""
    root = _store(tmp_path)
    lesson_cache.load_active_with_terms(root)
    cpath = lesson_cache.cache_path(root)
    raw = json.load(open(cpath, encoding="utf-8"))
    dropped = next(iter(raw["entries"]))
    del raw["entries"][dropped]
    tmp = cpath + ".other"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    os.replace(tmp, cpath)
    lesson_cache.load_active_with_terms(root)
    assert dropped in json.load(open(cpath, encoding="utf-8"))["entries"]
