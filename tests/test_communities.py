from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
import yaml

from commontrace import communities, entity_store, hierarchical

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _write_lesson(root, slug, description, tags=None, source_traces=None):
    fm = {
        "name": slug,
        "description": description,
        "tags": tags or ["testing"],
        "agent_type": "code",
        "domain": "testing",
        "importance": 3,
        "applies_when": "when testing",
        "do_not_apply_when": "when not testing",
        "uses": 0,
        "last_hit": "NEVER",
        "source_traces": source_traces or [],
        "source_episodes": [],
        "status": "active",
    }
    body = "## Rule\nx\n\n## Why\ny\n\n## How to apply\nz\n\n## Counter-examples\nn/a\n"
    path = os.path.join(root, "memory", "lessons", f"lesson_{slug}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body)
    return path


def _write_trace(root, trace_id, date="2026-09-20"):
    fm = {"id": trace_id, "title": f"trace {trace_id}", "agent_type": "code", "tags": ["t"]}
    body = "## Context\nc\n\n## Solution\ns\n"
    safe = trace_id.replace("/", "-")[:16]
    path = os.path.join(root, "memory", "traces", f"{date}_trace_{safe}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body)
    return path


def _seed_two_clusters(root):
    _write_trace(root, "trace-alpha-1")
    _write_trace(root, "trace-beta-1")
    _write_lesson(root, "alpha-one", "Alpha cluster lesson one", tags=["alpha"], source_traces=["trace-alpha-1"])
    _write_lesson(root, "alpha-two", "Alpha cluster lesson two", tags=["alpha"], source_traces=["trace-alpha-1"])
    _write_lesson(root, "beta-one", "Beta cluster lesson one", tags=["beta"], source_traces=["trace-beta-1"])
    _write_lesson(root, "beta-two", "Beta cluster lesson two", tags=["beta"], source_traces=["trace-beta-1"])


def _group_of(groups, member):
    for name, members in groups.items():
        if member in members:
            return name
    return None


def test_build_deterministic_and_clustered(store):
    _seed_two_clusters(store)
    first = communities.build_communities(store)
    second = communities.build_communities(store)
    assert first == second
    assert _group_of(first, "alpha-one") == _group_of(first, "alpha-two")
    assert _group_of(first, "beta-one") == _group_of(first, "beta-two")
    assert _group_of(first, "alpha-one") != _group_of(first, "beta-one")
    # each community written as one markdown file with frontmatter + members
    stored = communities.list_communities(store)
    assert {row["name"] for row in stored} == set(first)
    for row in stored:
        assert row["size"] == len(row["members"])
        assert row["summary"].startswith("Top terms:")
        assert row["top_terms"]


def test_facts_join_via_shared_trace(store):
    _seed_two_clusters(store)
    fact, _ = hierarchical.add_fact(store, "Alpha cluster fact via shared trace.", source_trace_id="trace-alpha-1")
    groups = communities.build_communities(store)
    assert _group_of(groups, fact.id) == _group_of(groups, "alpha-one")


def test_entity_store_links_form_edges(store):
    f1, _ = hierarchical.add_fact(store, "CheckoutFlow retries payments twice.")
    f2, _ = hierarchical.add_fact(store, "CheckoutFlow timeouts surface as 502.")
    entity_store.rebuild_entity_index(store)
    groups = communities.build_communities(store)
    assert _group_of(groups, f1.id) == _group_of(groups, f2.id) == _group_of(groups, f2.id)
    assert set(groups[_group_of(groups, f1.id)]) >= {f1.id, f2.id}


def test_update_community_for_lesson_is_incremental(store):
    _seed_two_clusters(store)
    communities.build_communities(store)
    _write_lesson(store, "alpha-three", "Alpha cluster lesson three", tags=["alpha"],
                  source_traces=["trace-alpha-1"])
    name = communities.update_community_for_lesson(store, "lesson_alpha-three")
    assert name == _group_of(communities.build_communities(store), "alpha-one")
    members = communities.get_community(store, name)["members"]
    assert "alpha-three" in members
    assert communities.update_community_for_lesson(store, "no-such-lesson") is None


def test_get_community_lookup_and_missing(store):
    _seed_two_clusters(store)
    groups = communities.build_communities(store)
    name = sorted(groups)[0]
    found = communities.get_community(store, name)
    assert found is not None
    assert found["members"] == groups[name]
    assert communities.get_community(store, "does-not-exist") is None


def test_stale_files_pruned(store):
    _seed_two_clusters(store)
    communities.build_communities(store)
    # remove every lesson+fact edge target by deleting lessons, then rebuild
    for path in os.listdir(os.path.join(store, "memory", "lessons")):
        if path.startswith("lesson_") and path != "lesson_template.md":
            os.unlink(os.path.join(store, "memory", "lessons", path))
    communities.build_communities(store)
    assert communities.list_communities(store) == []


def _payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def test_mcp_community_members_round_trip(store):
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    from commontrace import mcp_server

    _seed_two_clusters(store)
    groups = communities.build_communities(store)
    server = mcp_server.build_server(store)
    name = _group_of(groups, "alpha-one")
    out = asyncio.run(server.call_tool("community_members", {"name": name}))
    payload = _payload(out)
    assert payload["ok"] is True
    assert set(payload["members"]) == set(groups[name])
    assert payload["size"] == len(groups[name])


def test_mcp_trace_lesson_round_trip(store):
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    from commontrace import mcp_server

    _seed_two_clusters(store)
    server = mcp_server.build_server(store)
    out = asyncio.run(server.call_tool("lessons_from_trace", {"trace_id": "trace-alpha-1"}))
    payload = _payload(out)
    assert payload["ok"] is True
    assert payload["lessons"] == ["alpha-one", "alpha-two"]
    out = asyncio.run(server.call_tool("traces_for_lesson", {"slug": "lesson_beta-one"}))
    payload = _payload(out)
    assert payload["ok"] is True
    assert payload["traces"] == ["trace-beta-1"]


def test_cli_community_build_list_show(store):
    _seed_two_clusters(store)
    rc = cli("community", "build", "--dest", store).returncode
    assert rc == 0
    listed = cli("community", "list", "--dest", store)
    assert listed.returncode == 0
    assert listed.stdout.strip()
    groups = communities.build_communities(store)
    name = sorted(groups)[0]
    shown = cli("community", "show", name, "--dest", store)
    assert shown.returncode == 0
    assert name in shown.stdout
