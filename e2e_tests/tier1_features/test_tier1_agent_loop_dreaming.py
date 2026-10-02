from __future__ import annotations

import os
from datetime import datetime, timezone

from e2e_tests.harness.cli_runner import require_milestone, run_cli


def test_t1_agent_loop_execution_turn_cycle(isolated_store: str):
    """E2E-T1-AL-1: Execute multi-turn agent loop with bounded context assembly (M3)."""
    require_milestone("M3")
    res = run_cli("agent", "run", "--prompt", "Optimize SQL index for users table", dest=isolated_store)
    res.assert_success()


def test_t1_agent_loop_memory_block_dynamic_updates(isolated_store: str):
    """E2E-T1-AL-2: Agent loop dynamically updates working memory blocks during task (M3)."""
    require_milestone("M3")
    run_cli("block", "set", "project", "Initial task state", dest=isolated_store).assert_success()
    res = run_cli("agent", "run", "--prompt", "Update project status with milestone 1 completed", dest=isolated_store)
    res.assert_success()


def test_t1_dreaming_profile_synthesis(isolated_store: str):
    """E2E-T1-AL-3: Dynamic dreaming generates memory/profile.md consolidating blocks and facts."""
    # Setup working memory block and fact
    run_cli(
        "block", "set", "persona", "Autonomous Database Reliability Engineer", dest=isolated_store
    ).assert_success()
    run_cli(
        "fact", "add", "Maximum Postgres connection count is 100", "--confidence", "0.9", dest=isolated_store
    ).assert_success()

    # Run dream pass
    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()
    assert "profile synthesized" in res_dream.stdout

    # Verify memory/profile.md exists on disk and contains blocks and facts
    profile_path = os.path.join(isolated_store, "memory", "profile.md")
    assert os.path.exists(profile_path), "memory/profile.md must be generated"

    with open(profile_path, encoding="utf-8") as f:
        profile_content = f.read()

    assert "Autonomous Database Reliability Engineer" in profile_content
    assert "Maximum Postgres connection count is 100" in profile_content
    assert "Fleet Health" in profile_content


def test_t1_dreaming_report_log_generation(isolated_store: str):
    """E2E-T1-AL-4: Dynamic dreaming creates a dated report in memory/dream/YYYY-MM-DD.md."""
    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = os.path.join(isolated_store, "memory", "dream", f"{today_str}.md")
    assert os.path.exists(report_path), f"Daily dreaming report {report_path} must exist"

    with open(report_path, encoding="utf-8") as f:
        report_content = f.read()
    assert "CommonTrace Dream" in report_content or "Dream" in report_content


def test_t1_dreaming_trace_mining(isolated_store: str):
    """E2E-T1-AL-5: Capture an episodic trace and run dream pass to consolidate trace signals."""
    res_cap = run_cli(
        "capture",
        "--title", "Postgres lock contention timeout",
        "--context", "Heavy concurrent batch jobs acquired shared exclusive locks",
        "--solution", "Partition batches and set statement_timeout",
        "--agent-type", "coding",
        dest=isolated_store,
    )
    res_cap.assert_success()

    # Run dream pass over captured trace
    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()
    assert "dream:" in res_dream.stdout
