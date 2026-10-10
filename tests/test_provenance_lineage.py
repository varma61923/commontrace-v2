"""Provenance lineage: derivation graph across provenance, facts, observations, proposals and lessons."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from commontrace import _jsonl, hierarchical, memory_control, provenance

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def root(tmp_path):
    os.makedirs(tmp_path / "memory")
    return str(tmp_path)


def _chain(root):
    provenance.append_provenance(root, "chunk", "chunk-1", source_path="docs/guide.md", run_id="r1")
    fact, _ = hierarchical.add_fact(root, "The guide says retries use backoff", source_trace_id="chunk-1")
    other, _ = hierarchical.add_fact(root, "Retries use exponential backoff", source_trace_id="chunk-1")
    proposal = memory_control.put(root, "proposal", "Retries back off exponentially",
                                  data={"status": "review", "sources": [fact.id, other.id]})
    _jsonl.append_row(os.path.join(root, "memory", "observations", "observations.jsonl"),
                      {"id": "obs-1", "statement": fact.statement, "source_fact_ids": [fact.id]})
    return fact, other, proposal


def test_down_lineage_follows_every_derivation_source(root):
    fact, other, proposal = _chain(root)
    result = provenance.lineage(root, "docs/guide.md", direction="down")
    depth = {n["id"]: n["depth"] for n in result["nodes"]}
    assert depth["docs/guide.md"] == 0 and depth["chunk-1"] == 1
    assert depth[fact.id] == 2 and depth[other.id] == 2
    assert depth[proposal["id"]] == 3 and depth["obs-1"] == 3
    relations = {(e["source"], e["target"]): e["relation"] for e in result["edges"]}
    assert relations[("docs/guide.md", "chunk-1")] == "provenance:chunk"
    assert relations[(fact.id, proposal["id"])] == "control:proposal"
    assert relations[(fact.id, "obs-1")] == "observation_of"
    assert {n["kind"] for n in result["nodes"] if n["id"] == fact.id} == {"fact"}
    assert not result["truncated"]


def test_up_lineage_and_depth_bound(root):
    fact, _other, proposal = _chain(root)
    up = provenance.lineage(root, proposal["id"], direction="up")
    ids = {n["id"] for n in up["nodes"]}
    assert {fact.id, "chunk-1", "docs/guide.md", "run:r1"} <= ids
    short = provenance.lineage(root, proposal["id"], direction="up", max_depth=1)
    assert {n["id"] for n in short["nodes"]} == {proposal["id"], fact.id, _other.id}
    assert short["truncated"] is True
    # Case-insensitive fallback, and unknown ids are reported, not raised.
    assert provenance.lineage(root, "DOCS/GUIDE.MD")["root"] == "docs/guide.md"
    assert provenance.lineage(root, "nothing-here")["found"] is False
    with pytest.raises(ValueError):
        provenance.lineage(root, "x", direction="sideways")
    with pytest.raises(ValueError):
        provenance.lineage(root, "x", max_depth=0)


def test_cycles_terminate_and_superseded_facts_link(root):
    provenance.append_provenance(root, "node", "a", source_path="b")
    provenance.append_provenance(root, "node", "b", source_path="a")
    result = provenance.lineage(root, "a", max_depth=32)
    assert {n["id"] for n in result["nodes"]} == {"a", "b"} and result["revisited"] == 1
    old, _ = hierarchical.add_fact(root, "Office is in Tokyo")
    new = hierarchical.supersede_fact(root, old.id, "Office is in Osaka")
    new_id = next(f.id for f in new if f.id != old.id)
    down = provenance.lineage(root, old.id)
    assert any(e["relation"] == "superseded_by" and e["target"] == new_id for e in down["edges"])


def test_lesson_sources_and_node_cap(root):
    lessons = os.path.join(root, "memory", "lessons")
    os.makedirs(lessons)
    with open(os.path.join(lessons, "lesson_retry.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: retry\nsource_traces: [trace-9]\n---\n\n## Rule\nRetry.\n")
    for i in range(10):
        provenance.append_provenance(root, "chunk", f"c{i}", source_path="trace-9")
    result = provenance.lineage(root, "trace-9", max_nodes=4)
    assert len(result["nodes"]) == 4 and result["truncated"]
    assert "lesson:retry" in {n["id"] for n in provenance.lineage(root, "trace-9")["nodes"]}


def test_cli_lineage(root):
    fact, _other, proposal = _chain(root)

    def cli(*argv):
        return subprocess.run([sys.executable, "-m", "commontrace.cli", "graph", "provenance", *argv,
                               "--dest", root], capture_output=True, text=True, cwd=REPO, check=False)

    res = cli("--lineage", proposal["id"], "--up", "--json")
    assert res.returncode == 0, res.stderr
    assert fact.id in {n["id"] for n in json.loads(res.stdout)["nodes"]}
    res = cli("--lineage", "docs/guide.md")
    assert res.returncode == 0 and "chunk-1" in res.stdout and "provenance:chunk" in res.stdout
    assert "source=docs/guide.md" in cli("chunk-1").stdout  # the flat view still works
    assert cli().returncode == 2
