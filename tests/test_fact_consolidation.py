"""Fact-cluster consolidation: lexical and embedding links, canonical choice, review-only proposals."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from commontrace import fact_consolidation as fc
from commontrace import hierarchical, memory_control

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def root(tmp_path, monkeypatch):
    # Write-time dedup (hierarchical, COMMONTRACE_FACT_DEDUP=near) already merges
    # very close statements. Exact mode models a store written before it existed,
    # or by a path that skips it: the duplicates consolidation is there to find.
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "exact")
    os.makedirs(tmp_path / "memory")
    return str(tmp_path)


def _add(root, statement, *, scopes=None, trace="", times=1):
    fact = None
    for n in range(times):
        fact, _ = hierarchical.add_fact(root, statement, scopes=scopes, source_trace_id=f"{trace}-{n}" if trace else "")
    return fact


class FakeEmbedder:
    """Maps statements onto fixed vectors; anything mentioning a cat is the same direction."""

    class spec:  # noqa: N801 - mirrors embeddings.Spec's attribute
        tag = "fake:paraphrase"

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, query):
        assert query is False
        self.calls.append(list(texts))
        out = []
        for text in texts:
            lower = text.lower()
            if "cat" in lower or "feline" in lower:
                out.append([1.0, 0.0, 0.0])
            elif "deploy" in lower:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


def test_paraphrases_cluster_within_scope_only(root):
    a = _add(root, "The user lives in Paris, France.", trace="t1", times=2)
    b = _add(root, "User lives in Paris", trace="t2")
    _add(root, "The user lives in Paris.", scopes=["project:x"])  # other scope: never merged
    _add(root, "Deploys run on Fridays.")
    report = fc.cluster_facts(root)
    assert report["facts"] == 4 and len(report["clusters"]) == 1
    cluster = report["clusters"][0]
    assert set(cluster["member_ids"]) == {a.id, b.id} and cluster["scopes"] == []
    assert cluster["canonical"] == a.id  # most confirmations wins
    assert {e["source_id"] for e in cluster["evidence"]} >= {"t1-0", "t1-1", "t2-0"}
    assert cluster["links"][0]["jaccard"] >= fc.DEFAULT_THRESHOLD
    assert "Paris" in cluster["entities"] and cluster["summary_method"] == "extractive"
    assert cluster["id"].startswith(fc.CLUSTER_PREFIX)
    assert report == fc.cluster_facts(root)  # deterministic


def test_finds_paraphrases_that_write_time_dedup_keeps_apart(root, monkeypatch):
    monkeypatch.setenv("COMMONTRACE_FACT_DEDUP", "near")
    _add(root, "The user lives in Paris, France.")
    _add(root, "User lives in Paris")
    assert len(hierarchical.list_facts(root)) == 2  # below the 0.85 write-time bar
    assert len(fc.cluster_facts(root)["clusters"]) == 1


def test_canonical_tie_breaks_on_recency(root):
    old = hierarchical.add_fact(root, "Office moved to Tokyo", created_at="2024-01-01T00:00:00+00:00")[0]
    new = hierarchical.add_fact(root, "The office moved to Tokyo.", created_at="2025-01-01T00:00:00+00:00")[0]
    report = fc.cluster_facts(root)
    assert report["clusters"][0]["canonical"] in {old.id, new.id}
    facts = hierarchical.list_facts(root)
    expected = max(facts, key=lambda f: (f.confirmations, f.updated_at or f.created_at))
    assert report["clusters"][0]["canonical"] == expected.id


def test_embedding_links_paraphrases_without_shared_words(root):
    _add(root, "Alice owns a cat named Tom")
    _add(root, "Her feline companion is Tom")
    _add(root, "Deploys run on Fridays")
    assert fc.cluster_facts(root)["clusters"] == []
    fake = FakeEmbedder()
    report = fc.cluster_facts(root, embedder=fake)
    assert report["embedder"] == "fake:paraphrase" and len(report["clusters"]) == 1
    assert report["clusters"][0]["links"][0]["cosine"] == pytest.approx(1.0)
    assert len(fake.calls) == 1


def test_lsh_path_for_large_scopes(root):
    items = [{"statement": f"Service {i} uses port {8000 + i} in region zone{i}"} for i in range(fc.LSH_MIN_ITEMS)]
    items.append({"statement": "Service 7 uses port 8007 in region zone7 today"})
    hierarchical.add_facts(root, items)
    report = fc.cluster_facts(root, threshold=0.8)
    assert len(report["clusters"]) == 1 and report["clusters"][0]["size"] == 2


def test_model_summary_and_fallback(root):
    _add(root, "The user lives in Paris, France.")
    _add(root, "User lives in Paris")
    prompts = []

    def complete(prompt):
        prompts.append(prompt)
        return "The user lives in Paris.", {}

    report = fc.cluster_facts(root, summarize="model", complete=complete)
    assert report["clusters"][0]["summary"] == "The user lives in Paris." and "- User lives in Paris" in prompts[0]

    def broken(prompt):
        raise RuntimeError("no model configured")

    report = fc.cluster_facts(root, summarize="model", complete=broken)
    assert report["clusters"][0]["summary_method"] == "extractive" and report["notes"]


def test_apply_writes_review_proposals_and_never_touches_facts(root):
    _add(root, "The user lives in Paris, France.")
    _add(root, "User lives in Paris")
    before = {f.id: f.to_dict() for f in hierarchical.list_facts(root)}
    report = fc.cluster_facts(root)
    applied = fc.apply_clusters(root, report)
    assert len(applied["written"]) == 1
    proposal = memory_control.records(root, "proposal")[0]
    assert proposal["id"] == report["clusters"][0]["id"] and proposal["data"]["status"] == "review"
    assert sorted(proposal["data"]["sources"]) == sorted(before)
    assert proposal["data"]["kind"] == "fact-cluster"
    assert {f.id: f.to_dict() for f in hierarchical.list_facts(root)} == before
    assert fc.apply_clusters(root, report)["unchanged"] == [proposal["id"]]
    memory_control.reject_proposal(root, proposal["id"], proposal["revision"], "not the same")
    assert fc.apply_clusters(root, report)["skipped_rejected"] == [proposal["id"]]


def test_cli_report_and_apply(root):
    _add(root, "The user lives in Paris, France.")
    _add(root, "User lives in Paris")

    def cli(*argv):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "consolidate", *argv, "--dest", root],
                              capture_output=True, text=True, cwd=REPO, check=False)

    res = cli("facts", "--json")
    assert res.returncode == 0, res.stderr
    assert len(json.loads(res.stdout)["clusters"]) == 1
    assert memory_control.records(root, "proposal") == []
    res = cli("facts", "--apply", "--threshold", "0.5")
    assert res.returncode == 0, res.stderr
    assert "status=review" in res.stdout
    assert len(memory_control.records(root, "proposal")) == 1
    assert cli("facts", "--threshold", "0").returncode == 2
