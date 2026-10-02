from __future__ import annotations

from pathlib import Path

from e2e_tests.harness.cli_runner import require_milestone, run_cli
from e2e_tests.harness.store_fixtures import create_sample_code_repo, create_sample_markdown_docs


def test_t3_ingest_code_and_docs_cross_subsystem(tmp_path: Path, isolated_store: str):
    """E2E-T3-CB-1: Ingesting code and documentation populates both facts and graph models (M2)."""
    require_milestone("M2")

    code_dir = create_sample_code_repo(tmp_path)
    docs_dir = create_sample_markdown_docs(tmp_path)

    run_cli("ingest", str(code_dir), "--format", "code", dest=isolated_store).assert_success()
    run_cli("ingest", str(docs_dir), "--format", "markdown", dest=isolated_store).assert_success()

    # Verify graph nodes and atomic facts created
    res_nodes = run_cli("graph", "render", "--format", "json", dest=isolated_store)
    res_nodes.assert_success()
    graph_data = res_nodes.json()
    assert len(graph_data["nodes"]) >= 2

    res_facts = run_cli("fact", "list", dest=isolated_store)
    res_facts.assert_success()
    assert len(res_facts.stdout.splitlines()) >= 1
