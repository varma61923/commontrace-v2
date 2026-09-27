"""The local `retrieve` tool carries each lesson's measured causal evidence.

The Hub's search does (hub/tests/test_search_evidence.py); a fleet on the
local store gets the same answer from the same kind of analysis. These
pin that the verdict on a retrieved lesson is the one `experiment_status`
reports, that a compromised experiment shows no numbers, that a store with
no experiment gets no extra fields, and that retrieving during an
experiment does not recompute the analysis on every call.
"""
from __future__ import annotations

import pytest

from commontrace import evidence, holdout_io, mcp_server
from tests.test_mcp_server import _curate, call, cli

pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")

TASK = "password reset email"


@pytest.fixture(autouse=True)
def _fresh_cache():
    evidence._cache.clear()
    yield
    evidence._cache.clear()


@pytest.fixture
def fleet(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0
    server = mcp_server.build_server(root)
    slug = _curate(server)
    return root, server, slug


def _run(server, root, slug, n, *, report):
    """Retrieve for n occasions and report outcomes by occasion id.
    `report(i, injected)` returns True/False to record, or None to skip."""
    for i in range(n):
        occ = f"occ-{i}"
        out = call(server, "retrieve", task=TASK, occasion_id=occ)
        injected = any(item["slug"] == slug for item in out["lessons"])
        outcome = report(i, injected)
        if outcome is not None:
            holdout_io.record_outcome(root, occ, outcome)


def _lesson(out, slug):
    for name in ("lessons", "withheld"):
        for item in out.get(name) or []:
            if item["slug"] == slug:
                return item
    raise AssertionError(f"{slug} not in the retrieval")


def test_no_experiment_means_no_extra_fields(fleet):
    root, server, slug = fleet
    out = call(server, "retrieve", task=TASK)
    assert "evidence" not in out
    assert all("evidence" not in item for item in out["lessons"])


def test_a_lesson_that_helps_says_so_and_agrees_with_experiment_status(fleet):
    root, server, slug = fleet
    holdout_io.configure(root, rate=0.5)
    # Seeded ground truth: the task succeeds exactly when the lesson was used.
    _run(server, root, slug, 120, report=lambda i, injected: injected)

    out = call(server, "retrieve", task=TASK)
    item = _lesson(out, slug)
    assert out["evidence"]["available"] is True
    assert item["evidence"]["verdict"] == "HELPS"
    assert item["evidence"]["effect"] > 0.5

    status = call(server, "experiment_status")
    expected = {e["lesson_slug"]: e["verdict"] for e in status["effects"]}
    assert item["evidence"]["verdict"] == expected[slug]


def test_a_compromised_experiment_shows_no_numbers(fleet):
    """Every injected occasion reported, most withheld ones not: the effect
    is biased by a named mechanism, so only the reason is shown."""
    root, server, slug = fleet
    holdout_io.configure(root, rate=0.5)
    _run(server, root, slug, 120,
         report=lambda i, injected: (i % 2 == 0) if (injected or i % 4 == 0) else None)

    status = call(server, "experiment_status")
    assert status["integrity"]["effects_readable"] is False
    out = call(server, "retrieve", task=TASK)
    assert out["evidence"]["available"] is False
    assert "COMPROMISED" in out["evidence"]["reason"]
    assert "evidence" not in _lesson(out, slug)


def test_retrieving_during_an_experiment_does_not_recompute_each_time(fleet, monkeypatch):
    """`retrieve` with an occasion_id appends to the holdout log, so a cache
    keyed on that log rebuilt the analysis on every retrieval of a store
    running an experiment. Effects come from resolved occasions only."""
    root, server, slug = fleet
    holdout_io.configure(root, rate=0.5)
    _run(server, root, slug, 30, report=lambda i, injected: injected)

    calls = {"n": 0}
    real = evidence.analyse

    def counted(r):
        calls["n"] += 1
        return real(r)

    monkeypatch.setattr(evidence, "analyse", counted)
    for i in range(5):
        call(server, "retrieve", task=TASK, occasion_id=f"live-{i}")
    assert calls["n"] == 1

    holdout_io.record_outcome(root, "live-0", True)
    call(server, "retrieve", task=TASK)
    assert calls["n"] == 2, "a newly recorded outcome must invalidate the evidence"
