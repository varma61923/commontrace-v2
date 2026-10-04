"""Tests for Sagas: ordered incident/migration narratives with watermarked running briefs."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from commontrace import sagas

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


def test_saga_crud(store):
    # Create
    saga = sagas.create_saga(
        store, "db-v16", "Postgres 16 Upgrade",
        tags=["database", "migration"],
        brief="Planning phase started",
    )
    assert saga.id == "db-v16"
    assert saga.status == "active"
    assert saga.watermarked_running_brief == "Planning phase started"

    # Duplicate create raises
    with pytest.raises(sagas.SagaError):
        sagas.create_saga(store, "db-v16", "Duplicate")

    # Get
    fetched = sagas.get_saga(store, "db-v16")
    assert fetched is not None
    assert fetched.title == "Postgres 16 Upgrade"

    # Append events
    sagas.append_saga_event(
        store, "db-v16", "Snapshot taken",
        description="RDS snapshot snapped before migration",
        actor="dba_dave",
    )
    sagas.append_saga_event(
        store, "db-v16", "Instance upgraded",
        description="Engine bumped from 15 to 16",
        actor="dba_dave",
        new_brief="Engine upgraded; validating extensions",
        watermark="step-engine-upgraded",
    )

    updated = sagas.get_saga(store, "db-v16")
    assert updated is not None
    assert len(updated.events) == 2
    assert updated.watermark == "step-engine-upgraded"

    # Resolve
    resolved = sagas.resolve_saga(store, "db-v16", final_brief="Migration successful and verified")
    assert resolved.status == "resolved"
    assert resolved.watermarked_running_brief == "Migration successful and verified"

    # Search
    results = sagas.search_sagas(store, "Postgres")
    assert len(results) == 1
    assert results[0].id == "db-v16"

    # Delete
    assert sagas.delete_saga(store, "db-v16") is True
    assert sagas.get_saga(store, "db-v16") is None


def test_saga_cli_commands(store):
    res_create = cli("saga", "create", "inc-99", "Network Partition Incident",
                     "--tags", "net", "p0", "--brief", "Packet loss detected", "--dest", store)
    assert res_create.returncode == 0
    assert "Created saga 'inc-99'" in res_create.stdout

    res_append = cli("saga", "append", "inc-99", "BGP Route Fixed",
                     "--desc", "Switched to secondary transit provider", "--dest", store)
    assert res_append.returncode == 0
    assert "Appended event to 'inc-99'" in res_append.stdout

    res_get = cli("saga", "get", "inc-99", "--dest", store)
    assert res_get.returncode == 0
    assert "Network Partition Incident" in res_get.stdout
    assert "BGP Route Fixed" in res_get.stdout

    res_list = cli("saga", "list", "--dest", store)
    assert res_list.returncode == 0
    assert "inc-99" in res_list.stdout
