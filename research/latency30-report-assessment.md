# Assessment of the second competitor roadmap

The second attached report is design input, not measured evidence. Competitor
commits, licenses, implementation paths and papers are pinned in
[competitor-analysis.md](competitor-analysis.md) and
[algorithm-audit.md](algorithm-audit.md). Its `/tmp/` paths and claims of passing
all tests do not establish the state of this checkout. The detailed assessment
of the six proposed pillars remains in [roadmap-assessment.md](roadmap-assessment.md).

The new 28 ms request budget is a hypothesis. Component p95 values cannot simply
be added to establish a request p95; queueing, tokenization, filtering, serialization,
model loading, corpus preparation and network work also require measurement.
An ANN index does not guarantee logarithmic worst-case search, exact recall or
sub-8-ms latency for arbitrary filters. Quantization requires local weights,
export/runtime support and ranking-quality validation. None of these proposed
numbers are adopted as performance results.

MinHash is not zero knowledge, and an embedding is not a private-set-intersection
protocol. Reported paraphrase recall needs a dataset, precision constraint and
threat model. Adaptive Thompson/LinUCB allocation cannot silently reuse the
current randomized holdout's unweighted confidence intervals. PPR estimates
importance under a graph transition model; it does not produce exact relational
answers or automatically reduce traversal cost. PostgreSQL partitioning by
tenant and timestamp does not by itself provide infinite horizontal capacity.

The immediate implementation focuses on measured production bottlenecks:

- Eliminate TCP delayed-ACK stalls on persistent gateway connections.
- Reuse immutable lesson generations after fully stat-verifying every source.
  The notification-only experiment was rejected because mmap writes can bypass
  notification delivery; authoritative source checks remain on every request.
- Reduce exact lexical scorer allocation and bookkeeping without changing scores,
  ordering, eligibility or context budgets.
- Avoid unrelated edge decoding for node-only graph operations and reduce graph
  setup work where exact behavior can be preserved.

All measurements distinguish warm reads from setup, mutation and cold work. The
30 ms objective applies to specified local memory workloads; arbitrary document
ingestion, large cold corpora, network transit and model inference are measured
separately. Existing temporal, provenance and source-isolation checks remain
required. No external inference service or new mandatory native dependency is
introduced to meet a timing claim.
