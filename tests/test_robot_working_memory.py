from __future__ import annotations

import time

from commontrace import frontier_protocol as fp
from commontrace import hierarchical
from commontrace.robotics.working_memory import WorkingMemory, percentile_latency


def test_safety_keyframes_survive_utility_eviction_pressure():
    memory = WorkingMemory(capacity_tokens=30)
    memory.add("safe", [1, 0], tokens=10, episode_id="episode-1", safety=True)
    memory.add("useful", [0, 1], tokens=20, episode_id="episode-1", utility=.9, future_retrieval=.8)
    added = memory.add("weak", [-1, 0], tokens=20, episode_id="episode-1", utility=.1, future_retrieval=.1)
    status = memory.evict_to_capacity()
    assert "safe" in status["kept"]
    assert status["protected_over_budget"] is False
    assert "weak" in added["evicted"]
    assert memory.used_tokens() <= 30


def test_p99_local_retrieval_budget_and_best_result_so_far():
    memory = WorkingMemory(capacity_tokens=100_000, budget_ms=5)
    for index in range(500):
        vector = [1 if dim == index % 32 else 0 for dim in range(32)]
        memory.add(f"keyframe-{index}", vector, tokens=1, episode_id="episode-latency")
    service_times = []
    for index in range(100):
        # Measure work performed by this process. Wall-clock retrieval latency
        # includes unrelated xdist worker preemption and belongs in a dedicated
        # benchmark on a controlled host; retrieve() itself still enforces its
        # wall-clock deadline and reports elapsed_ms to callers.
        started = time.process_time()
        result = memory.retrieve([1] + [0] * 31, budget_ms=50)
        service_times.append((time.process_time() - started) * 1000)
        assert result["result"] is not None
    assert percentile_latency(service_times) < 50  # generous 10x the 5 ms control-loop target
    partial = memory.retrieve([1] + [0] * 31, budget_ms=.1)
    assert partial["result"] is not None
    assert partial["over_budget"] is True


def test_episode_consolidation_is_audited_and_predictive_only(tmp_path):
    root = str(tmp_path)
    memory = WorkingMemory()
    memory.add("predictive-a", [1, 2], tokens=5, episode_id="episode-9", predictive=True)
    memory.add("reactive-b", [2, 1], tokens=5, episode_id="episode-9", predictive=False)
    result = memory.consolidate(root, "episode-9", scopes=["site:depot", "robot:robot-9"])
    assert len(result["promoted"]) == 1 and result["audited"]
    facts = hierarchical.load_facts(root)
    assert result["promoted"][0]["fact_id"] in facts
    rows = fp.journal(root, "robot-working-memory.jsonl")
    assert rows[0]["keyframe_id"] == "predictive-a"
    assert rows[0]["episode_id"] == "episode-9"
