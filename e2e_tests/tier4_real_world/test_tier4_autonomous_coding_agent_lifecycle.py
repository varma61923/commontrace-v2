from __future__ import annotations

from commontrace import graph, hierarchical, memory_blocks


def test_t4_autonomous_coding_agent_lifecycle(isolated_store: str):
    memory_blocks.set_block(
        isolated_store,
        "persona",
        "Staff Backend Engineer specializing in resilient distributed systems.",
        actor="orchestrator",
        reason="agent initialization",
    )
    memory_blocks.set_block(
        isolated_store,
        "human",
        "User requires strict typing, 100% test pass rate, and zero data loss.",
        actor="user",
        reason="user preferences",
    )
    memory_blocks.set_block(
        isolated_store,
        "project",
        "Current sprint: Migrate user authentication service to OAuth2/OIDC.",
        actor="agent-01",
        reason="sprint kickoff",
    )

    hierarchical.add_fact(
        isolated_store,
        "JWT tokens must expire within 15 minutes and use RS256 signature algorithm.",
        category="constraint",
        scopes=["auth", "security"],
        confidence=1.0,
    )

    graph.add_node(isolated_store, "service:auth", entity_type="service", name="Auth Service")
    graph.add_node(isolated_store, "service:redis", entity_type="service", name="Redis Session Cache")
    graph.add_edge(isolated_store, "service:auth", "service:redis", "uses", weight=1.0)

    neighbors = graph.get_neighbors(isolated_store, "service:auth")
    assert len(neighbors) == 1
    assert neighbors[0]["neighbor_id"] == "service:redis"

    memory_blocks.append_block(
        isolated_store,
        "project",
        "\nProgress: Completed Redis token blacklist integration.",
        actor="agent-01",
        reason="milestone completion",
    )

    memory_blocks.replace_block(
        isolated_store,
        "project",
        "Migrate user authentication service to OAuth2/OIDC",
        "Migrate user authentication service to OAuth2/OIDC [IN REVIEW]",
        actor="agent-01",
        reason="status update",
    )

    history = memory_blocks.block_history(isolated_store, "project")
    assert len(history) == 3
    revisions = [h["revision"] for h in history]
    assert len(set(revisions)) == 3
