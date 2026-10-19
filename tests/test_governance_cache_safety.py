from __future__ import annotations

import os
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

from commontrace import conformal_gate, incident, retention, safety, spatial
from commontrace import frontier_protocol as fp


def test_governance_cache_reads_are_consistent_across_threads(tmp_path):
    root = str(tmp_path)

    def read_all():
        return (
            safety.protected(root),
            retention.trials(root),
            conformal_gate.current(root),
            incident.held(root),
            spatial.history(root, context={"tenant": "tenant", "site": "site"}),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: read_all(), range(64)))
    assert all(result == ({}, {}, {"enabled": False}, {}, []) for result in results)


@pytest.mark.skipif(not hasattr(os, "fork") or not hasattr(os, "register_at_fork"),
                    reason="governance cache fork reset requires POSIX at-fork support")
def test_governance_caches_are_cleared_by_registered_fork_callbacks(tmp_path):
    root = os.path.realpath(tmp_path)
    with fp._GUARD:
        spatial._STATES[root] = spatial._Spatial()
        safety._PROTECTED[root] = ((0, ""), {})
        conformal_gate._CURRENT[root] = (None, {"enabled": False})
        retention._TRIALS[root] = ((0, ""), {})
        incident._HELD[root] = ((0, ""), {})

    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        counts = (len(spatial._STATES), len(safety._PROTECTED), len(conformal_gate._CURRENT),
                  len(retention._TRIALS), len(incident._HELD))
        os.write(write_fd, ",".join(map(str, counts)).encode("ascii"))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    child_counts = os.read(read_fd, 64).decode("ascii")
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    with fp._GUARD:
        spatial._STATES.pop(root, None)
        safety._PROTECTED.pop(root, None)
        conformal_gate._CURRENT.pop(root, None)
        retention._TRIALS.pop(root, None)
        incident._HELD.pop(root, None)
    assert status == 0
    assert child_counts == "0,0,0,0,0"
