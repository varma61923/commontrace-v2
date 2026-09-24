"""E2E Test Suite Fixtures and Test Helpers for CommonTrace v2.

Provides isolated environments, CLI invocation helpers, and synthetic test
data generators for opaque-box testing across all 19 features.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml


@dataclass
class CLIResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@pytest.fixture
def cli_runner() -> Callable[..., CLIResult]:
    """Execute commontrace CLI as an opaque subprocess."""
    def _run(
        *args: Any,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> CLIResult:
        merged_env = os.environ.copy()
        merged_env["PYTHONUTF8"] = "1"
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        existing_pp = merged_env.get("PYTHONPATH", "")
        merged_env["PYTHONPATH"] = f"{repo_root}:{existing_pp}" if existing_pp else repo_root
        if env:
            merged_env.update(env)

        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            cli_args = [str(a) for a in args[0]]
        elif len(args) >= 1 and isinstance(args[0], Path):
            if cwd is None:
                cwd = args[0]
            cli_args = [str(a) for a in args[1:]]
        else:
            cli_args = [str(a) for a in args]

        cmd = [sys.executable, "-m", "commontrace.cli", *cli_args]
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return CLIResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    return _run


@pytest.fixture
def isolated_store(tmp_path: Path, cli_runner: Callable[..., CLIResult]) -> Path:
    """Provide a freshly initialized, isolated commontrace store directory."""
    store_dir = tmp_path / "workspace_store"
    store_dir.mkdir(parents=True, exist_ok=True)
    res = cli_runner(["init", "--dest", str(store_dir), "--agent-type", "code"])
    assert res.exit_code == 0, f"Failed to init isolated store: {res.stderr}"
    return store_dir


@pytest.fixture
def lesson_factory() -> Callable[..., Path]:
    """Helper to generate a valid lesson markdown file in a store."""
    def _create(
        store: Path,
        slug: str,
        title: str = "Test Lesson Title",
        description: str = "Test lesson description covering best practices.",
        applies_when: str = "When performing system tasks",
        domain: str = "testing",
        importance: int = 3,
        status: str = "active",
        agent_type: str = "code",
        body: str = "## Rule\nAlways write tests before code.\n",
        extra_fm: dict[str, Any] | None = None,
    ) -> Path:
        lessons_dir = store / "memory" / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)
        file_path = lessons_dir / f"{slug}.md"
        fm = {
            "name": slug,
            "title": title,
            "description": description,
            "applies_when": applies_when,
            "do_not_apply_when": "When explicitly not applicable",
            "domain": domain,
            "importance": importance,
            "importance_rationale": "Justification for importance score.",
            "status": status,
            "agent_type": agent_type,
            "uses": 1,
            "last_hit": "2026-09-20",
            "tags": [domain, "e2e"],
        }

        if extra_fm:
            fm.update(extra_fm)
        content = f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n{body}\n"
        file_path.write_text(content, encoding="utf-8")
        return file_path

    return _create


@pytest.fixture
def trace_factory() -> Callable[..., Path]:
    """Helper to generate a valid trace markdown file in a store."""
    def _create(
        store: Path,
        trace_id: str = "2026-09-20_test-trace-001",
        title: str = "Test Trace Title",
        context: str = "Problem context encountered during execution.",
        solution: str = "Solution implemented that resolved the error.",
        agent_type: str = "code",
        agent_id: str = "agent-alpha-01",
        tags: list[str] | None = None,
        outcome: dict[str, Any] | None = None,
        extra_fm: dict[str, Any] | None = None,
    ) -> Path:
        traces_dir = store / "memory" / "traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        file_path = traces_dir / f"{trace_id}.md"
        fm = {
            "id": trace_id,
            "title": title,
            "context_text": context,
            "solution_text": solution,
            "agent_type": agent_type,
            "agent_id": agent_id,
            "created_at": "2026-09-20T01:00:00Z",
            "tags": tags or ["testing", "e2e"],
        }
        if outcome is not None:
            fm["outcome"] = outcome
        if extra_fm:
            fm.update(extra_fm)
        content = (
            f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n"
            f"## Context\n{context}\n\n## Solution\n{solution}\n"
        )
        file_path.write_text(content, encoding="utf-8")
        return file_path

    return _create


@pytest.fixture
def episode_factory() -> Callable[..., Path]:
    """Helper to generate a valid episode markdown file in a store for benchmark tests."""
    def _create(
        store: Path,
        name: str = "2026-07-01_test-episode",
        agent_type: str = "code",
        project: str = "demo-project",
        verdict: str = "CONFORM",
        importance: int = 3,
        lessons_retrieved: list[str] | None = None,
        lessons_hit: list[str] | None = None,
        lessons_proposed: list[str] | None = None,
        lessons_validated: list[str] | None = None,
    ) -> Path:
        episodes_dir = store / "memory" / "episodes"
        episodes_dir.mkdir(parents=True, exist_ok=True)
        file_path = episodes_dir / f"{name}.md"
        fm = {
            "name": name,
            "description": "Episode demonstration run",
            "agent_type": agent_type,
            "task_invocation": "/commontrace test run",
            "tags": ["testing", "example"],
            "project": project,
            "verdict": verdict,
            "importance": importance,
            "importance_rationale": "Anchor for benchmark tests.",
            "n_iterations": 1,
            "commit_sha": "0000000",
            "duration_minutes": 10,
            "lessons_retrieved_by_alpha": lessons_retrieved or [],
            "lessons_hit": lessons_hit or [],
            "lessons_proposed_by_omega": lessons_proposed or [],
            "lessons_validated_by_lambda": lessons_validated or [],
        }
        content = (
            f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n"
            "## What happened\nExecution completed successfully.\n\n"
            "## What surprised me\nClean pass.\n\n"
            "## What worked well\nEverything worked well.\n\n"
            "## What worked less well\nNothing to report.\n"
        )
        file_path.write_text(content, encoding="utf-8")
        return file_path

    return _create

