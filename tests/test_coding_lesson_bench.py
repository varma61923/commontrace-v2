import sys

from benchmarks.coding_lesson_bench import run


def test_real_test_outcomes_in_isolated_randomized_tasks(tmp_path):
    repo = tmp_path / "task"
    repo.mkdir()
    (repo / "solution.py").write_text("answer = 0\n")
    (repo / "check.py").write_text("from solution import answer\nassert answer == 1\n")
    runner = tmp_path / "runner.py"
    runner.write_text(
        'import json, sys\nr=json.load(sys.stdin)\nv=1 if r["lesson"] else 2\n'
        'patch="diff --git a/solution.py b/solution.py\\n--- a/solution.py\\n+++ b/solution.py\\n@@ -1 +1 @@\\n-answer = 0\\n+answer = "+str(v)+"\\n"\n'
        'print(json.dumps({"patch":patch,"usage":{"input_tokens":100,"output_tokens":20},"cost_usd":0.01}))\n'
    )
    tasks = [
        {
            "id": "repair-fixture",
            "repo": str(repo),
            "prompt": "repair the answer",
            "test_command": [sys.executable, "check.py"],
            "lesson": "answer must be one",
            "lesson_relevant": True,
        }
    ]
    result = run(tasks, [sys.executable, str(runner)], occasions=12)
    assert all(row["succeeded"] == row["injected"] for row in result["rows"])
    assert (repo / "solution.py").read_text() == "answer = 0\n"
    assert result["causal_effect"] is not None
    assert result["causal_effect"]["effect"] == 1.0  # Sanity fixture, not deployment evidence.

    assert result["injection_precision"] == 1.0
    assert result["cost"]["unpriced_occasions"] == 0
    assert result["cost"]["tokens"] == {"input_tokens": 1200, "output_tokens": 240}
    assert round(result["cost"]["known_usd"], 2) == 0.12
    assert result["harmful_retirement_N"] is None
