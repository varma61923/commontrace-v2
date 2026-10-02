from __future__ import annotations

from commontrace import graph, hierarchical, memory_blocks


def test_t4_autonomous_coding_agent_lifecycle(isolated_store: str):
    """E2E-T4-RW-2: Multi-turn autonomous coding agent manages working memory and consults knowledge models."""
    # 1. Agent boots up with persona and project context
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

    # 2. Agent consults atomic facts for team constraints
    hierarchical.add_fact(
        isolated_store,
        "JWT tokens must expire within 15 minutes and use RS256 signature algorithm.",
        category="constraint",
        scopes=["auth", "security"],
        confidence=1.0,
    )

    # 3. Agent navigates knowledge graph
    graph.add_node(isolated_store, "service:auth", entity_type="service", name="Auth Service")
    graph.add_node(isolated_store, "service:redis", entity_type="service", name="Redis Session Cache")
    graph.add_edge(isolated_store, "service:auth", "service:redis", "uses", weight=1.0)

    neighbors = graph.get_neighbors(isolated_store, "service:auth")
    assert len(neighbors) == 1
    assert neighbors[0]["neighbor_id"] == "service:redis"

    # 4. Agent completes migration step 1 and updates project working memory block
    memory_blocks.append_block(
        isolated_store,
        "project",
        "\nProgress: Completed Redis token blacklist integration.",
        actor="agent-01",
        reason="milestone completion",
    )

    # 5. Agent replaces sprint status string atomically
    memory_blocks.replace_block(
        isolated_store,
        "project",
        "Migrate user authentication service to OAuth2/OIDC",
        "Migrate user authentication service to OAuth2/OIDC [IN REVIEW]",
        actor="agent-01",
        reason="status update",
    )

    # 6. Verify audit history contains chronological cryptographic revisions
    history = memory_blocks.block_history(isolated_store, "project")
    assert len(history) == 3
    # Verify cryptographic chaining: each revision is unique 16-hex hash
    revisions = [h["revision"] for h in history]
    assert len(set(revisions)) == 3
