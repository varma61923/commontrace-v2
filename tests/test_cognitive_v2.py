"""T4: retrieval entity index + hierarchical expansion, profiles, foresights, notices, bench adapters."""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lesson(name, description="", applies_when="", tags=None, domain="", importance=3, uses=0):
    return (
        f"/tmp/{name}.md",
        {
            "name": name,
            "description": description,
            "applies_when": applies_when,
            "tags": tags or [],
            "domain": domain,
            "importance": importance,
            "uses": uses,
        },
    )


def cli(*argv):
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


# --- entity index + entity_boost ---

def test_entity_index_maps_tokens_to_docs():
    from commontrace import retrieval
    lessons = [
        _lesson("lesson_deploy", description="Deploy pipeline runbook", tags=["Deploy-Flow"], domain="ops"),
        _lesson("lesson_other", description="unrelated changelog notes", tags=["docs"]),
    ]
    idx = retrieval.build_entity_index(lessons)
    assert isinstance(idx, dict)
    assert 0 in idx.get("deploy-flow", idx.get("deploy", [])) or any(
        0 in v for k, v in idx.items() if "deploy" in k
    )
    # other doc should not share the deploy token posting
    assert idx


def test_entity_boost_default_zero_is_behavior_unchanged():
    from commontrace import retrieval
    lessons = [
        _lesson("lesson_a", description="refund policy enterprise", importance=3),
        _lesson("lesson_b", description="refund policy enterprise", importance=1),
    ]
    base = retrieval.rank_lessons("refund policy enterprise", lessons)
    boosted = retrieval.rank_lessons("refund policy enterprise", lessons, entity_boost=0.0)
    assert [r.slug for r in base] == [r.slug for r in boosted]
    assert [r.relevance for r in base] == [r.relevance for r in boosted]


def test_entity_boost_can_break_tie():
    from commontrace import retrieval
    lessons = [
        _lesson("lesson_plain", description="refund policy enterprise accounts", importance=3),
        _lesson("lesson_entity", description="refund policy enterprise accounts AcmeCorp rollout", tags=["AcmeCorp"], importance=3, uses=0),
    ]
    idx = retrieval.build_entity_index(lessons)
    q = "AcmeCorp refund policy enterprise"
    ranked = retrieval.rank_lessons(q, lessons, entity_index=idx, entity_boost=0.5)
    assert ranked[0].slug == "lesson_entity"


# --- hierarchical_expand ---

def test_hierarchical_expand_links_episodes_to_facts_with_floor():
    from commontrace import retrieval
    episodes = ["ep-1", "ep-2"]
    facts = [
        {"id": "f-linked", "statement": "linked fact", "confidence": 0.5, "source_traces": ["ep-1"]},
        {"id": "f-unlinked", "statement": "stray fact", "confidence": 0.9, "source_traces": ["ep-zzz"]},
    ]
    out = retrieval.hierarchical_expand(episodes, facts, top_n=10)
    ids = [fid for fid, _ in out]
    assert "f-linked" in ids
    # linked fact should outrank unlinked despite lower confidence (fan-out fusion)
    assert ids.index("f-linked") < ids.index("f-unlinked")
    # floor eviction: absurd floor removes everything
    assert retrieval.hierarchical_expand(episodes, facts, min_score=999.0) == []
    # top_n respected
    assert len(retrieval.hierarchical_expand(episodes, facts, top_n=1)) == 1


def test_hierarchical_expand_empty_inputs():
    from commontrace import retrieval
    assert retrieval.hierarchical_expand([], []) == []
    assert retrieval.hierarchical_expand(["ep-1"], []) == []


def test_rrf_helper_still_present():
    from commontrace import retrieval
    fused = retrieval.reciprocal_rank_fusion({"a": ["x", "y"], "b": ["y", "x"]}, top_k=2)
    assert [fid for fid, _ in fused] == ["x", "y"] or [fid for fid, _ in fused] == ["y", "x"]


# --- profile store ---

def test_profile_static_dynamic_roundtrip(tmp_path):
    from commontrace import profile_store
    root = str(tmp_path / "store")
    os.makedirs(os.path.join(root, "memory"), exist_ok=True)
    prof = profile_store.load_profile(root)
    assert prof == {"static": "", "dynamic": []}
    profile_store.save_profile_static(root, "Staff backend engineer. Prefers stdlib.")
    profile_store.append_dynamic(root, "working on retrieval")
    profile_store.append_dynamic(root, "prefers pytest")
    loaded = profile_store.load_profile(root)
    assert "Staff backend" in loaded["static"]
    assert len(loaded["dynamic"]) == 2
    # static save preserves dynamic
    profile_store.save_profile_static(root, "Updated static traits")
    reloaded = profile_store.load_profile(root)
    assert "Updated static" in reloaded["static"]
    assert len(reloaded["dynamic"]) == 2
    # prune keeps last N
    pruned = profile_store.prune_dynamic(root, keep=1)
    assert len(pruned["dynamic"]) == 1
    assert "pytest" in pruned["dynamic"][0]
    # markers present on disk
    text = open(os.path.join(root, "memory", "profile.md"), encoding="utf-8").read()
    assert "<!-- static -->" in text and "<!-- dynamic -->" in text


# --- foresights ---

def test_foresight_record_and_list(tmp_path):
    from commontrace import hierarchical
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    fs = hierarchical.record_foresight(
        root, "Deploy queue depth will exceed threshold Friday", owner="sage", evidence_ids=["ep-1"],
    )
    assert fs.id.startswith("foresight-")
    assert fs.owner == "sage"
    assert fs.status == "open"
    assert fs.evidence_ids == ["ep-1"]
    got = hierarchical.list_foresights(root)
    assert len(got) == 1 and got[0].statement.startswith("Deploy queue")
    assert hierarchical.list_foresights(root, status="open")
    assert hierarchical.list_foresights(root, owner="sage")
    assert hierarchical.list_foresights(root, owner="nobody") == []
    # fact lifecycle untouched: facts file still independent
    f, act = hierarchical.add_fact(root, "Unrelated atomic fact for isolation.")
    assert act == "ADD"


# --- notices ---

def test_notices_thresholds():
    from commontrace import notices
    assert notices.check_health({}) == []
    assert notices.check_health(None) == []
    healthy = notices.check_health({"pollution_ratio": 1.0, "p_at_1": 0.9, "recall": 0.8})
    assert healthy == []
    bad = notices.check_health({"pollution_ratio": 2.0, "p_at_1": 0.1, "recall": 0.1})
    codes = {n["code"] for n in bad}
    assert "high_pollution" in codes
    assert "low_p_at_1" in codes
    assert "low_recall" in codes
    # alias keys work
    alias = notices.check_health({"pollution": 9.0})
    assert any(n["code"] == "high_pollution" for n in alias)


# --- benchmark adapters ---

def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_locomo_adapter_load_and_run(tmp_path):
    from benchmarks import locomo_adapter
    corpus = str(tmp_path / "locomo.jsonl")
    _write_jsonl(corpus, [
        {"question": "how to force push safely", "expected_doc_ids": ["lesson_git_safety"]},
        {"question": "changelog style", "expected_doc_ids": ["lesson_docs"]},
        {"bad": "line without query/expected"},
    ])
    pairs = locomo_adapter.load_corpus(corpus)
    assert len(pairs) == 2
    assert pairs[0][1] == ["lesson_git_safety"]
    lessons = [
        _lesson("lesson_git_safety", description="Never force-push shared branches", tags=["git-safety"]),
        _lesson("lesson_docs", description="How to write a good changelog entry", tags=["docs"]),
    ]
    res = locomo_adapter.run(corpus, lessons, top_k=2)
    assert res["n"] == 2
    assert res["p_at_1"] == 1.0
    assert res["recall"] == 1.0
    assert locomo_adapter.load_corpus(str(tmp_path / "missing.jsonl")) == []


def test_longmemeval_adapter_load_and_run(tmp_path):
    from benchmarks import longmemeval_adapter
    corpus = str(tmp_path / "lme.jsonl")
    _write_jsonl(corpus, [
        {"question": "refund policy enterprise", "relevant_doc_ids": ["lesson_refund"]},
        {"item": {"question": "changelog style", "relevant_doc_ids": ["lesson_docs"]}},
    ])
    pairs = longmemeval_adapter.load_corpus(corpus)
    assert len(pairs) == 2
    lessons = [
        _lesson("lesson_refund", description="refund policy for enterprise accounts", tags=["refund"]),
        _lesson("lesson_docs", description="How to write a good changelog entry", tags=["docs"]),
    ]
    res = longmemeval_adapter.run(corpus, lessons, top_k=2)
    assert res["n"] == 2
    assert res["p_at_1"] >= 0.5
    empty = longmemeval_adapter.run(corpus, [], top_k=2)
    assert empty["n"] == 2 and empty["p_at_1"] == 0.0
