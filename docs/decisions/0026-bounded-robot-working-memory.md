# Decision 0026: local bounded working memory

Keep in-loop keyframes in process memory, bound retrieval by a configurable deadline, and return the best local result on overrun. Use the retention utility heuristic for non-safety eviction and keep safety keyframes pinned. Consolidation is explicit, predictive-only and audited. The 5 ms latency is a target verified with a generous CI host multiplier, not a hard real-time guarantee.
