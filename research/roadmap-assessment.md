# Assessment of the proposed six-pillar roadmap

The attached roadmap contains useful hypotheses, but several numerical,
privacy, complexity, and competitor claims are unsupported. It is considered
design input, not proof. Implementation decisions should preserve provenance,
source isolation, bounded context, valid evaluation, and local operation.
This assessment builds on [algorithm-audit.md](algorithm-audit.md), which pins
all six competitor checkouts, licenses, code paths, and inspected papers.

## Claims requiring correction

| Claim in the attachment | Evidence and corrected interpretation |
|---|---|
| Mem0 ingestion requires remote inference on every write | Pinned `mem0/memory/main.py::_add_to_vector_store` supports `infer=False`; `mem0/utils/factory.py` includes Ollama, LM Studio, and vLLM providers. Extraction commonly uses inference, but remote inference is not mandatory. |
| Competitor vector retrieval uses unbounded top-k | Mem0 validates a nonnegative top-k and requests bounded candidate pools. Returned results being O(k) does not mean search is unbounded. Filtered ANN recall and candidate relevance are separate issues. |
| Graphiti requires a Neo4j cluster and unbounded traversal | Pinned `graphiti_core/driver/driver.py` lists Neo4j, FalkorDB, Kuzu, and Neptune. Search recipes expose BFS depth/limits. Graph construction can be expensive, but the cluster-only claim is false. |
| Hindsight always blocks async requests on temporal CPU work | Its inspected `search/temporal_extraction.py::extract_temporal_constraint_async` explicitly uses an executor. The cost of all request stages still needs measurement. |
| CommonTrace is the only system with causal evaluation | A randomized holdout is a useful product differentiator; the inspected memory repositories do not establish uniqueness across the whole industry. Application-specific governance is also not a substitute for retrieval accuracy. |
| Pollution 1.00–1.06× beats conventional vector systems at 1.89–2.50× | README documents these as CommonTrace's own 144-query lesson-assignment regression and historical scorer comparisons. They are not matched Mem0/Graphiti measurements or long-conversation accuracy. |
| MinHash interchange is zero knowledge | `commontrace/overlap.py` implements deterministic token hashing, public seeded permutations, and approximate Jaccard matching. It does not implement a zero-knowledge protocol. Signatures and clear metadata can reveal information through dictionary/membership/linkage attacks. |
| Dense PSI guarantees over 85% paraphrase recall | No cryptographic protocol, threat model, held-out dataset, precision constraint, or measured result supports that number. Embeddings alone are not PSI. |

Honcho's AGPL source remains reference material only. No competitor implementation
is copied. Cognee is omitted from the attachment's comparison table but is covered
in the pinned audit; its modular graph/text configuration and small-sample paper
results also do not establish universal dominance.

## Pillar 1: ANN, semantic fleet overlap, and quantization

### Vector acceleration

**Present:** compact vectors, batched fixed-order exact scoring, filtered sparse
and dense arms, process-local query-vector reuse, and generation-safe mapped
snapshots for indexes exceeding the heap-cache budget. SQLite remains source of
truth. The retained mapped workload improves the latest baseline 5.41× and the
original measured implementation by a conservative approximately 39×, with all
eight top-ten lists unchanged. See [retrieval-scale.md](retrieval-scale.md).
Exact search remains linear in eligible vectors; mapping removes decoding work,
not the scoring complexity.

**Next experiment:** compare an optional local ANN implementation with exact
scan at 100K/1M vectors, multiple dimensions and realistic embedding distributions.
Measure recall@k, p50/p95/p99 latency, index build/update cost, disk/RSS, cold
starts, metadata selectivity, deletion, crash recovery, and rebuilding from
SQLite. Preserve an exact fallback and source-generation checks. Independent
sparse candidates must remain eligible. An approximate candidate index cannot
promise exact top-k without additional proof or complete fallback.

HNSW's logarithmic search characterization is not a worst-case guarantee for
arbitrary high-dimensional corpora or restrictive filters. Neither `hnswlib`
nor `sqlite-vss` establishes sub-10-ms end-to-end retrieval by being installed.
They introduce native dependency, persistence, portability, and memory costs.
The repository's dependency policy requires an explicit optional integration,
not an unmeasured mandatory replacement.

### Semantic fleet overlap and PSI

Separate two products: approximate semantic matching and cryptographically
protected exchange. A semantic matcher should first be evaluated locally on
disjoint positive paraphrases and unrelated negatives, with precision/recall
curves and thresholds. The optional Arctic arm already improves one official
100K conversation's evidence recall from 68.61% to 74.49%, but that is neither
fleet overlap nor PSI; preparation costs 572 seconds under shared CPU load.
See [retrieval-quality-experiments.md](retrieval-quality-experiments.md).

A privacy design needs defined sets, parties, adversary capabilities, leakage,
security parameters, key management, malicious versus honest execution, and a
specific reviewed protocol. Ordinary PSI computes an intersection of encoded
items; similarity matching of continuous vectors requires additional secure
computation or an explicitly accepted leakage model. Sending embeddings over
TLS, hashing them, or using MinHash is not a zero-knowledge proof. Do not promise
an 85% retrieval result or stronger privacy before separate tests substantiate it.

### ONNX INT8 reranking

`commontrace/rerank_arm.py` already supports local MiniLM and TinyBERT
cross-encoders. No cached cross-encoder weights were available during this audit,
so the proposed 230→35-ms p95 comparison has not been run. An optional ONNX
experiment should time export/loading, tokenize and score the same passage
pairs, evaluate ranking differences on ties/close scores and temporal queries,
and check official retrieval/answer quality at equal budgets. Quantization can
change ranking even if its average numerical error is small. Throughput, thread
counts, batch sizes, sequence lengths, CPU instructions, and warm/cold state must
be specified. Keep the existing backend until that comparison justifies a switch.

## Pillar 2: contextual bandits and causal holdouts

**Present:** `experiment.py` provides deterministic salted random arm assignment,
fixed-horizon analysis and per-lesson anytime confidence sequences.
`holdout_io.assign_and_log` already records assignment, rate, salt, revision,
rank and optional relevance/scorer. The attachment overlooks this existing
assignment-probability record.

A Thompson/LinUCB policy optimizes accumulated reward; it does not automatically
preserve the current causal estimand. Context-dependent treatment probabilities
can make injected and withheld populations differ. Simply pooling their success
counts into the existing intervals can then be invalid. Stable outcomes,
interference between lessons, revision changes, repeated users, outcome delay,
and the definition of an eligible occasion also matter.

**Next experiment:** an isolated simulator using recorded pre-treatment contexts,
logged action propensities, a strictly positive holdout floor, policy version,
seed, candidate set, and explicit reward/estimand. Compare regret and estimator
bias/coverage under stationary and drifting rewards, sparse outcomes, and lesson
interference. Use estimators and sequential uncertainty methods valid for the
adaptive design, rather than silently reusing the existing unweighted arm test.
The suggested 2% and 30% allocations are hypotheses; low holdout rates can make
harm difficult to detect. Keep the current auditable randomized design as a
control and preserve the human activation gate.

## Pillar 3: personalized graph retrieval and coreference

**Present:** `commontrace/graph.py` stores typed relations and separate validity,
creation and expiration fields, supports `as_of`/`known_at`, and bounds hop
depth (default two, maximum four). Its general subgraph API's edge limit is
optional. Conversation recall is a distinct SQLite entity co-occurrence path
with bounded fanout, candidates, and at most two hops. These are different graph
surfaces; adding PPR to one does not automatically improve the other.

Graphiti's paper motivates valid-time versus observed-time modeling and
episode provenance. Hindsight's paper describes typed, decayed activation;
Cognee supports bounded seed-neighborhood projection. These justify graph
retrieval experiments, not a claim that co-occurrence edges identify causal
relationships. A `causes` label also needs supported meaning; it is not an RCT.

**Next experiment:** temporal-filtered, tenant-scoped bounded PPR or activation
over explicitly sourced relations, with fixed node/edge/iteration limits and
residual tolerance. Compare recovered complete support chains, irrelevant hub
expansion, temporal validity, total tokens, and latency against the current
traversal. PPR estimates graph importance under its transition model; it does
not produce exact multihop logical answers. Each selected answer candidate must
retain its connecting premises within the context budget.

Coreference additionally needs a labeled disjoint evaluation of merges/splits,
ambiguous names, different owners, pronouns, cross-session drift, and rollback.
Prefer explicit confirmed aliases before uncertain model merges. A local model
must retain confidence and citations; spaCy installation alone does not provide
arbitrary conversational coreference resolution. Wrong merges can leak another
person's memories and create incorrect belief updates.

## Pillar 4: semantic distillation and narrower lessons

**Present:** `commontrace/distill.py` already has exact small-corpus Jaccard
clustering and bounded MinHash/LSH candidates for larger inputs, with exact
pair checks and a bounded representative calculation. That is more specific than
the attachment's "word overlap" description, though it still misses semantic
paraphrases. HDBSCAN adds embedding, density, parameter, dependency, and noise
handling costs; a small textual root-cause label is not a verified causal cause.

`commands/lesson_cmd.py::run_suggest_revision` already supports optional
LLM-assisted drafts from hit/miss occasions. It requires a MISCALIBRATED lesson,
preserves the rule, narrows `applies_when`/`do_not_apply_when`, cites allowed
evidence, and creates a review draft. A harmful rule is deliberately not repaired
merely by narrowing its trigger. The roadmap proposes part of an existing flow.

**Next experiment:** evaluate semantic candidates against the current bounded
clustering on held-out labeled trace families, measuring pairwise/cluster
precision and recall, runtime, memory, and draft usefulness. Compare successful
and failed occasions while accounting for retrieval/assignment selection.
LLM-created counterfactual narratives are hypotheses, not observed alternate
outcomes. Drafts must pass source validation and review; automatic activation
would remove a useful product safeguard without proof of benefit.

## Pillar 5: MCP progress

Use the SDK's request context and standard `notifications/progress` correlation
with the request's progress token. Intermediate notification messages do not
replace the final tool result and should not expose raw memory content.
Cancellation, error cleanup, absent-token behavior, interleaved calls, both
supported SDK variants, and HTTP/stdio transport require tests.

The current implementation work adds progress to conversation ingestion,
recall and summarization and moves synchronous operations to worker threads.
This is a concrete responsiveness improvement with preserved tool results.
It does not make the underlying computation faster or provide genuine token
streaming. Arbitrary JSON-RPC token chunks are not interchangeable with the MCP
progress protocol; a separate supported streaming mechanism and client contract
would be needed before advertising incremental answer tokens.

## Pillar 6: tenant storage and distributed operation

**Present:** Hub queries use tenant scope; `hub/models.py` already declares
organization/time and search indexes. The current review-serving implementation
projects narrow SQL records, filters and sorts in PostgreSQL, and limits pages
before ORM hydration. It measures approximately 66–80× faster queue operations
on a 10,000-entry paired workload with identical results. See
[hub-queue-performance.md](hub-queue-performance.md).

**Next experiment:** use query plans and realistic tenant skew to determine
whether time range or tenant-hash partitioning helps specific workloads.
Partitioning belongs in schema migrations and operational tooling, with staged
backfill, uniqueness/foreign-key semantics, retention, pruning, default partitions,
index build/locking, rollout and rollback tests. It is not a change confined to
`hub/crud.py`. Cross-tenant access checks must remain effective through every
index, cache, aggregate, partition, and background queue.

Partitions on one PostgreSQL cluster do not create infinite horizontal scaling.
Sharding, routing, replication, failover, consistency, backup/restore, capacity,
observability, noisy neighbors and recovery objectives remain separate concerns.
An indexed exact aggregate can still scan many qualifying rows. The retained
SQL serving gains should precede a costly distributed database migration.

## Decisions from the current iteration

Retain the independently implemented exact mapped retrieval, localized belief
repair, checkpoint-aware extraction, bounded summary work, narrow Hub SQL
serving, consistent UI elements, and tested MCP progress. These improve identified
bottlenecks while preserving behavior and provenance. Several measured local
operations exceed 20×; none proves a universal gain or competitive answer score.

Reject unvalidated default retrieval changes: larger primary quotas, forced
session diversification, shorter excerpts, and their advice/history adaptive
variant regress on disjoint official conversations. Preserve their measured
results rather than presenting their best individual score as a general win.
Keep ANN, quantization, semantic clustering, PPR, bandits and partitioning as
explicit experiments with the acceptance criteria above. Privacy claims need
an actual reviewed construction. Comparable benchmark superiority needs matched
generation/judge settings and representative held-out official evaluations.
