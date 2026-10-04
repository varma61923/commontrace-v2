"""Tests for Knowledge Pages: curated pages with dry-run diffs and version history."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import knowledge_pages

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "commontrace.cli", *argv],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "coding").returncode == 0
    return root


def test_knowledge_page_lifecycle(store):
    content_v1 = "# Architecture Guide\n\nAll services communicate via gRPC."
    res1 = knowledge_pages.update_page(
        store, "guide/arch", content_v1, title="Architecture Guide", tags=["architecture", "backend"],
    )
    assert res1["version"] == 1
    assert res1["dry_run"] is False

    page = knowledge_pages.get_page(store, "guide/arch")
    assert page is not None
    assert page.version == 1
    assert "gRPC" in page.content

    # Dry-run diff
    content_v2 = "# Architecture Guide\n\nAll services communicate via gRPC and NATS JetStream."
    preview = knowledge_pages.update_page(
        store, "guide/arch", content_v2, dry_run=True,
    )
    assert preview["dry_run"] is True
    assert preview["changed"] is True
    assert preview["current_version"] == 1
    assert preview["proposed_version"] == 2
    assert "+All services communicate via gRPC and NATS JetStream." in preview["diff"]

    # Verify no mutation occurred on disk
    page_unchanged = knowledge_pages.get_page(store, "guide/arch")
    assert page_unchanged.version == 1
    assert "NATS" not in page_unchanged.content

    # Commit update with version check
    res2 = knowledge_pages.update_page(
        store, "guide/arch", content_v2, expected_version=1, comment="Add NATS JetStream messaging",
    )
    assert res2["version"] == 2

    # Check version history
    history = knowledge_pages.page_history(store, "guide/arch")
    assert len(history) == 2
    assert history[0]["version"] == 1
    assert history[1]["version"] == 2

    # Search
    search_hits = knowledge_pages.search_pages(store, "JetStream")
    assert len(search_hits) == 1
    assert search_hits[0].slug == "guide/arch"


def test_knowledge_page_cli_commands(store):
    res_set = cli("page", "set", "playbook/deploy", "1. Run tests\n2. Push image",
                  "--title", "Deploy Playbook", "--tags", "deploy", "ci", "--dest", store)
    assert res_set.returncode == 0
    assert "Saved page 'playbook/deploy' (v1)" in res_set.stdout

    res_diff = cli("page", "diff", "playbook/deploy", "1. Run tests\n2. Push image\n3. Smoke test", "--dest", store)
    assert res_diff.returncode == 0
    assert "Dry-run diff for 'playbook/deploy'" in res_diff.stdout
    assert "+3. Smoke test" in res_diff.stdout

    res_get = cli("page", "get", "playbook/deploy", "--dest", store)
    assert res_get.returncode == 0
    assert "Deploy Playbook" in res_get.stdout
    assert "1. Run tests" in res_get.stdout

    res_list = cli("page", "list", "--dest", store)
    assert res_list.returncode == 0
    assert "playbook/deploy" in res_list.stdout
