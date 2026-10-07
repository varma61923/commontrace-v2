"""Durable failure-to-lesson jobs over the ordinary guarded CLI pipeline."""
from __future__ import annotations

import math
import subprocess
import sys
from typing import Any

from commontrace import memory_guard
from commontrace.distill import ExtractionPolicy


def distill_job(root: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract review candidates in an isolated, bounded-duration process.

    The durable queue supplies leases, backoff and crash recovery. Retries use
    the same locked source-coverage and candidate-dedup checks as interactive
    distillation. The worker never approves a lesson. Its 480-second deadline
    is shorter than the queue's default 600-second claim lease.
    """
    allowed = {"failed_only", "semantic_dedup", "min_validation_score", "similarity", "min_cluster", "agent_type"}
    if set(payload) - allowed:
        raise ValueError("unsupported distillation job option")
    failed_only = payload.get("failed_only", True)
    semantic_dedup = payload.get("semantic_dedup", False)
    if not isinstance(failed_only, bool) or not isinstance(semantic_dedup, bool):
        raise ValueError("distillation switches must be booleans")
    score, similarity = payload.get("min_validation_score", 0.7), payload.get("similarity", 0.3)
    if (isinstance(score, bool) or not isinstance(score, (int, float))
            or isinstance(similarity, bool) or not isinstance(similarity, (int, float))
            or not math.isfinite(similarity) or not 0 <= similarity <= 1):
        raise ValueError("distillation scores must be finite numbers in [0, 1]")
    ExtractionPolicy(min_validation_score=float(score))
    minimum = payload.get("min_cluster", 2)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 2 <= minimum <= 1000:
        raise ValueError("min_cluster must be an integer between 2 and 1000")
    agent_type = payload.get("agent_type", "")
    if not isinstance(agent_type, str) or len(agent_type) > 128:
        raise ValueError("agent_type must be a string of at most 128 characters")
    argv = [sys.executable, "-m", "commontrace.cli", "distill", "--dest", root, "--extract",
            "--min-validation-score", str(score), "--similarity-threshold", str(similarity),
            "--min-cluster-size", str(minimum)]
    if failed_only:
        argv.append("--failed")
    if semantic_dedup:
        argv.append("--semantic-dedup")
    if agent_type:
        argv.extend(["--agent-type", agent_type])
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=480)
    except subprocess.TimeoutExpired:
        raise RuntimeError("distillation job exceeded its execution deadline") from None
    if completed.returncode:
        raise RuntimeError(f"distillation job failed with exit code {completed.returncode}")
    summary = memory_guard.redact_secrets(completed.stdout)[0][:2000]
    return {"review_only": True, "summary": summary}
