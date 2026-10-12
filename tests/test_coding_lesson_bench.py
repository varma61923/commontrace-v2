import json
import math
import os
import sys
from types import SimpleNamespace

import pytest

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


def _heterogeneous_task_fixture(tmp_path):
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

    return tasks, runner


def test_multi_task_effects_report_weighted_stratified_uplift_with_simultaneous_ci(tmp_path):
    from commontrace import experiment

    tasks, runner = _heterogeneous_task_fixture(tmp_path)
    result = run(tasks, [sys.executable, str(runner)], occasions=81, seed=61923)

    assert result["causal_effect"] is None
    assert result["causal_effect_scope"] == "weighted_task_stratified"
    assert result["uplift_gate_evaluated"] is True
    assert result["uplift_gate_met"] is False
    effects = {effect["lesson_slug"]: effect for effect in result["causal_effects_by_task"]}
    assert set(effects) == {"parser", "formatter"}
    assert effects["parser"]["effect"] > 0.5
    assert effects["formatter"]["effect"] < -0.5

    aggregate = result["causal_effect_aggregate"]
    assert aggregate["weighting"] == "round_robin_schedule"
    assert aggregate["task_weights"] == {"parser": 41 / 81, "formatter": 40 / 81}
    scheduled_counts = {task_id: 0 for task_id in ("parser", "formatter")}
    for row in result["rows"]:
        scheduled_counts[row["task"]] += 1
    assert scheduled_counts == {"parser": 41, "formatter": 40}
    assert aggregate["effect"] == round(
        (41 / 81) * effects["parser"]["effect"] + (40 / 81) * effects["formatter"]["effect"], 4
    )
    displayed_ci_low = (
        (41 / 81) * effects["parser"]["ci_low"] + (40 / 81) * effects["formatter"]["ci_low"]
    )
    displayed_ci_high = (
        (41 / 81) * effects["parser"]["ci_high"] + (40 / 81) * effects["formatter"]["ci_high"]
    )
    assert aggregate["ci_low"] <= displayed_ci_low
    assert displayed_ci_low - aggregate["ci_low"] <= 0.0002
    assert aggregate["ci_high"] >= displayed_ci_high
    assert aggregate["ci_high"] - displayed_ci_high <= 0.0002
    assert aggregate["per_task_alpha"] == 0.025
    assert "Bonferroni familywise correction" in aggregate["interval_method"]
    assert aggregate["ci_low"] <= 0 <= aggregate["ci_high"]
    for task_id, target_n in (("parser", 21), ("formatter", 20)):
        task_rows = [
            row for row in result["rows"]
            if row["task"] == task_id and row["randomized"] and row["succeeded"] is not None
        ]
        injected = [row for row in task_rows if row["injected"]]
        withheld = [row for row in task_rows if not row["injected"]]
        raw_low, raw_high = experiment.anytime_confidence_interval(
            sum(row["succeeded"] for row in injected),
            len(injected),
            sum(row["succeeded"] for row in withheld),
            len(withheld),
            alpha=0.025,
            target_n_per_arm=target_n,
        )
        assert effects[task_id]["ci_low"] == math.floor(raw_low * 10000 + 1e-10) / 10000
        assert effects[task_id]["ci_high"] == math.ceil(raw_high * 10000 - 1e-10) / 10000


def test_benchmark_reports_the_preplanned_horizon_when_outcome_rate_changes(tmp_path, monkeypatch):
    from commontrace import experiment

    task = {
        "id": "parser",
        "repo": str(tmp_path / "parser"),
        "prompt": "repair the parser",
        "test_command": ["fixture-test"],
        "lesson": "repair parser",
    }
    os.makedirs(task["repo"])

    def run_at_rate(period):
        succeeded_by_workspace = {}

        def fake_copytree(_source, destination, **_kwargs):
            os.makedirs(destination)
            return destination

        def fake_run(command, **kwargs):
            workspace = kwargs["cwd"]
            if command == [sys.executable, "fixture-runner"]:
                request = json.loads(kwargs["input"])
                succeeded_by_workspace[workspace] = int(request["occasion_id"]) % period == 0
                return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}), stderr="")
            if command == ["git", "apply", "-"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command == ["fixture-test"]:
                return SimpleNamespace(
                    returncode=0 if succeeded_by_workspace[workspace] else 1,
                    stdout=b"",
                    stderr=b"",
                )
            raise AssertionError(f"unexpected subprocess: {command}")

        monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
        monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)
        return run([task], [sys.executable, "fixture-runner"], occasions=400, seed=912)

    results = [run_at_rate(2), run_at_rate(10)]
    observed_rates = [
        sum(row["succeeded"] for row in result["rows"]) / len(result["rows"])
        for result in results
    ]
    assert observed_rates[0] > 0.4 and observed_rates[1] < 0.15

    for result in results:
        effect = result["causal_effects_by_task"][0]
        assert effect["planned_occasions"] == 400
        assert effect["target_n_per_arm"] == 200
        injected_n = sum(row["randomized"] and row["injected"] for row in result["rows"])
        injected_successes = sum(row["randomized"] and row["injected"] and row["succeeded"] for row in result["rows"])
        withheld_n = sum(row["randomized"] and not row["injected"] for row in result["rows"])
        withheld_successes = sum(row["randomized"] and not row["injected"] and row["succeeded"] for row in result["rows"])
        raw_low, raw_high = experiment.anytime_confidence_interval(
            injected_successes,
            injected_n,
            withheld_successes,
            withheld_n,
            alpha=0.05,
            target_n_per_arm=200,
        )
        assert effect["ci_low"] == math.floor(raw_low * 10000 + 1e-10) / 10000
        assert effect["ci_high"] == math.ceil(raw_high * 10000 - 1e-10) / 10000


def test_round_robin_with_more_tasks_than_occasions_keeps_zero_weight_strata(tmp_path, monkeypatch):
    tasks = []
    for task_id in ("a", "b", "c"):
        repo = tmp_path / task_id
        repo.mkdir()
        tasks.append({
            "id": task_id,
            "repo": str(repo),
            "prompt": "run task",
            "test_command": ["fixture-test"],
            "lesson": f"lesson {task_id}",
        })

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **kwargs):
        if command == [sys.executable, "fixture-runner"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}), stderr="")
        if command == ["git", "apply", "-"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["fixture-test"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)
    result = run(tasks, [sys.executable, "fixture-runner"], occasions=2, seed=912)

    aggregate = result["causal_effect_aggregate"]
    assert aggregate["task_weights"] == {"a": 0.5, "b": 0.5, "c": 0.0}
    assert [row["planned_occasions"] for row in aggregate["per_task"]] == [1, 1, 0]
    assert [row["target_n_per_arm"] for row in aggregate["per_task"]] == [1, 1, 0]
    assert [row["target_n_per_arm"] for row in result["causal_effects_by_task"]] == [1, 1, 0]
    assert aggregate["evaluated"] is False
    assert result["uplift_gate_met"] is False


def test_explicit_task_weights_define_the_reported_target_population(tmp_path):
    tasks, runner = _heterogeneous_task_fixture(tmp_path)
    result = run(
        tasks,
        [sys.executable, str(runner)],
        occasions=80,
        seed=61923,
        task_weights={"parser": 3, "formatter": 1},
    )

    effects = {effect["lesson_slug"]: effect for effect in result["causal_effects_by_task"]}
    aggregate = result["causal_effect_aggregate"]
    assert aggregate["weighting"] == "explicit_task_weights"
    assert aggregate["task_weights"] == {"parser": 0.75, "formatter": 0.25}
    assert aggregate["effect"] == round(
        0.75 * effects["parser"]["effect"] + 0.25 * effects["formatter"]["effect"], 4
    )
    displayed_ci_low = 0.75 * effects["parser"]["ci_low"] + 0.25 * effects["formatter"]["ci_low"]
    displayed_ci_high = 0.75 * effects["parser"]["ci_high"] + 0.25 * effects["formatter"]["ci_high"]
    assert aggregate["ci_low"] <= displayed_ci_low + 1e-12
    assert displayed_ci_low - aggregate["ci_low"] <= 0.0002
    assert aggregate["ci_high"] >= displayed_ci_high - 1e-12
    assert aggregate["ci_high"] - displayed_ci_high <= 0.0002


def test_task_weights_must_cover_every_task_with_positive_weight(tmp_path):
    tasks = [
        {
            "id": task_id,
            "repo": str(tmp_path / task_id),
            "prompt": "run",
            "test_command": ["test"],
            "lesson": "lesson",
        }
        for task_id in ("parser", "formatter")
    ]
    with pytest.raises(ValueError, match="name every task exactly once"):
        run(tasks, [sys.executable, "unused-runner"], occasions=2, task_weights={"parser": 1})
    with pytest.raises(ValueError, match="must be positive"):
        run(
            tasks,
            [sys.executable, "unused-runner"],
            occasions=2,
            task_weights={"parser": 1, "formatter": 0},
        )


def test_multi_task_gate_fails_closed_on_unresolved_randomized_outcome(tmp_path, monkeypatch):
    tasks = []
    for task_id in ("parser", "formatter"):
        repo = tmp_path / task_id
        repo.mkdir()
        tasks.append({
            "id": task_id,
            "repo": str(repo),
            "prompt": "run the task",
            "test_command": ["fixture-test"],
            "lesson": f"repair {task_id}",
        })

    def fake_copytree(_source, destination, **_kwargs):
        os.makedirs(destination)
        return destination

    def fake_run(command, **kwargs):
        if command == [sys.executable, "fixture-runner"]:
            request = json.loads(kwargs["input"])
            if request["task_id"] == "parser" and request["occasion_id"] == "0":
                return SimpleNamespace(returncode=1, stdout="", stderr="runner failed")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"patch": ""}))
        if command == ["git", "apply", "-"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["fixture-test"]:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected subprocess: {command}")

    monkeypatch.setattr("benchmarks.coding_lesson_bench.shutil.copytree", fake_copytree)
    monkeypatch.setattr("benchmarks.coding_lesson_bench.subprocess.run", fake_run)

    result = run(tasks, [sys.executable, "fixture-runner"], occasions=40, seed=7)

    aggregate = result["causal_effect_aggregate"]
    assert aggregate["evaluated"] is False
    assert aggregate["effect"] is None
    assert aggregate["ci_low"] is None
    assert aggregate["unresolved_randomized_occasions"] == 1
    assert aggregate["reason"] == "unresolved randomized outcomes prevent aggregate inference"
    assert result["uplift_gate_evaluated"] is False
    assert result["uplift_gate_met"] is False
    assert result["uplift_gate_reason"] == aggregate["reason"]
    parser = next(row for row in aggregate["per_task"] if row["task"] == "parser")
    assert parser["status"] == "no_resolved_randomized_outcomes"
    assert parser["unresolved_occasions"] == 1


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
    assert len(result["causal_effects_by_task"]) == 1
    assert result["causal_effects_by_task"][0]["effect"] is None
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
