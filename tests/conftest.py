"""Shared pytest fixtures for commontrace-v2 tests."""
import os
import sys
import tempfile
import shutil

import pytest

# Ensure scripts can be imported without installing as a package
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "benchmark"))
sys.path.insert(0, os.path.join(REPO_ROOT, "memory", "attention"))


LESSON_TEMPLATE = """\
---
name: {name}
description: {description}
tags: {tags}
domain: {domain}
importance: {importance}
importance_rationale: "Test lesson rationale."
importance_history: []
applies_when: When running a test suite against the example.
do_not_apply_when: When no test suite is present.
uses: {uses}
last_hit: {last_hit}
source_episodes: {source_episodes}
status: {status}
---

## Rule
{rule}

## Why
For testing purposes only.

## How to apply
Add to the A brief.

## Counter-examples
N/A in tests.
"""

EPISODE_TEMPLATE = """\
---
name: {name}
description: {description}
task_invocation: /commontrace {task}
tags: {tags}
project: {project}
verdict: {verdict}
importance: {importance}
importance_rationale: "Test episode rationale."
n_iterations: 1
commit_sha: abc1234
duration_minutes: 10
lessons_retrieved_by_alpha: {retrieved}
lessons_hit: {hit}
lessons_proposed_by_omega: {proposed}
lessons_validated_by_lambda: {validated}
---

## What happened
Test episode.

## What surprised me
Nothing.

## What worked well
- Everything.

## What worked less well
- Nothing.
"""


@pytest.fixture
def tmp_memory(tmp_path):
    """Create a temporary memory directory with lessons and episodes subdirs."""
    mem = tmp_path / "memory"
    (mem / "lessons").mkdir(parents=True)
    (mem / "episodes").mkdir(parents=True)
    (mem / "benchmark_reports").mkdir(parents=True)
    return mem


def write_lesson(mem_dir, name, description="A test lesson", domain="testing",
                 importance=3, uses=0, last_hit="NEVER", source_episodes=None,
                 status="active", rule="Apply this rule.", tags=None):
    tags = tags or ["test"]
    src_eps = source_episodes or []
    content = LESSON_TEMPLATE.format(
        name=name,
        description=description,
        tags=str(tags),
        domain=domain,
        importance=importance,
        uses=uses,
        last_hit=last_hit,
        source_episodes=str(src_eps),
        status=status,
        rule=rule,
    )
    path = mem_dir / "lessons" / f"{name}.md"
    path.write_text(content)
    return path


def write_episode(mem_dir, name, description="A test episode", project="test-project",
                  verdict="CONFORM", importance=3, task="do something",
                  retrieved=None, hit=None, proposed=None, validated=None, tags=None):
    tags = tags or ["test"]
    content = EPISODE_TEMPLATE.format(
        name=name,
        description=description,
        task=task,
        tags=str(tags),
        project=project,
        verdict=verdict,
        importance=importance,
        retrieved=str(retrieved or []),
        hit=str(hit or []),
        proposed=str(proposed or []),
        validated=str(validated or []),
    )
    path = mem_dir / "episodes" / f"{name}.md"
    path.write_text(content)
    return path
