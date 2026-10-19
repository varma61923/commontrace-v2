# Governed in-loop VLA working memory (G25)

`robotics.working_memory.WorkingMemory` holds bounded vectors and keyframe metadata in process memory. Retrieval performs local cosine ranking, uses a 5 ms default budget, returns the best result found so far and marks overruns. It has no network or persistence calls in the retrieval path. The optional ROS helper exposes add, query and episode-consolidation topics; long-term writes occur only on explicit consolidation.

Eviction uses `retention.dream_budget` with configured utility and expected retrieval, while safety keyframes stay protected even when their combined size exceeds capacity. At episode end, only predictive keyframes are written to canonical memory, with lineage and an append-only audit record. Values and utilities are local heuristics and are not safety guarantees.

The 5 ms figure is a design target. Unit tests measure process CPU service time;
the separate `python -m benchmarks.robot_memory --check` gate measures wall-clock
p50/p95/p99 on a quiet process with a generous 10x allowance. Robot integrators
must benchmark their hardware and vector dimensions. Concurrent reads use a
consistent snapshot, lock waits count against the wall-clock budget, and a
protected keyframe cannot be demoted by replacing its ID. ROS messages are bounded
JSON envelopes. The node is a memory service, never a motion controller.
