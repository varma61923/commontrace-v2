from __future__ import annotations

import os

from commontrace import hierarchical, memory_blocks
from e2e_tests.harness.cli_runner import run_cli


def test_t3_agent_memory_to_dream_pipeline(isolated_store: str):
    memory_blocks.set_block(
        isolated_store,
        "persona",
        "Autonomous Staff Reliability Agent for Enterprise Kubernetes.",
        actor="agent-01",
        reason="initial identity",
    )
    memory_blocks.set_block(
        isolated_store,
        "project",
        "Target: Resolve pod eviction cascading failures.",
        actor="agent-01",
        reason="current task",
    )

    hierarchical.add_fact(
        isolated_store,
        "Kubernetes ephemeral storage limit must be 10Gi per node.",
        category="constraint",
        scopes=["infra", "k8s"],
        confidence=0.95,
    )

    res_cap = run_cli(
        "capture",
        "--title", "Kubernetes pod eviction incident",
        "--context", "Nodes exhausted ephemeral storage running unconstrained logs",
        "--solution", "Enforce 10Gi storage limits and logrotate sidecar",
        "--agent-type", "coding",
        dest=isolated_store,
    )
    res_cap.assert_success()

    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()
    assert "profile synthesized" in res_dream.stdout

    profile_path = os.path.join(isolated_store, "memory", "profile.md")
    assert os.path.exists(profile_path)
    with open(profile_path, encoding="utf-8") as f:
        profile_text = f.read()

    assert "Autonomous Staff Reliability Agent" in profile_text
    assert "Target: Resolve pod eviction cascading failures" in profile_text
    assert "Kubernetes ephemeral storage limit must be 10Gi" in profile_text
    assert "Fleet Health" in profile_text
