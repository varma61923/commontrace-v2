import json
import os
import sys
from types import SimpleNamespace

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


def test_multi_task_effects_stay_stratified_without_a_pooled_uplift_gate(tmp_path):
    tasks = []
    for task_id in ("parser", "formatter"):
        repo = tmp_path / task_id
        repo.mkdir()
        (repo / "solution.py").write_text("answer = 0\n")
        (repo / "check.py").write_text("from solution import answer\nassert answer == 1\n")
        tasks.append(
            {
                "id": task_id,
                "repo": str(repo),
                "prompt": "repair the answer",
                "test_command": [sys.executable, "check.py"],
                "lesson": f"repair {task_id}",
            }
        )
    runner = tmp_path / "runner.py"
    runner.write_text(
        'import json, sys\nr=json.load(sys.stdin)\n'
        'helpful=(r["task_id"] == "parser") == bool(r["lesson"])\n'
        'v=1 if helpful else 2\n'
        'patch="diff --git a/solution.py b/solution.py\\n--- a/solution.py\\n+++ b/solution.py\\n@@ -1 +1 @@\\n-answer = 0\\n+answer = "+str(v)+"\\n"\n'
        'print(json.dumps({"patch":patch}))\n'
    )

    result = run(tasks, [sys.executable, str(runner)], occasions=80, seed=61923)

    assert result["causal_effect"] is None
    assert result["causal_effect_scope"] == "per_task_no_pooled_gate"
    assert result["uplift_gate_evaluated"] is False
    assert result["uplift_gate_met"] is False
    assert result["uplift_gate_reason"] == (
        "Multi-task results remain stratified; no pooled uplift estimator is implemented."
    )
    effects = {effect["lesson_slug"]: effect for effect in result["causal_effects_by_task"]}
    assert set(effects) == {"parser", "formatter"}
    assert effects["parser"]["effect"] > 0.5
    assert effects["formatter"]["effect"] < -0.5


def test_harmful_lesson_retirement_uses_the_real_causal_lifecycle(tmp_path, monkeypatch):
    from commontrace import holdout_io, memory_journal

    repo = tmp_path / "task"
    repo.mkdir()
    task = {
        "id": "harmful-fixture",
        "repo": str(repo),
        "prompt": "run the task",
        "test_command": ["fixture-test"],
        "lesson": "Use the broken parser path.",
        "lesson_harmful": True,
    }
    injected = False

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **kwargs):
        nonlocal injected
        if command == [sys.executable, "fixture-runner"]:
            request = json.loads(kwargs["input"])
            injected = bool(request["lesson"])
            return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}))
        if command == ["git", "apply", "-"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == task["test_command"]:
            # The lesson causes failure; withholding it leaves the task green.
            return SimpleNamespace(returncode=1 if injected else 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)

    initialized_before = memory_journal._INITIALIZED.stats()["entries"]
    result = run([task], [sys.executable, "fixture-runner"], occasions=1000, seed=61923)

    retirement = result["harmful_retirement"]
    assert retirement["all_retired"] is True
    assert retirement["per_task"][0]["status"] == "retired"
    assert 1 <= result["harmful_retirement_N"] <= 1000
    assert retirement["per_task"][0]["N"] == result["harmful_retirement_N"]
    assert retirement["per_task"][0]["assigned_occasions"] == result["harmful_retirement_N"]
    assert retirement["per_task"][0]["resolved_occasions"] == result["harmful_retirement_N"]
    assert retirement["per_task"][0]["integrity"] == "SOUND"
    assert result["harmful_retirement_N"] < 1000
    after_retirement = result["rows"][result["harmful_retirement_N"] :]
    assert after_retirement
    assert all(not row["randomized"] and not row["injected"] for row in after_retirement)
    assert all(row["lifecycle_status"] == "retired" for row in after_retirement)
    assert memory_journal._INITIALIZED.stats()["entries"] == initialized_before
    assert not any("codinglessonbench-state-" in key[0] for key in memory_journal._CONNECTIONS.idle)
    assert not any("codinglessonbench-state-" in path for path in holdout_io._outcome_cache)
    assert not any("codinglessonbench-state-" in path for path in holdout_io._log_cache)


def test_harmful_retirement_stays_unreported_when_outcomes_are_neutral(tmp_path, monkeypatch):
    repo = tmp_path / "task"
    repo.mkdir()
    task = {
        "id": "neutral-fixture",
        "repo": str(repo),
        "prompt": "run the task",
        "test_command": ["fixture-test"],
        "lesson": "This lesson has no effect.",
        "lesson_harmful": True,
    }

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **_kwargs):
        if command == [sys.executable, "fixture-runner"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}))
        if command == ["git", "apply", "-"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == task["test_command"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)

    result = run([task], [sys.executable, "fixture-runner"], occasions=12, seed=42)

    assert result["harmful_retirement_N"] is None
    assert result["harmful_retirement"]["all_retired"] is False
    assert result["harmful_retirement"]["per_task"][0]["status"] == "shadow"
    assert result["harmful_retirement"]["per_task"][0]["N"] is None
    assert result["harmful_retirement"]["per_task"][0]["resolved_occasions"] == 12


def test_execution_errors_are_unresolved_and_censor_harmful_lifecycle(tmp_path, monkeypatch):
    repo = tmp_path / "task"
    repo.mkdir()
    task = {
        "id": "runner-error",
        "repo": str(repo),
        "prompt": "run the task",
        "test_command": ["fixture-test"],
        "lesson": "A lesson with no measured effect.",
        "lesson_harmful": True,
    }

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **_kwargs):
        if command == [sys.executable, "fixture-runner"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="runner failed")
        raise AssertionError(f"unexpected subprocess after runner failure: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)

    result = run([task], [sys.executable, "fixture-runner"], occasions=12, seed=91)

    assert result["causal_effect"] is None
    assert result["harmful_retirement_N"] is None
    assert result["harmful_retirement"]["per_task"][0]["status"] == "censored_execution_error"
    assert result["harmful_retirement"]["per_task"][0]["assigned_occasions"] == 1
    assert result["harmful_retirement"]["per_task"][0]["resolved_occasions"] == 0
    assert all(row["succeeded"] is None for row in result["rows"])
    assert all(not row["randomized"] for row in result["rows"][1:])


def test_repository_setup_errors_are_unresolved_and_censor_harmful_lifecycle(tmp_path, monkeypatch):
    repo = tmp_path / "task"
    repo.mkdir()
    task = {
        "id": "copy-error",
        "repo": str(repo),
        "prompt": "run the task",
        "test_command": ["fixture-test"],
        "lesson": "A lesson with an unmeasured effect.",
        "lesson_harmful": True,
    }

    def failed_copytree(*_args, **_kwargs):
        raise OSError("fixture repository copy failed")

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("runner must not start when repository setup fails")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", failed_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", unexpected_run)

    result = run([task], [sys.executable, "fixture-runner"], occasions=12, seed=91)

    assert result["causal_effect"] is None
    assert result["harmful_retirement_N"] is None
    assert result["harmful_retirement"]["per_task"][0]["status"] == "censored_execution_error"
    assert result["harmful_retirement"]["per_task"][0]["execution_error"] == "OSError"
    assert result["harmful_retirement"]["per_task"][0]["assigned_occasions"] == 1
    assert result["harmful_retirement"]["per_task"][0]["resolved_occasions"] == 0
    assert all(row["succeeded"] is None for row in result["rows"])


def test_execution_errors_censor_untracked_task_assignments_too(tmp_path, monkeypatch):
    repo = tmp_path / "task"
    repo.mkdir()
    task = {
        "id": "unlabeled-runner-error",
        "repo": str(repo),
        "prompt": "run the task",
        "test_command": ["fixture-test"],
        "lesson": "An unlabelled lesson.",
    }
    runner_calls = 0

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **_kwargs):
        nonlocal runner_calls
        if command == [sys.executable, "fixture-runner"]:
            runner_calls += 1
            if runner_calls == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="runner failed")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}))
        if command == ["git", "apply", "-"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == task["test_command"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)

    result = run([task], [sys.executable, "fixture-runner"], occasions=12, seed=42)

    assert result["causal_effect"] is None
    assert result["causal_effects_by_task"] == []
    assert result["uplift_gate_evaluated"] is False
    assert result["rows"][0]["randomized"] is True
    assert result["rows"][0]["execution_error"] is not None
    assert all(not row["randomized"] and not row["injected"] for row in result["rows"][1:])
    assert all(row["lifecycle_status"] == "censored_execution_error" for row in result["rows"])


def test_harmful_tasks_without_trial_opportunities_are_unmeasured(tmp_path, monkeypatch):
    tasks = [
        {
            "id": f"task-{index}",
            "repo": str(tmp_path / f"repo-{index}"),
            "prompt": "run the task",
            "test_command": ["fixture-test"],
            "lesson": f"lesson {index}",
            "lesson_harmful": True,
        }
        for index in range(3)
    ]
    for task in tasks:
        os.makedirs(task["repo"])

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **_kwargs):
        if command == [sys.executable, "fixture-runner"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}))
        if command == ["git", "apply", "-"] or command == ["fixture-test"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)

    result = run(tasks, [sys.executable, "fixture-runner"], occasions=2, seed=7)

    assert result["harmful_retirement_N"] is None
    assert result["harmful_retirement"]["all_retired"] is False
    assert result["harmful_retirement"]["per_task"][2]["status"] == "unmeasured"
    assert result["harmful_retirement"]["per_task"][2]["assigned_occasions"] == 0
