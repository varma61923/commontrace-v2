from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class CLIResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            msg = f"Failed to parse CLI output as JSON.\nSTDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
            raise ValueError(msg) from exc

    def assert_success(self, msg: str = "") -> CLIResult:
        if self.returncode != 0:
            details = f"\nCMD: {' '.join(self.command)}\nSTDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
            if msg:
                details = f"{msg}\n{details}"
            raise AssertionError(f"CLI command failed with exit code {self.returncode}: {details}")
        return self

    def assert_failure(self, expected_code: int | None = None, msg: str = "") -> CLIResult:
        if self.returncode == 0:
            details = f"\nCMD: {' '.join(self.command)}\nSTDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
            if msg:
                details = f"{msg}\n{details}"
            raise AssertionError(f"CLI command unexpectedly succeeded (code 0): {details}")
        if expected_code is not None and self.returncode != expected_code:
            raise AssertionError(
                f"CLI command returned code {self.returncode}, expected {expected_code}.\nSTDERR:\n{self.stderr}"
            )
        return self


def run_cli(
    *argv: str,
    dest: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> CLIResult:
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = f"{REPO_ROOT}:{full_env.get('PYTHONPATH', '')}"
    if env:
        full_env.update(env)

    cmd = [sys.executable, "-m", "commontrace.cli"]
    cmd.extend(argv)

    if dest:
        if "--dest" not in argv:
            cmd.extend(["--dest", dest])

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
        env=full_env,
        timeout=timeout,
        check=False,
    )

    return CLIResult(
        command=cmd,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def is_milestone_implemented(milestone: str) -> bool:
    m = milestone.upper()
    if m == "M1":
        return bool(
            importlib.util.find_spec("commontrace.graph")
            and importlib.util.find_spec("commontrace.hierarchical")
            and importlib.util.find_spec("commontrace.memory_blocks")
        )
    elif m == "M2":
        return bool(
            importlib.util.find_spec("commontrace.ingest")
            or importlib.util.find_spec("commontrace.commands.ingest_cmd")
        )
    elif m == "M3":
        return bool(
            importlib.util.find_spec("commontrace.agent_loop")
            or importlib.util.find_spec("commontrace.commands.agent_cmd")
        )
    elif m == "M4":
        return bool(
            importlib.util.find_spec("hub.crud")
            and importlib.util.find_spec("hub.rest")
        )
    return False


def require_milestone(milestone: str) -> None:
    if not is_milestone_implemented(milestone):
        pytest.skip(
            f"[PROGRESSIVE TESTABILITY] Milestone {milestone.upper()} is not yet implemented. "
            f"This test will automatically run once {milestone.upper()} components are compiled."
        )
