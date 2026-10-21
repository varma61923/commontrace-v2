from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from unimem import frontier_protocol as fp
from unimem.robotics import signatures


def samples(seed: int = 7, *, count: int = 240, shift: float = 0):
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        firmware = "fw-a" if index < count // 2 else "fw-b"
        drift = 0 if firmware == "fw-a" else shift
        rows.append({"episode_id": f"episode-{index}", "timestamp": (start + timedelta(seconds=index)).isoformat(),
            "phase": "approach", "payload": "cup-200g", "firmware": firmware, "robot": "robot-7",
            "signals": {"joint_current": 1.0 + drift + rng.gauss(0, .1),
                        "force_torque": [2.0 + drift + rng.gauss(0, .1), 3.0 + drift + rng.gauss(0, .1)],
                        "tactile": 4.0 + rng.gauss(0, .1), "temperature": 28 + rng.gauss(0, .1)}})
    return rows


def test_quantile_band_false_alarm_and_planted_multichannel_anomaly(tmp_path):
    root = str(tmp_path)
    baseline = samples()
    signatures.build(root, baseline, skill="place-cup", embodiment="arm-v4", site="depot",
                     alpha=.05, minimum_samples=30)
    rng = random.Random(44)
    false_alarms = 0
    for index in range(200):
        sample = {**baseline[0], "episode_id": f"holdout-{index}",
                  "timestamp": fp.now(), "signals": {"joint_current": 1.0 + rng.gauss(0, .1)}}
        false_alarms += signatures.check(root, sample, skill="place-cup", embodiment="arm-v4",
                                          site="depot", record_anomaly=False)["anomaly"]
    assert false_alarms <= 20

    planted = {**baseline[0], "episode_id": "episode-anomaly", "timestamp": fp.now(),
               "signals": {"joint_current": 5.0, "force_torque": [2.0, 3.0],
                           "tactile": 4.0, "temperature": 28.0}}
    result = signatures.check(root, planted, skill="place-cup", embodiment="arm-v4", site="depot")
    assert result["anomaly"] is True
    assert result["memory_id"] and result["recovery_id"]
    assert result["event_time"] == fp.moment(planted["timestamp"])


def test_firmware_drift_is_found_at_correct_boundary(tmp_path):
    root = str(tmp_path)
    signatures.build(root, samples(shift=1.5), skill="place-cup", embodiment="arm-v4", site="depot",
                     alpha=.05, minimum_samples=30)
    rows = signatures.drift(root, skill="place-cup", embodiment="arm-v4", site="depot",
                            sensor="joint_current", since_firmware="fw-a", minimum_shift=.75)
    assert rows and all(row["firmware"] == "fw-b" and row["shift"] > 1.0 for row in rows)
    assert all(row["valid_from"] and row["recorded_at"] for row in rows)


def test_downsampled_edge_evaluation_is_bounded(tmp_path):
    root = str(tmp_path)
    baseline = samples(count=100)
    signatures.build(root, baseline, skill="place-cup", embodiment="arm-v4", site="depot",
                     alpha=.05, minimum_samples=30)
    start = time.perf_counter()
    for index in range(100):
        sample = {**baseline[index], "episode_id": f"edge-{index}"}
        signatures.check(root, sample, skill="place-cup", embodiment="arm-v4", site="depot",
                         record_anomaly=False)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0  # 100 downsampled, local checks; no network or model inference.
