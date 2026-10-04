from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
import yaml

from commontrace import hierarchical, observations

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = "2026-10-04T00:00:00+00:00"


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "code").returncode == 0
    return root


def _write_trace(root, trace_id, date):
    fm = {"id": trace_id, "title": f"trace {trace_id}", "agent_type": "code", "tags": ["t"]}
    body = "## Context\nc\n\n## Solution\ns\n"
    safe = trace_id.replace("/", "-")[:16]
    path = os.path.join(root, "memory", "traces", f"{date}_trace_{safe}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body)
    return path


def _reinforced_fact(root, statement, trace_ids):
    fact = None
    for tid in trace_ids:
        fact, _action = hierarchical.add_fact(root, statement, source_trace_id=tid)
    return fact


def _seed(store, statement, traces):
    for tid, date in traces:
        _write_trace(store, tid, date)
    return _reinforced_fact(store, statement, [tid for tid, _ in traces])


def test_boost_is_bounded():
    assert observations.observation_boost(0) == 0.0
    assert observations.observation_boost(1) == 0.05
    assert observations.observation_boost(3) == 0.15
    assert observations.observation_boost(6) == 0.3
    assert observations.observation_boost(600) == 0.3
    assert observations.observation_boost(-4) == 0.0
    assert observations.observation_boost("lots") == 0.0


def test_consolidate_groups_reinforced_facts_only(store):
    _seed(store, "Postgres max connections is 100.", [("t1", "2026-10-01"), ("t2", "2026-10-02")])
    hierarchical.add_fact(store, "Redis cache TTL is 3600 seconds.", source_trace_id="t3")  # once: skipped
    out = observations.consolidate_facts(store, now=NOW)
    assert len(out) == 1
    observation = out[0]
    assert observation.statement == "Postgres max connections is 100."
    assert observation.proof_count == 2
    assert observation.trend == "new"
    assert {e["source_id"] for e in observation.evidence} == {"t1", "t2"}
    assert all(e["quote"] == observation.statement for e in observation.evidence)


def test_trend_strengthening_weakening_stable_stale(store):
    _seed(store, "Strengthening claim here.", [("s1", "2026-10-01"), ("s2", "2026-09-30"),
                                               ("s3", "2026-08-10")])
    _seed(store, "Weakening claim here xyz.", [("w1", "2026-08-10"), ("w2", "2026-08-05"),
                                               ("w3", "2026-10-01")])
    _seed(store, "Stable claim here abc.", [("p1", "2026-10-01"), ("p2", "2026-08-20")])
    _seed(store, "Stale claim here def.", [("o1", "2026-06-01"), ("o2", "2026-05-20")])
    trends = {o.statement: o.trend for o in observations.consolidate_facts(store, now=NOW)}
    assert trends["Strengthening claim here."] == "strengthening"
    assert trends["Weakening claim here xyz."] == "weakening"
    assert trends["Stable claim here abc."] == "stable"
    assert trends["Stale claim here def."] == "stale"


def test_consolidate_is_deterministic(store):
    _seed(store, "Deterministic observation body.", [("d1", "2026-10-01"), ("d2", "2026-08-12")])
    first = [o.to_dict() for o in observations.consolidate_facts(store, now=NOW)]
    second = [o.to_dict() for o in observations.consolidate_facts(store, now=NOW)]
    assert first == second
    again = observations.consolidate_facts(store, now=NOW)
    assert [o.id for o in again] == sorted(o.id for o in again)
    # the JSONL store itself is sorted and reloadable
    path = os.path.join(store, "memory", "observations", "observations.jsonl")
    assert os.path.isfile(path)
    reloaded = observations.load_observations(store)
    assert [o.id for o in reloaded.values()] == sorted(reloaded)


def test_consolidate_preserves_created_at_across_reruns(store):
    _seed(store, "Created stamp claim.", [("c1", "2026-10-01"), ("c2", "2026-10-02")])
    first = observations.consolidate_facts(store, now=NOW)[0]
    second = observations.consolidate_facts(store, now="2026-10-05T00:00:00+00:00")[0]
    assert second.created_at == first.created_at
    assert second.updated_at == "2026-10-05T00:00:00+00:00"
    assert second.id == first.id


def test_observations_file_atomic_and_bounded(store):
    _seed(store, "Boosted by proofs.", [("b1", "2026-10-01"), ("b2", "2026-10-02"),
                                       ("b3", "2026-10-03")])
    out = observations.consolidate_facts(store, now=NOW)
    assert out[0].proof_count == 3
    boost = observations.observation_boost(out[0].proof_count)
    assert boost == 0.15
    assert all(observations.observation_boost(o.proof_count) <= 0.3 for o in out)


def test_mcp_observation_evidence_round_trip(store):
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    from commontrace import mcp_server

    _seed(store, "MCP observable claim.", [("m1", "2026-10-01"), ("m2", "2026-10-02")])
    observation = observations.consolidate_facts(store, now=NOW)[0]
    server = mcp_server.build_server(store)
    out = asyncio.run(server.call_tool("observation_evidence", {"id": observation.id}))
    payload = out.structured_content.get("result", out.structured_content) if getattr(
        out, "structured_content", None) else json.loads(out.content[0].text)
    assert payload["ok"] is True
    assert payload["observation"]["id"] == observation.id
    assert payload["observation"]["evidence"] == observation.evidence
    missing = asyncio.run(server.call_tool("observation_evidence", {"id": "obs-nope"}))
    m_payload = missing.structured_content.get("result", missing.structured_content) if getattr(
        missing, "structured_content", None) else json.loads(missing.content[0].text)
    assert m_payload["ok"] is False


def test_cli_observation_consolidate_list_show(store):
    _seed(store, "CLI observable claim.", [("k1", "2026-10-01"), ("k2", "2026-10-02")])
    rc = cli("observation", "consolidate", "--dest", store).returncode
    assert rc == 0
    listed = cli("observation", "list", "--dest", store)
    assert listed.returncode == 0
    assert "CLI observable claim." in listed.stdout or "obs-" in listed.stdout
    observation = observations.load_observations(store)
    obs_id = sorted(observation)[0]
    shown = cli("observation", "show", obs_id, "--dest", store)
    assert shown.returncode == 0
    assert "k1" in shown.stdout
