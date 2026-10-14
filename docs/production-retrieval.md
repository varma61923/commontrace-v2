# Production conversation retrieval

HTTP and MCP conversation recall now reuse bounded read-only SQLite connections.
The shared service pool allows at most 16 handles and four for one exact source
path. Leases are exclusive; exhausted capacity waits up to one second and HTTP
returns `503 conversation_busy` when a retry is needed. Idle handles expire on
subsequent pool activity or are evicted when capacity is needed. HTTP shutdown
retires its root's handles after active reads finish.

Each lease checks file identity and binds schema/units identity inspection and
recall to one SQLite snapshot. File replacement, migration and generation
changes reopen the handle. Updates and deletions invalidate source and response
caches through the existing revision checks. Read-only connections have the
same bounded 8 MiB page cache and memory scratch setting as writers. Unicode
postings are repaired through the normal writer before the read when needed;
frozen banks retain exact streaming fallback. The shared service pool and
response cache reset their locks and handles after fork. Application-owned
stores should be opened in their worker process.

Within a snapshot, version checks are reused; local writes still invalidate
cached turns. Response sizes include explanation metadata and are counted once
per insertion. Cache bindings include the resolved embedding configuration and
availability. Transient dense/reranker fallback responses are retried rather
than cached indefinitely. These changes apply to production request paths,
including queries with the existing default packing strategy.

## Evidence and reading

`conversation recall --context-strategy coverage-v2` retains existing packing
for detail queries and diversifies relevant sessions and chronological span for
history queries. It is also available through HTTP and MCP. Candidate ordering
uses the question and eligible quoted sources; benchmark labels and gold answers
never enter retrieval. Scope, cutoff, injection screening, atomic graph support
and final budget admission remain authoritative.

The strategy returns an evidence manifest under
`explain.context_selection.evidence`. It identifies the exact delivered excerpts
by source turn, session, date, role and memory kind. It does not expose the hidden
remainder of an excerpted turn. Standing instructions and profile statements
retain their source identity.

`conversation answer --grounded` requests bounded JSON with answer, source
citations and an abstention flag from the configured reader. Unknown citations,
unsupported schema fields, malformed output or an uncited non-abstaining answer
produce an abstention. Citation validation establishes source membership, not
entailment or factual correctness. Reader accuracy still requires independent
evaluation. Plain answers remain compatible. Follow-up searches accept only
bounded strings and an explicit `done=false` decision.

Relevant older directives are retrieved through their sparse index even when a
recent-rule cap is full. Conditional instruction context receives priority over
generic request verbs. Assistant-history queries prioritize assistant evidence;
relational discovery recognizes additional authorship/ownership clauses.
Conversation timestamps preserve seconds and fractions end to end, and invalid
offsets are rejected. Offsets retain the existing documented speaker wall-clock
semantics; this is not a new cross-time-zone UTC model.

## Measurements and limits

The [HTTP workload](../benchmarks/gateway_read_pool.py) runs real loopback
HTTP/1.1 requests with bearer authentication through the production gateway.
Both arms use the same recall implementation and deterministic operational notes;
the reference reproduces the old fresh-store lifecycle. Returned context, turn
IDs and token counts have identical hashes. These are simulated notes, not
production customer traces, and exclude model inference and remote latency.

| Source turns | Request workload | Fresh-store p50 | Pooled p50 | Speedup |
| --- | --- | ---: | ---: | ---: |
| 2,000 | Repeated query, 60 requests | 9.451 ms | 1.023 ms | 9.24× |
| 2,000 | Varied queries, 60 requests | 9.623 ms | 7.272 ms | 1.32× |
| 10,000 | Repeated query, 60 requests | 17.430 ms | 1.003 ms | 17.37× |
| 10,000 | Varied queries, 60 requests | 18.207 ms | 16.367 ms | 1.11× |

The [paired coverage oracle](../benchmarks/evidence_coverage.py) measured
109.962 ms versus 2.787 ms (39.46×) on an 80-facet, 16-passage workload with exact
output equality. It measures the lexical gate only. Ordinary short questions
have less repeated work to remove. Neither measurement establishes a universal
10× improvement or competitor-wide superiority.

The [17-task contract suite](../benchmarks/task_suite.py) covers single hop,
multi-hop, open-domain temporal, temporal user/session recall, assistant recall,
preferences, updates, temporal reasoning, multi-session recall/reasoning,
preference/instruction following, extraction, summarization, event ordering,
abstention and contradictions. It checks synthetic source delivery and budget
behavior; it is not answer accuracy. Both strategies passed its small fixture.

Exploratory seeded public-data comparisons used 240 LoCoMo questions, 60
LongMemEval-S questions and 80 BEAM-100K questions. Detail packing retained
LoCoMo/LongMemEval source recall; BEAM showed small improvements in selected
history categories. These development measurements do not establish significant
gains across every task. No model-backed answer comparison or complete Mem0
system comparison is claimed. Mem0 is a system, while LoCoMo, LongMemEval and
BEAM are datasets with different task definitions and scoring profiles.

Graphify reviewed the production paths and competitor source. The final local
surface has 302 Python modules and no missing graph endpoints. Five new
reachability candidates are the read-pool constructor/class, root retirement,
shutdown and fork-reset methods; singleton creation and lifecycle hooks are
exercised by the pool tests. The reviewed candidate allowance increases from 304 to 309 without changing
the module budget. Static warnings are not declarations of dead code.

Regression coverage includes [pool lifecycle and boundaries](../tests/test_conversation_read_pool.py),
[snapshot/cache races](../tests/test_conversation_snapshot_cache.py),
[task evidence and cited answers](../tests/test_conversation_task_evidence.py),
and [coverage oracle equivalence](../tests/test_evidence_coverage_benchmark.py).

## Default hybrid fact retrieval

All configuration profiles select the cached local FastEmbed BGE-small model by default.
Install `commontrace[fastembed]` and provision its model cache to activate semantic retrieval;
missing packages or weights retain lexical recall, and automatic model loading never downloads
weights during a request. Set `embedding: none` in a profile for an explicit lexical-only deployment.

Provision the local models once during deployment setup:

```bash
python -m pip install 'commontrace[fastembed,attention]'
python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"
python -c "from commontrace import rerank_arm; error = rerank_arm.ready('cross-encoder-fast'); raise SystemExit(error or 0)"
```

Run setup as the same service account and with the same cache configuration as the server.

The default `balanced` recipe combines independent BM25 and semantic rankings with RRF
(`k=60`) on both JSONL and SQLite stores. Query entities seed bounded breadth-first traversal:
two hops by default, at most eight seeds and 2,000 visited nodes per seed. Every discovered
fact and every memory used as a bridge must pass current scope, forgetting, TTL, authority
and evidence checks. A seed alone does not fabricate an extra graph relevance signal.
Traversal preserves feedback weights by multiplying edge reliability along each path.
Entities extracted during ingestion provide a capped relative boost to admitted candidates;
they cannot bypass scope or turn an otherwise absent candidate into evidence.
`legacy-balanced` retains the weighted score mode for callers migrating from JSONL rankings.

Search accepts independent `as_of` (valid time) and `known_at` (record time) snapshots.
Both are pinned across lexical, semantic, graph, category and planned retrieval stages;
current erasure and authorization rules continue to apply to historical queries.

```bash
commontrace evolve search 'Alice office' --dest ./store --graph-hops 2 \
  --as-of 2025-01-15 --known-at 2025-03-15 --reranker mmr
```

The same fields are in `/v1/memory/search`, its generated OpenAPI SDK contract,
and the MCP `memory_search` tool. MCP retrieval runs in bounded store workers;
the older `query_facts` and `archival_memory_search` tools retain their sparse
ranking contracts for existing clients.
`graph_hops: 0` disables graph expansion. `reranker: mmr` selects diverse evidence;
`cross-encoder-fast`, `cross-encoder`, `bge-reranker-v2-m3`, and `mxbai-rerank` select
local second-stage models. Automatic selection honors the profile and only loads cached
weights; explicit model selection follows the existing model-provisioning policy.
MMR normalizes relevance before comparing it with redundancy so RRF's small score scale
does not overwhelm the relevance/diversity tradeoff.
