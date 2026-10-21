"""The paired summary workload must preserve output and restore production hooks."""
import pytest

from benchmarks.session_summary import main, run
from unimem.conversation import summary


def test_workload_checks_complete_persisted_summary_equality():
    selector = summary._select_candidates
    report = run(turns=30, trials=2)
    assert report["equivalent"]
    assert len(set(report["fingerprints"].values())) == 1
    assert all(len(rows) == 2 for rows in report["samples_ms"].values())
    assert summary._select_candidates is selector


def test_workload_rejects_unbounded_inputs():
    with pytest.raises(ValueError):
        run(turns=0)
    with pytest.raises(SystemExit):
        main(["--turns", "20001"])
