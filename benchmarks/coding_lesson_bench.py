"""CodingLessonBench v1: isolated real test commands, randomized lesson exposure.

The runner is an operator-supplied executable receiving JSON on stdin and
returning {"patch": "unified diff"}. No model or outcome is fabricated. Tasks
are explicit SWE-style checkouts with a test argv; each occasion starts fresh.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict

from unimem import experiment

_UPLIFT_ALPHA = 0.05
_UPLIFT_THRESHOLD = 0.05


def _planned_task_slots(tasks: list[dict], occasions: int) -> dict[str, int]:
    base, remainder = divmod(occasions, len(tasks))
    return {
        task["id"]: base + int(index < remainder)
        for index, task in enumerate(tasks)
    }


def _normalize_task_weights(
    tasks: list[dict], task_weights: dict[str, float] | None, occasions: int
) -> dict[str, float]:
    """Return fixed target weights, normalized and in task-manifest order."""
    task_ids = [task["id"] for task in tasks]
    if task_weights is None:
        raw = _planned_task_slots(tasks, occasions)
    else:
        if not isinstance(task_weights, dict) or set(task_weights) != set(task_ids):
            raise ValueError("task weights must name every task exactly once")
        raw = task_weights
    weights = {}
    for task_id in task_ids:
        value = raw[task_id]
        if type(value) not in (int, float):
            raise ValueError("task weights must be finite numbers")
        try:
            value = float(value)
        except OverflowError as exc:
            raise ValueError("task weights must be finite numbers") from exc
        if not math.isfinite(value):
            raise ValueError("task weights must be finite numbers")
        if (task_weights is not None and value <= 0) or value < 0:
            raise ValueError("explicit task weights must be positive; scheduled weights cannot be negative")
        weights[task_id] = value
    total = sum(weights.values())
    if not math.isfinite(total) or total <= 0:
        raise ValueError("task weights must have a finite positive sum")
    return {task_id: value / total for task_id, value in weights.items()}


def _stratified_uplift(
    tasks: list[dict],
    rows: list[dict],
    effects: list[dict],
    weights: dict[str, float],
    planned_slots: dict[str, int],
    *,
    weighting: str,
) -> dict:
    """Build a weighted estimate with preplanned, simultaneous anytime intervals."""
    observed = {
        effect["lesson_slug"]: effect
        for effect in effects
        if effect.get("effect") is not None
    }
    effect_rows = {effect["lesson_slug"]: effect for effect in effects}
    target_tasks = [task for task in tasks if weights[task["id"]] > 0]
    task_counts = {}
    for task in tasks:
        task_counts[task["id"]] = {
            "randomized": 0,
            "unresolved": 0,
            "injected_n": 0,
            "injected_successes": 0,
            "withheld_n": 0,
            "withheld_successes": 0,
        }
    for row in rows:
        if row["randomized"]:
            counts = task_counts[row["task"]]
            counts["randomized"] += 1
            if row["succeeded"] is None:
                counts["unresolved"] += 1
            elif row["injected"]:
                counts["injected_n"] += 1
                counts["injected_successes"] += int(row["succeeded"])
            else:
                counts["withheld_n"] += 1
                counts["withheld_successes"] += int(row["succeeded"])

    per_task = []
    raw_intervals = {}
    raw_effects = {}
    all_have_estimate = True
    all_testable = True
    for task in tasks:
        task_id = task["id"]
        counts = task_counts[task_id]
        effect = observed.get(task_id)
        injected_n = counts["injected_n"]
        withheld_n = counts["withheld_n"]
        target_n = (planned_slots[task_id] + 1) // 2
        has_estimate = injected_n > 0 and withheld_n > 0
        if effect is None or not has_estimate:
            if weights[task_id] > 0:
                all_have_estimate = False
                all_testable = False
            task_effect = effect_rows.get(task_id)
            if task_effect is not None:
                task_effect.update({
                    "n_injected": injected_n,
                    "n_withheld": withheld_n,
                    "planned_occasions": planned_slots[task_id],
                    "target_n_per_arm": target_n,
                    "unresolved_randomized_occasions": counts["unresolved"],
                    "ci_low": None,
                    "ci_high": None,
                    "significant": False,
                    "verdict": experiment.VERDICT_UNDERPOWERED,
                    "interval_method": "withheld: task has no resolved observations in both arms",
                    "note": (
                        "Unresolved randomized outcomes prevent a task-level interval."
                        if counts["unresolved"] else
                        "Resolved outcomes are missing from one randomized arm."
                    ),
                })
            per_task.append({
                "task": task_id,
                "weight": weights[task_id],
                "randomized_occasions": counts["randomized"],
                "unresolved_occasions": counts["unresolved"],
                "planned_occasions": planned_slots[task_id],
                "target_n_per_arm": target_n,
                "n_injected": injected_n,
                "n_withheld": withheld_n,
                "effect": (
                    round(
                        counts["injected_successes"] / injected_n
                        - counts["withheld_successes"] / withheld_n,
                        4,
                    )
                    if has_estimate else None
                ),
                "ci_low": None,
                "ci_high": None,
                "status": "no_resolved_randomized_outcomes",
            })
            continue
        testable = injected_n >= experiment.DEFAULT_MIN_ARM and withheld_n >= experiment.DEFAULT_MIN_ARM
        unresolved_task = counts["unresolved"] > 0
        if weights[task_id] > 0:
            all_testable = all_testable and testable
        if testable and not unresolved_task:
            raw_intervals[task_id] = experiment.anytime_confidence_interval(
                counts["injected_successes"],
                injected_n,
                counts["withheld_successes"],
                withheld_n,
                alpha=_UPLIFT_ALPHA / len(tasks),
                target_n_per_arm=target_n,
            )
        raw_effects[task_id] = (
            counts["injected_successes"] / injected_n
            - counts["withheld_successes"] / withheld_n
        )
        effect["planned_occasions"] = planned_slots[task_id]
        effect["target_n_per_arm"] = target_n
        effect["unresolved_randomized_occasions"] = counts["unresolved"]
        effect["interval_method"] = "UniMem anytime-valid interval with preplanned round-robin horizon"
        if task_id in raw_intervals:
            interval_low, interval_high = raw_intervals[task_id]
            effect["ci_low"] = math.floor(interval_low * 10000 + 1e-10) / 10000
            effect["ci_high"] = math.ceil(interval_high * 10000 - 1e-10) / 10000
            effect["significant"] = interval_low > 0.0 or interval_high < 0.0
            if interval_low > 0.0:
                effect["verdict"] = experiment.VERDICT_HELPS
                effect["note"] = "The fixed-horizon anytime-valid interval supports a positive task effect."
            elif interval_high < 0.0:
                effect["verdict"] = experiment.VERDICT_HURTS
                effect["note"] = "The fixed-horizon anytime-valid interval supports a negative task effect."
            else:
                effect["verdict"] = experiment.VERDICT_UNDERPOWERED
                effect["note"] = "The fixed-horizon anytime-valid interval spans zero; no directional verdict is established."
        else:
            effect["ci_low"] = None
            effect["ci_high"] = None
            effect["significant"] = False
            effect["verdict"] = experiment.VERDICT_UNDERPOWERED
            if unresolved_task:
                effect["note"] = "Unresolved randomized outcomes prevent a task-level interval."
            else:
                effect["note"] = (
                    f"At least {experiment.DEFAULT_MIN_ARM} resolved outcomes in each arm are required "
                    "before reporting an anytime-valid task interval."
                )
        per_task.append({
            "task": task_id,
            "weight": weights[task_id],
            "randomized_occasions": counts["randomized"],
            "unresolved_occasions": counts["unresolved"],
            "planned_occasions": planned_slots[task_id],
            "target_n_per_arm": target_n,
            "n_injected": injected_n,
            "n_withheld": withheld_n,
            "effect": round(
                counts["injected_successes"] / injected_n
                - counts["withheld_successes"] / withheld_n,
                4,
            ),
            "ci_low": raw_intervals.get(task_id, (None, None))[0],
            "ci_high": raw_intervals.get(task_id, (None, None))[1],
            "status": "testable" if testable else "underpowered",
        })

    target_unresolved = sum(task_counts[task["id"]]["unresolved"] for task in target_tasks)
    point = (
        sum(
            weights[task["id"]] * raw_effects[task["id"]]
            for task in target_tasks
        )
        if all_have_estimate and target_unresolved == 0 else None
    )
    interval_available = all_have_estimate and all_testable and target_unresolved == 0
    for row in per_task:
        if row["task"] in raw_intervals:
            row["ci_low"] = math.floor(row["ci_low"] * 10000 + 1e-10) / 10000
            row["ci_high"] = math.ceil(row["ci_high"] * 10000 - 1e-10) / 10000
    if interval_available:
        ci_low = sum(
            weights[task["id"]] * raw_intervals[task["id"]][0]
            for task in target_tasks
        )
        ci_high = sum(
            weights[task["id"]] * raw_intervals[task["id"]][1]
            for task in target_tasks
        )
        # Keep displayed confidence bounds conservative by rounding out.
        ci_low = math.floor(ci_low * 10000 + 1e-10) / 10000
        ci_high = math.ceil(ci_high * 10000 - 1e-10) / 10000
    else:
        ci_low = ci_high = None
    if target_unresolved:
        reason = "unresolved randomized outcomes prevent aggregate inference"
    elif not all_have_estimate:
        reason = "one or more tasks have no resolved randomized outcomes"
    elif not all_testable:
        reason = f"each task needs at least {experiment.DEFAULT_MIN_ARM} resolved outcomes in both arms"
    else:
        reason = None

    return {
        "estimand": "weighted_average_of_task_specific_success_rate_differences",
        "weighting": weighting,
        "task_weights": weights,
        "effect": round(point, 4) if point is not None else None,
        "effect_unrounded": point,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "evaluated": interval_available,
        "reason": reason,
        "confidence_level": 0.95,
        "alpha": _UPLIFT_ALPHA,
        "per_task_alpha": _UPLIFT_ALPHA / len(tasks),
        "positive_weight_strata": len(target_tasks),
        "interval_method": "weighted sum of UniMem anytime-valid intervals with preplanned round-robin horizons and Bonferroni familywise correction",
        "min_resolved_per_arm": experiment.DEFAULT_MIN_ARM,
        "unresolved_randomized_occasions": target_unresolved,
        "per_task": per_task,
    }


class _HarmfulRetirementTracker:
    """Run the production randomized-assignment and lesson-retirement path per task."""

    def __init__(self, tasks: list[dict], occasions: int, seed: int) -> None:
        from unimem import holdout_io
        from unimem.measure import content_revision
        from unimem.memory_journal import Journal
        from unimem.memory_record import MemoryRecord

        self._holdout_io = holdout_io
        self._entries: dict[str, dict] = {}
        self._roots: list[str] = []
        self._halted_tasks: dict[str, str] = {}
        base, remainder = divmod(occasions, len(tasks))
        for task_index, task in enumerate(tasks):
            assignments = base + (1 if task_index < remainder else 0)
            if task.get("lesson_harmful") is True and assignments > 10000:
                raise ValueError("harmful lesson evaluations are limited to 10000 occasions per task")
        self._temporary = tempfile.TemporaryDirectory(prefix="codinglessonbench-state-")
        try:
            self._initialize(tasks, seed, base, remainder, content_revision, Journal, MemoryRecord)
        except BaseException:
            self.close()
            raise

    def _initialize(self, tasks, seed, base, remainder, content_revision, Journal, MemoryRecord) -> None:
        from unimem import holdout_io

        for task_index, task in enumerate(tasks):
            if task.get("lesson_harmful") is not True:
                continue
            assignments = base + (1 if task_index < remainder else 0)
            if assignments == 0:
                continue
            task_id = task["id"]
            digest = hashlib.sha256(task_id.encode()).hexdigest()
            root = os.path.join(self._temporary.name, digest)
            self._roots.append(root)
            identity = f"codinglessonbench-{digest[:32]}"
            config = holdout_io.configure(
                root,
                rate=0.5,
                salt=f"codinglessonbench-{seed}-{digest[:16]}",
                note="Isolated CodingLessonBench randomized lifecycle trial.",
            )
            journal = Journal(root)
            record = journal.put(
                MemoryRecord(identity, "lesson", 1, {"text": task["lesson"]}, status="shadow")
            )
            journal.authorize_evaluation(
                identity,
                actor="CodingLessonBench",
                occasions=max(1, assignments),
                expires="2099-01-01T00:00:00+00:00",
            )
            self._entries[task_id] = {
                "root": root,
                "identity": identity,
                "journal": journal,
                "revision": content_revision(record.payload["text"]),
                "salt": config.salt,
                "assigned": 0,
                "resolved": 0,
                "injected": 0,
                "halted": False,
                "execution_error": None,
            }

    def assign(self, task: dict, occasion: int, rng: random.Random) -> tuple[bool, bool]:
        if task["id"] in self._halted_tasks:
            return False, False
        entry = self._entries.get(task["id"])
        if entry is None:
            return (False, False) if task.get("lesson_harmful") is True else (rng.random() >= 0.5, True)
        if entry["halted"]:
            return False, False
        current = entry["journal"].latest(entry["identity"])
        if current is None or current.status == "retired":
            return False, False
        withheld = self._holdout_io.assign_and_log(
            entry["root"],
            [entry["identity"]],
            occasion_id=str(occasion),
            rate=0.5,
            salt=entry["salt"],
            revisions={entry["identity"]: entry["revision"]},
        )
        injected = entry["identity"] not in withheld
        entry["assigned"] += 1
        entry["injected"] += int(injected)
        return injected, True

    def observe(self, task: dict, occasion: int, randomized: bool, succeeded: bool | None, error: str | None) -> None:
        if not randomized:
            return
        if error is not None or succeeded is None:
            # Keep the assignment unresolved and stop exposure. A runner or
            # harness failure is not evidence that the lesson harmed the task.
            reason = error or "UnresolvedOutcome"
            self._halted_tasks[task["id"]] = reason
            entry = self._entries.get(task["id"])
            if entry is not None:
                entry["halted"] = True
                entry["execution_error"] = reason
            return
        entry = self._entries.get(task["id"])
        if entry is None:
            return
        self._holdout_io.record_outcome(entry["root"], str(occasion), succeeded)
        entry["resolved"] += 1
        journal = entry["journal"]
        current = journal.latest(entry["identity"])
        if current is None or current.status == "retired":
            return
        # Reconcile the real integrity-checked, sequential causal log after each
        # resolved occasion. The summary below reads the journal transition itself.
        from unimem.session_learning import reconcile

        reconcile(entry["root"])

    def status(self, task: dict) -> str:
        if task["id"] in self._halted_tasks:
            return "censored_execution_error"
        entry = self._entries.get(task["id"])
        if entry is None:
            return "unmeasured" if task.get("lesson_harmful") is True else "not_managed"
        if entry["halted"]:
            return "censored_execution_error"
        record = entry["journal"].latest(entry["identity"])
        return record.status if record is not None else "missing"

    def summary(self, tasks: list[dict]) -> dict:
        per_task = []
        harmful = []
        for task in tasks:
            label = task.get("lesson_harmful")
            if type(label) is not bool:
                continue
            if label is False:
                per_task.append({"task": task["id"], "status": "not_harmful", "N": None})
                continue
            entry = self._entries.get(task["id"])
            if entry is None:
                row = {
                    "task": task["id"],
                    "status": "unmeasured",
                    "N": None,
                    "assigned_occasions": 0,
                    "resolved_occasions": 0,
                    "injected_exposures": 0,
                    "integrity": None,
                }
                per_task.append(row)
                harmful.append(row)
                continue
            record = entry["journal"].latest(entry["identity"])
            retired = record is not None and record.status == "retired"
            count = record.payload.get("occasions_to_decision") if retired else None
            integrity = record.payload.get("integrity") if retired else None
            row = {
                "task": task["id"],
                "status": (
                    "censored_execution_error" if entry["halted"]
                    else record.status if record is not None else "missing"
                ),
                "N": count if type(count) is int else None,
                "assigned_occasions": entry["assigned"],
                "resolved_occasions": entry["resolved"],
                "injected_exposures": entry["injected"],
                "integrity": integrity,
            }
            if entry["execution_error"] is not None:
                row["execution_error"] = entry["execution_error"]
            per_task.append(row)
            harmful.append(row)
        all_retired = bool(harmful) and all(
            row["status"] == "retired" and row["N"] is not None and row["integrity"] == "SOUND"
            for row in harmful
        )
        observed = [row["N"] for row in harmful if row["N"] is not None]
        return {
            "harmful_retirement_N": max(observed) if all_retired else None,
            "harmful_retirement": {
                "all_retired": all_retired,
                "harmful_tasks": len(harmful),
                "retired_tasks": sum(row["status"] == "retired" for row in harmful),
                "per_task": per_task,
                "N_definition": (
                    "All randomized injected and withheld occasions through the journal's actual retirement transition."
                ),
            },
        }

    def close(self) -> None:
        from unimem import memory_journal

        for root in self._roots:
            memory_journal.close_connections(root)
            self._holdout_io.clear_root_caches(root)
        self._entries.clear()
        self._roots.clear()
        self._halted_tasks.clear()
        self._temporary.cleanup()


def run(
    tasks: list[dict],
    runner: list[str],
    *,
    occasions=100,
    seed=61923,
    timeout=120,
    task_weights: dict[str, float] | None = None,
) -> dict:
    if not tasks or not runner or type(occasions) is not int or occasions < 2:
        raise ValueError("tasks, an executable runner and at least two occasions are required")
    required = ("id", "repo", "prompt", "test_command", "lesson")
    for task in tasks:
        if not isinstance(task, dict) or not all(key in task for key in required):
            raise ValueError("coding tasks require id, repo, prompt, test_command and lesson")
        if not isinstance(task["id"], str) or not task["id"]:
            raise ValueError("coding task ids must be non-empty strings")
        if not isinstance(task["test_command"], list) or any(
            not isinstance(value, str) for value in task["test_command"]
        ):
            raise ValueError("test command must be an argument vector")
    if len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("coding task ids must be unique for isolated lifecycle measurements")
    normalized_weights = _normalize_task_weights(tasks, task_weights, occasions)
    planned_slots = _planned_task_slots(tasks, occasions)
    target_n_by_task = {task_id: (slots + 1) // 2 for task_id, slots in planned_slots.items()}
    rng = random.Random(seed)
    observations, rows = [], []
    retirement_tracker = _HarmfulRetirementTracker(tasks, occasions, seed)
    try:
        for index in range(occasions):
            task = tasks[index % len(tasks)]
            lifecycle_started = time.perf_counter()
            injected, randomized = retirement_tracker.assign(task, index, rng)
            assignment_ms = (time.perf_counter() - lifecycle_started) * 1000
            with tempfile.TemporaryDirectory(prefix="codinglessonbench-") as directory:
                working = os.path.join(directory, "repo")
                succeeded, error, usage, cost = False, None, {}, None
                started = time.perf_counter()
                try:
                    shutil.copytree(
                        task["repo"],
                        working,
                        symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"),
                    )
                    request = {
                        "task_id": task["id"],
                        "prompt": task["prompt"],
                        "repo": working,
                        "lesson": task["lesson"] if injected else "",
                        "occasion_id": str(index),
                    }
                    generated = subprocess.run(
                        runner,
                        input=json.dumps(request),
                        text=True,
                        capture_output=True,
                        cwd=working,
                        timeout=timeout,
                        check=False,
                    )
                    if generated.returncode:
                        raise ValueError("runner exited unsuccessfully")
                    reply = json.loads(generated.stdout)
                    offered = reply.get("usage", {})
                    if isinstance(offered, dict):
                        usage = {
                            key: offered[key]
                            for key in ("input_tokens", "output_tokens")
                            if type(offered.get(key)) is int and offered[key] >= 0
                        }
                    offered_cost = reply.get("cost_usd")
                    if type(offered_cost) in (int, float) and math.isfinite(offered_cost) and offered_cost >= 0:
                        cost = offered_cost
                    patch = reply["patch"]
                    if not isinstance(patch, str) or len(patch.encode()) > 1000000:
                        raise ValueError("runner patch must be bounded text")
                    applied = subprocess.run(
                        ["git", "apply", "-"],
                        input=patch,
                        text=True,
                        capture_output=True,
                        cwd=working,
                        timeout=10,
                        check=False,
                    )
                    if applied.returncode:
                        raise ValueError("patch did not apply")
                    checked = subprocess.run(
                        task["test_command"], cwd=working, capture_output=True, timeout=timeout, check=False
                    )
                    succeeded = checked.returncode == 0
                except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
                    error = type(exc).__name__
            execution_ms = (time.perf_counter() - started) * 1000
            lifecycle_started = time.perf_counter()
            outcome = succeeded if error is None else None
            retirement_tracker.observe(task, index, randomized, outcome, error)
            lifecycle_ms = assignment_ms + (time.perf_counter() - lifecycle_started) * 1000
            if randomized and outcome is not None:
                observation = experiment.HoldoutObservation(
                    task["id"], str(index), injected, outcome, rate=0.5
                )
                observations.append(observation)
            rows.append(
                {
                    "occasion": index,
                    "task": task["id"],
                    "injected": injected,
                    "randomized": randomized,
                    "succeeded": outcome,
                    "execution_error": error,
                    "lifecycle_status": retirement_tracker.status(task),
                    "elapsed_ms": execution_ms,
                    "lifecycle_ms": lifecycle_ms,
                    "usage": usage,
                    "cost_usd": cost,
                    "label_relevant": task.get("lesson_relevant") if type(task.get("lesson_relevant")) is bool else None,
                    "label_harmful": task.get("lesson_harmful") if type(task.get("lesson_harmful")) is bool else None,
                }
            )
        effects = experiment.analyze(
            observations,
            sequential=True,
            alpha=_UPLIFT_ALPHA / len(tasks),
            target_n_per_arm=target_n_by_task,
        )
        observed_effects = {item.lesson_slug: asdict(item) for item in effects}
        effects_by_task = []
        for task in tasks:
            effect_row = observed_effects.get(task["id"])
            if effect_row is not None:
                effects_by_task.append(effect_row)
                continue
            assignment_rows = [
                row for row in rows if row["task"] == task["id"] and row["randomized"]
            ]
            effects_by_task.append({
                "lesson_slug": task["id"],
                "planned_occasions": planned_slots[task["id"]],
                "target_n_per_arm": target_n_by_task[task["id"]],
                "n_injected": 0,
                "n_withheld": 0,
                "rate_injected": 0.0,
                "rate_withheld": 0.0,
                "effect": None,
                "ci_low": None,
                "ci_high": None,
                "p_value": None,
                "significant": False,
                "min_detectable_effect": None,
                "verdict": experiment.VERDICT_UNDERPOWERED,
                "note": "No resolved randomized outcome for this task.",
                "unresolved_randomized_occasions": sum(
                    row["succeeded"] is None for row in assignment_rows
                ),
            })
        effect = observed_effects.get(tasks[0]["id"]) if len(tasks) == 1 else None
        aggregate = _stratified_uplift(
            tasks,
            rows,
            effects_by_task,
            normalized_weights,
            planned_slots,
            weighting="explicit_task_weights" if task_weights is not None else "round_robin_schedule",
        )
        uplift_gate_evaluated = aggregate["evaluated"]
        labelled = [row for row in rows if row["injected"] and row["label_relevant"] is not None]
        precision = sum(row["label_relevant"] for row in labelled) / len(labelled) if labelled else None
        priced = [row for row in rows if row["cost_usd"] is not None]
        retirement = retirement_tracker.summary(tasks)
        return {
            "benchmark": "CodingLessonBench-v1",
            "seed": seed,
            "occasions": occasions,
            "rows": rows,
            "task_manifest_sha256": hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest(),
            "causal_effect": effect,
            "causal_effects_by_task": effects_by_task,
            "causal_effect_aggregate": aggregate,
            "causal_effect_scope": "single_task" if len(tasks) == 1 else "weighted_task_stratified",
            "injection_precision": precision,
            "labelled_injections": len(labelled),
            "cost": {
                "known_usd": sum(row["cost_usd"] for row in priced),
                "unpriced_occasions": len(rows) - len(priced),
                "tokens": {key: sum(row["usage"].get(key, 0) for row in rows) for key in ("input_tokens", "output_tokens")},
            },
            **retirement,
            "precision_gate_met": precision is not None and precision >= 0.8,
            "uplift_gate_evaluated": uplift_gate_evaluated,
            "uplift_gate_met": bool(
                uplift_gate_evaluated
                and aggregate["effect_unrounded"] >= _UPLIFT_THRESHOLD
                and aggregate["ci_low"] > 0
            ),
            "uplift_gate_reason": (
                aggregate["reason"]
                if not uplift_gate_evaluated
                else None
                if aggregate["effect_unrounded"] >= _UPLIFT_THRESHOLD and aggregate["ci_low"] > 0
                else "The weighted task-average estimate did not meet the existing +5 pp and positive lower-CI gate."
            ),
            "limitations": [
                "Results apply only to the supplied task distribution and runner.",
                "No inference that an unlabelled fixture is SWE-bench.",
                "Precision requires explicit task labels; unpriced calls are never counted as free.",
                "Unretired harmful lessons are censored at run end; only a journal retirement transition reports N.",
            ],
        }
    finally:
        retirement_tracker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument(
        "--model", required=True, help="Actual runner/model identity, including deterministic fixture labels."
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--task-weights",
        help="Optional JSON object mapping every task id to a positive target-population weight; defaults to round-robin schedule weights.",
    )
    parser.add_argument("--occasions", type=int, default=100)
    parser.add_argument("--seed", type=int, default=61923)
    args = parser.parse_args()
    with open(args.tasks, encoding="utf-8") as stream:
        tasks = json.load(stream)
    task_weights = None
    if args.task_weights:
        with open(args.task_weights, encoding="utf-8") as stream:
            task_weights = json.load(stream)
    result = run(
        tasks,
        shlex.split(args.runner),
        occasions=args.occasions,
        seed=args.seed,
        task_weights=task_weights,
    )
    result["runner_model"] = args.model
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
