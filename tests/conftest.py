import os
import sys

import pytest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "commontrace", "reference"))

os.environ.setdefault("COMMONTRACE_DEFAULT_RERANK", "none")
os.environ.setdefault("COMMONTRACE_WARM", "0")


def pytest_collection_modifyitems(items):
    """Classify existing contracts without moving files or changing their execution."""
    integration = {"test_optional_memory_backends.py", "test_new_framework_sdks.py", "test_vector_store.py",
                   "test_vector_engines.py", "test_postgres_vector_snapshots.py"}
    for item in items:
        path = str(item.path).replace(os.sep, "/")
        if "/e2e/" in path or "/e2e_tests/" in path or path.endswith("test_ui_browser.py"):
            item.add_marker(pytest.mark.e2e)
        elif os.path.basename(path) in integration or "postgres" in os.path.basename(path):
            item.add_marker(pytest.mark.integration)
        elif not any(item.get_closest_marker(name) for name in ("unit", "integration", "e2e")):
            item.add_marker(pytest.mark.unit)


def _write_frontmatter_file(path, fm, body):
    content = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def tmp_memory(tmp_path):
    mem = tmp_path / "memory"
    (mem / "lessons").mkdir(parents=True)
    (mem / "episodes").mkdir(parents=True)
    (mem / "benchmark_reports").mkdir(parents=True)
    return mem


def write_lesson(mem_dir, name, description="A test lesson", domain="testing",
                 importance=3, uses=0, last_hit="NEVER", source_episodes=None,
                 source_traces=None, agent_type="code",
                 status="active", rule="Apply this rule.", tags=None):
    fm = {
        "name": name,
        "description": description,
        "tags": tags or ["test"],
        "agent_type": agent_type,
        "domain": domain,
        "importance": importance,
        "importance_rationale": "Test lesson rationale.",
        "importance_history": [],
        "applies_when": "When running a test suite against the example.",
        "do_not_apply_when": "When no test suite is present.",
        "uses": uses,
        "last_hit": last_hit,
        "source_traces": source_traces if source_traces is not None else (source_episodes or []),
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
