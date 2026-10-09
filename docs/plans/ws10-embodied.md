# WS10: Embodied and multimodal fleets

**Problem.** Robots need memory that is measured per environment, never pools simulation with reality, and never randomizes safety.

**Design.** Multimodal episodes through `/v1/episode` with content-addressed media (`episodes.py`); protected `safety:` memories; fleet adoption and merge with sim/real refusal (`fleet.py`); the robot fleet demo (`benchmarks/robot_fleet_demo.py`).

**Invariants touched.** Protected memories are always delivered and never get a verdict. Merging stores from different environments is refused.

**Tests.** `tests/test_episodes.py`, `tests/test_robot_fleet_demo.py`, `tests/test_fleet*.py`.

**Benchmark to move.** Harm detection on the real fleet and on-robot recall latency (measured p95 0.75 ms against a 25 ms budget).

**Risks.** Seeded simulation only; no physical robots measured.

**Rollback.** Episodes are ordinary traces and can be forgotten like any other.
