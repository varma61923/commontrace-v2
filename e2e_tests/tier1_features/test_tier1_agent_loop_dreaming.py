from __future__ import annotations

import os
from datetime import datetime, timezone

from e2e_tests.harness.cli_runner import require_milestone, run_cli
from e2e_tests.harness.fake_llm import fake_model


def test_t1_agent_loop_execution_turn_cycle(isolated_store: str):
    require_milestone("M3")
    reply = {"response": "Added a composite index on users(email, created_at).", "done": True, "memory": []}
    with fake_model(reply) as (env, prompts):
        res = run_cli("agent", "run", "--prompt", "Optimize SQL index for users table", "--json",
                      dest=isolated_store, env=env)
    res.assert_success()
    out = res.json()
    assert out["success"] is True and out["turns"] == 1
    assert "Optimize SQL index for users table" in prompts[0]
    traces = os.listdir(os.path.join(isolated_store, "memory", "traces"))
    assert any(out["run_id"][:8] in name for name in traces)


def test_t1_agent_loop_memory_block_dynamic_updates(isolated_store: str):
    require_milestone("M3")
    run_cli("block", "set", "project", "Initial task state", dest=isolated_store).assert_success()
    reply = {"response": "Recorded the milestone.", "done": True, "memory": [
        {"type": "memory_block_update", "name": "project", "content": "Milestone 1 completed", "mode": "set"},
    ]}
    with fake_model(reply) as (env, prompts):
        res = run_cli("agent", "run", "--prompt", "Update project status with milestone 1 completed",
                      dest=isolated_store, env=env)
    res.assert_success()
    assert "Initial task state" in prompts[0]
    block = run_cli("block", "get", "project", dest=isolated_store).assert_success()
    assert "Milestone 1 completed" in block.stdout


def test_t1_agent_loop_refuses_without_a_model(isolated_store: str):
    res = run_cli("agent", "run", "--prompt", "Anything", dest=isolated_store,
                  env={"COMMONTRACE_LLM_PROVIDER": "anthropic", "COMMONTRACE_LLM_API_KEY": ""})
    res.assert_failure(expected_code=2)


def test_t1_dreaming_profile_synthesis(isolated_store: str):
    run_cli(
        "block", "set", "persona", "Autonomous Database Reliability Engineer", dest=isolated_store
    ).assert_success()
    run_cli(
        "fact", "add", "Maximum Postgres connection count is 100", "--confidence", "0.9", dest=isolated_store
    ).assert_success()

    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()
    assert "profile synthesized" in res_dream.stdout

    profile_path = os.path.join(isolated_store, "memory", "profile.md")
    assert os.path.exists(profile_path), "memory/profile.md must be generated"

    with open(profile_path, encoding="utf-8") as f:
        profile_content = f.read()

    assert "Autonomous Database Reliability Engineer" in profile_content
    assert "Maximum Postgres connection count is 100" in profile_content
    assert "Fleet Health" in profile_content


def test_t1_dreaming_report_log_generation(isolated_store: str):
    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    report_path = os.path.join(isolated_store, "memory", "dream", f"{today_str}.md")
    assert os.path.exists(report_path), f"Daily dreaming report {report_path} must exist"

    with open(report_path, encoding="utf-8") as f:
        report_content = f.read()
    assert "CommonTrace Dream" in report_content or "Dream" in report_content


def test_t1_dreaming_trace_mining(isolated_store: str):
    res_cap = run_cli(
        "capture",
        "--title", "Postgres lock contention timeout",
        "--context", "Heavy concurrent batch jobs acquired shared exclusive locks",
        "--solution", "Partition batches and set statement_timeout",
        "--agent-type", "coding",
        dest=isolated_store,
    )
    res_cap.assert_success()

    res_dream = run_cli("dream", dest=isolated_store)
    res_dream.assert_success()
    assert "dream:" in res_dream.stdout
