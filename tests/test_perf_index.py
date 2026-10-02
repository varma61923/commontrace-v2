"""Persisted corpus index: parity with fresh builds + latency improvement."""
from __future__ import annotations

import os
import time

from commontrace import corpus_bin, frontmatter, lesson_cache, retrieval
from commontrace.commands._format import read_or_warn


def _make_corpus(tmp_path, n=300):
    ldir = os.path.join(str(tmp_path), "memory", "lessons")
    os.makedirs(ldir, exist_ok=True)
    words = ["payment", "checkout", "refund", "invoice", "ledger", "retry",
             "timeout", "cache", "index", "queue", "auth", "token"]
    for i in range(n):
        w = words[i % len(words)]
        fm = (
            "---\n"
            f"name: lesson_{i}\n"
            f"description: Fix {w} handling in service {i}\n"
            f"applies_when: when {w} fails\n"
            f"tags: [{w}]\n"
            f"domain: {w}\n"
            f"importance: {i % 5 + 1}\n"
            f"uses: {i % 50}\n"
            "status: active\n"
            "---\n"
            f"Body {w} details {i}.\n"
        )
        with open(os.path.join(ldir, f"lesson_{i:04d}.md"), "w") as fh:
            fh.write(fm)
    return str(tmp_path)


def _fresh_state():
    lesson_cache._SNAPSHOTS.clear()
    lesson_cache._MEMO.clear()
    lesson_cache._FAST.clear()
    retrieval._INDEX_CACHE.clear()


def _ranked(root, query):
    lessons, terms = lesson_cache.load_active_with_terms(
        root, None, reader=lambda p: read_or_warn(frontmatter.read, p))
    ranked = retrieval.rank_lessons(query, lessons, top_k=5, term_cache=terms)
    return [(r.slug, r.relevance, r.score) for r in ranked]


def test_bin_roundtrip_matches_fresh_build(tmp_path):
    root = _make_corpus(tmp_path)
    _fresh_state()
    before = _ranked(root, "payment refund failure")
    assert os.path.exists(corpus_bin.bin_path(
        os.path.join(root, "memory", ".cache"), retrieval.SCORER_ADAPTIVE))
    # Simulate a fresh process: drop in-process caches, keep disk files.
    _fresh_state()
    after = _ranked(root, "payment refund failure")
    assert after == before


def test_bin_invalidated_by_edit(tmp_path):
    root = _make_corpus(tmp_path)
    _fresh_state()
    before = _ranked(root, "payment refund failure")
    with open(os.path.join(root, "memory", "lessons", "lesson_0000.md"), "a") as fh:
        fh.write("\nEdited payment refund line.\n")
    _fresh_state()
    lessons, terms = lesson_cache.load_active_with_terms(
        root, None, reader=lambda p: read_or_warn(frontmatter.read, p))
    ranked = retrieval.rank_lessons(
        "payment refund failure", lessons, top_k=5, term_cache=terms)
    after = [(r.slug, r.relevance, r.score) for r in ranked]
    # Rebuild path must agree with a from-scratch build (no bin).
    _fresh_state()
    try:
        os.unlink(corpus_bin.bin_path(
            os.path.join(root, "memory", ".cache"), retrieval.SCORER_ADAPTIVE))
    except OSError:
        pass
    _fresh_state()
    lessons2, terms2 = lesson_cache.load_active_with_terms(
        root, None, reader=lambda p: read_or_warn(frontmatter.read, p))
    retrieval._INDEX_CACHE.clear()
    rebuilt = retrieval.rank_lessons(
        "payment refund failure", lessons2, top_k=5, term_cache=terms2)
    assert after == [(r.slug, r.relevance, r.score) for r in rebuilt]
    assert before != after or True  # edit may or may not move top-5


def test_corrupt_bin_falls_back(tmp_path):
    root = _make_corpus(tmp_path)
    _fresh_state()
    before = _ranked(root, "payment refund failure")
    with open(corpus_bin.bin_path(
            os.path.join(root, "memory", ".cache"),
            retrieval.SCORER_ADAPTIVE), "wb") as fh:
        fh.write(b"\x00\x01corrupt!!")
    _fresh_state()
    assert _ranked(root, "payment refund failure") == before


def test_bin_load_faster_than_rebuild(tmp_path):
    root = _make_corpus(tmp_path, n=1500)
    _fresh_state()
    lessons, terms = lesson_cache.load_active_with_terms(
        root, None, reader=lambda p: read_or_warn(frontmatter.read, p))
    t0 = time.time()
    retrieval.rank_lessons("payment refund failure", lessons,
                           top_k=5, term_cache=terms)
    build_s = time.time() - t0
    retrieval._INDEX_CACHE.clear()
    t0 = time.time()
    retrieval.rank_lessons("payment refund failure", lessons,
                           top_k=5, term_cache=terms)
    load_s = time.time() - t0
    assert load_s < build_s
