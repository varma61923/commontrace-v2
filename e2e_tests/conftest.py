from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from e2e_tests.harness.store_fixtures import create_test_store  # noqa: E402


@pytest.fixture
def isolated_store(tmp_path: Path) -> str:
    store_dir = tmp_path / "fleet"
    return create_test_store(store_dir, agent_type="coding")


@pytest.fixture
def coding_store(isolated_store: str) -> str:
    return isolated_store
