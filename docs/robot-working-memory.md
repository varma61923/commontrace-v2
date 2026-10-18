# Governed in-loop VLA working memory (G25)

`robotics.working_memory.WorkingMemory` holds bounded vectors and keyframe metadata in process memory. Retrieval performs local cosine ranking, uses a 5 ms default budget, returns the best result found so far and marks overruns. It has no network or persistence calls in the retrieval path. The optional ROS helper exposes add, query and episode-consolidation topics; long-term writes occur only on explicit consolidation.

Eviction uses `retention.dream_budget` with configured utility and expected retrieval, while safety keyframes stay protected even when their combined size exceeds capacity. At episode end, only predictive keyframes are written to canonical memory, with lineage and an append-only audit record. Values and utilities are local heuristics and are not safety guarantees.

The 5 ms figure is a design target. CI measures local p99 retrieval with a generous 10x allowance on its host; robot integrators must benchmark their hardware and vector dimensions. ROS messages are bounded JSON envelopes. The node is a memory service, never a motion controller.
