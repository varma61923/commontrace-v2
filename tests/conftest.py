"""Shared pytest fixtures for commontrace-v2 tests."""
import os
import sys

import pytest
import yaml

# The benchmark reference scripts are run as subprocesses in production, so they
# are not importable as `commontrace.reference.*` (that directory deliberately has
# no __init__.py -- it ships as package data). Tests import them directly, so put
# their directory on the path.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "commontrace", "reference"))
sys.path.insert(0, os.path.join(REPO_ROOT, "memory", "attention"))


def _write_frontmatter_file(path, fm, body):
    # Use yaml.safe_dump -- same YAML-writing path production's frontmatter.write() uses --
    # rather than interpolating raw values into a string template. A naive template lets
    # adversarial content (a description containing ": " or a quote) produce invalid or
    # silently-misparsed YAML that no longer matches what the fixture's caller intended.
    content = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body
    path.write_text(content, encoding="utf-8")


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
    fm = {
        "name": name,
        "description": description,
        "tags": tags or ["test"],
        "domain": domain,
        "importance": importance,
        "importance_rationale": "Test lesson rationale.",
        "importance_history": [],
        "applies_when": "When running a test suite against the example.",
        "do_not_apply_when": "When no test suite is present.",
        "uses": uses,
        "last_hit": last_hit,
        "source_episodes": source_episodes or [],
        "status": status,
    }
    body = (
        f"## Rule\n{rule}\n\n"
        "## Why\nFor testing purposes only.\n\n"
        "## How to apply\nAdd to the A brief.\n\n"
        "## Counter-examples\nN/A in tests.\n"
    )
    path = mem_dir / "lessons" / f"{name}.md"
    _write_frontmatter_file(path, fm, body)
    return path


def write_episode(mem_dir, name, description="A test episode", project="test-project",
                  verdict="CONFORM", importance=3, task="do something",
                  retrieved=None, hit=None, proposed=None, validated=None, tags=None):
    fm = {
        "name": name,
        "description": description,
        "task_invocation": f"/commontrace {task}",
        "tags": tags or ["test"],
        "project": project,
        "verdict": verdict,
        "importance": importance,
        "importance_rationale": "Test episode rationale.",
        "n_iterations": 1,
        "commit_sha": "abc1234",
        "duration_minutes": 10,
        "lessons_retrieved_by_alpha": retrieved or [],
        "lessons_hit": hit or [],
        "lessons_proposed_by_omega": proposed or [],
        "lessons_validated_by_lambda": validated or [],
    }
    body = (
        "## What happened\nTest episode.\n\n"
        "## What surprised me\nNothing.\n\n"
        "## What worked well\n- Everything.\n\n"
        "## What worked less well\n- Nothing.\n"
    )
    path = mem_dir / "episodes" / f"{name}.md"
    _write_frontmatter_file(path, fm, body)
    return path
