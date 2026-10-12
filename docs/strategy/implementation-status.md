# Implementation status: master prompt (Part G) and competitive report

Audited 2026-10-09 against `claude/commontrace-competitive-analysis-rnt3xr`
(which includes `feat/commontrace-memory-evolution`). "Done" means code, tests
and docs exist in this repository. Numbers are measured on this machine with the
commands in [Measured results](#measured-results). Nothing here is a claim about
hosted infrastructure, live customers or model-graded answer accuracy, because
none of those were available to measure.

Legend: **Done** · **Done (this pass)** (added or fixed in this audit) ·
**Partial** · **Not done** (with the reason).

## Next-generation implementation (2026-10-10)

### Integrated continuation (2026-10-12)

The integrated source adds bounded multi-row FTS projection on exact/lexical
fact writes, raw-score telemetry on both unified and direct conversation recall,
and a harmful-label CodingLessonBench path that only reports retirement N after
a real SOUND journal retirement. Direct conversation spans now include a
fixed-scale score/margin, heuristic coverage confidence and abstention on fresh
and cached reads, including accepted second-pass turns; the caller-provided
space label was removed. One focused bundle passes 139 tests with 15 optional
skips. See the [direct telemetry note](next-generation-conversation-retrieval-telemetry.md).
Competitor patterns from Hindsight and EverOS informed the isolation and score
semantics; no competitor code was copied. The current clean 1M run passes the
single-write target at 6.198 ms p95, while batch ingest reaches 1,858.199
facts/s and misses the 2,000 facts/s gate. The earlier sparse-slot reference
measured 6.127 ms and 1,962.476 facts/s; the negative-ID probe-cache candidate
measured 7.216 ms and 1,843.770 facts/s and was removed. These separate runs do
not establish causality. See
[the probe-cache note](next-generation-f1-negative-id-probe-cache.md),
[the latest candidate note](next-generation-p0-conflict-slot-fastpath.md), and
[the earlier FTS candidate](next-generation-p0-batch-projection.md).

A 1M run of the detached-snapshot candidate measured 1.797 ms single-write p95
and 2,071.287 facts/s in its 1,000-fact batch. A later run on the active
descendant measured 1.766 ms and 1,995.368 facts/s. Both are one-batch
measurements; `configuration.py` and `memory_record.py` hashes differ between
reports, so the batch result remains marginal and reproducibility is
unestablished. See the [snapshot-reuse note](next-generation-f1-snapshot-reuse.md),
[earlier measurement](measurements/f1-snapshot-reuse-candidate-1m.json), and
[follow-up measurement](measurements/f1-snapshot-reuse-followup-2026-10-12.json).

A paired, retrieval-only LoCoMo comparison covers a stratified 154-question
sample at 1,500 and 4,000 tokens. All reported non-latency aggregate metrics
match the frozen reference, and no comparison gates fail. The run has no answer
generator or judge, so answer accuracy remains unmeasured. See the
[compact provenance report](measurements/retrieval-telemetry-regression.json).
The earlier integrated `tests/` and `e2e_tests/` run passed 6,825 tests with 126
skips and 26 warnings (4,194.17 seconds). Ruff, docs drift, both OpenAPI checks,
Bandit, strict mypy, and pip-audit pass as well.

The native hook transport now passes three frozen rounds at 43.47, 42.58 and
43.37 ms p95, with 150/150 deliveries; the matching baseline had one round at
55.16 ms. Removing `argparse` from the successful bridge startup path preserves
the fixed hook CLI and receipt behavior. This transport-only fixture does not
cover cold broker startup or broad host-load variation. Hybrid/server latency,
zero-reembedding restart at scale, model-backed SWE uplift, harmful-task N, and
model-judged answer quality also remain unmeasured.

### Additional feature slices (2026-10-12)

The F1 hybrid acceptance harness exercises `search_recipes.search` end to end,
including FTS, dense candidates, canonical admission and fusion. A 64-fact
synthetic-adapter smoke measured 14.16 ms p95 over two samples. Its fresh
process retained dense-only and lexical-only candidates, excluded the
out-of-scope row, replayed zero projection events and requested zero document
embeddings. This is plumbing evidence; the 1M local, real-model and server
gates remain unverified. See the
[hybrid acceptance note](next-generation-hybrid-acceptance.md).

Hub Postgres record search now hydrates dense ANN IDs in 500-key batches with
tenant, scope, kind and lifecycle filters. The follow-up adds a tenant-ordered
record outbox, revision-keyed embedding cache, fenced generation publication,
and a server-owned local embedder identity. Dense candidates return through the
authenticated REST route and are checked against current records and recursively
validated AtomicFact evidence. Forgetting an evidence source cascades to derived
records and erases historical vectors, including through an old pinned reader.
The combined Hub route/record/REST/embedder/startup slice passes 83 tests in
83.10 s on disposable PostgreSQL; the current route suite also passes 13 tests
in 15.75 s against real PostgreSQL/pgvector 0.8.2. Isolated server-image import
parity passes 11 tests in 12.40 s.
These use deterministic embeddings. Default model packages and cached weights
are optional, so lexical fallback remains possible; no production model quality,
1M server latency or write-throughput gate is established. See the [hosted route](next-generation-hosted-hybrid-route.md),
[event stream](next-generation-hosted-vector-stream.md),
[projection worker](next-generation-hosted-vector-projection.md), and
[pgvector validation](next-generation-hosted-pgvector-validation.md) notes.

Aggregate facts recall now uses a bounded Lance candidate set for current-time
queries, scores only post-watermark facts directly, and retains exact cached
scoring for historical queries. Projection admission tests and the existing
bootstrap, retrieval and fact-store regressions pass 120 tests; no 1M latency or
real-model recall result is established. See the
[recall projection note](next-generation-recall-projection.md).

The F5/F8 quality evaluator records a reproducible hand-labeled synthetic
baseline for legacy entity thresholds, exact/MinHash resolution, keyword
classification, pattern relations and endpoint-aware edge-pair decisions. The
Graphiti-adapted fixture contains 40 entity pairs plus 10 labeled edge-pair
scenarios; the edge evaluator resolves five pairs with 100% resolved-only
agreement and abstains on the rest. On the expanded synthetic pairs, the
exact/MinHash path reports TP=9, FP=5, FN=15, TN=11; no production threshold was
changed. The tiered entity resolver uses exact postings and four-wide MinHash
bands, preserving the prior accepted pair set while reducing synthetic
700-entity candidate-filter median from 1,641.34 to 56.02 ms. Embedding and LLM
quality remain unmeasured, and the fixture is not a held-out calibration set.
See the [F5 candidate-index note](next-generation-graphiti-f5-dedup-adaptation.md),
[measurement](measurements/f5-graphiti-lsh-candidate-2026-10-12.json), and [F8
fixture supplement](next-generation-f8-graph-dedup-fixture.md).
The F6 one-call profile/search route now returns
search hits from a fresh non-indexed store through the existing fallback, with
19 focused tests passing and 2 optional skips. See the dedicated
[graph-quality](next-generation-graph-quality-evaluation.md) and
[profile-tier](next-generation-profile-tier.md) notes.
Preference reinforcement now decays the previous weight before adding a fresh
assertion: a weight of 10 last observed 180 days ago becomes 3.5 under the
90-day half-life instead of returning to 10. The shared helper covers local and
hosted capture; 21 focused profile/preference tests pass, including a hosted
PostgreSQL capture regression. Preference extraction quality and held-out
ranking uplift remain unmeasured. See the
[Cognee adaptation note](next-generation-profile-reobservation-decay.md).

The existing add-only fact extraction prompt now resolves relative dates only
against `observed_at`, keeps vague periods approximate, and requires transitions
to preserve both old and new states. Six focused prompt/data-flow tests pass;
real-model extraction accuracy is unmeasured. The rule adapts mem0's local
observation-date and transition guidance without copying its prompt; see the
[F5 extraction note](next-generation-f5-extraction-rules.md).

F9 summaries now retain prompt/completion direction per purpose, treat replayed
LLM-cache cost (at the cache-time price) as historical rather than fresh spend,
and mark session cost unknown when a fresh call has no price. Eleven focused
runtime/ledger tests pass;
invoice reconciliation and provider-backed usage remain outside the fixture.
See the [token accounting note](next-generation-f9-token-accounting.md).

The F9 durable-worker gap is already covered by `commontrace/jobs.py`: SQLite
WAL persistence, leased claims and crash reclaim, bounded retries, dead-letter
redrive, and CLI operations are used by background ingestion and other local
workflows. Eleven queue tests pass. This does not establish an external worker
service or an atomic outbox between every caller's state change and enqueue;
see the [durable-worker note](next-generation-f9-durable-worker.md).

Codex failure injection now registers the host-supported `PostToolUse` event
and maps only explicit nonzero/error results to CommonTrace's
`PostToolUseFailure`. A matching error-signature directive is injected; success
and unstructured results remain ordinary post-tool events. The six-file native
hook regression set passes 80 tests. See the [Codex hook note](next-generation-codex-post-tool-failure.md).

Native hook capture now recovers when a connected broker returns malformed data
or stalls before replying. The bridge retries through the detached capture-only
writer with a per-invocation event ID, so a broker commit immediately before the
failure and the retry leave exactly one journal row. Four accepted-request
failure/commit combinations pass; this adapts Mem0's durable handoff and retry
discipline to CommonTrace's SQLite capture journal. Failure to start the detached
writer remains outside the measured path. See the
[accepted-request recovery note](next-generation-hook-accepted-request-recovery.md).

Cold-start code verification now treats a backticked source path as a citation
and checks the remaining quoted symbol against that source before seeding a
quarantined lesson candidate. History-import/session-learning tests pass 36,
with three skill-integration regressions also passing. This verifies importer
contracts, not lesson quality or CodingLessonBench lift. See the
[history citation note](next-generation-f4-history-citations.md).

`commontrace up` now discovers supported hosts through known PATH executables
and configuration roots, then installs repository-local hooks without requiring
an agent name. Kimi is reported but its host-global config is withheld unless
the user names it or passes `--allow-global-agent-config`. The existing
bounded project-export history scan remains the default; private Claude Code
and Codex history can now be imported only with `--import-agent-history`.
Discovery is repo-attributed from transcript `cwd`, capped at 20,000 entries,
128 files, 100 MiB imported, 20 MiB per transcript, and a 32 MiB aggregate
header-probe budget; exhausted scans persist partial coverage. History import
and bootstrap tests pass 30 focused cases. GUI-only installs without known
signals still need `--agent`; see the [detection note](next-generation-up-agent-detection.md)
and [history onboarding note](next-generation-f4-agent-history-onboarding.md).

F4 reflection now stores its watermark by session and scope. The prior
session-only cursor could skip interleaved captures if a caller reused one
session ID across agents; legacy rows migrate to the scope at the sealed capture,
and other scopes replay rather than lose work. The learner and journal suite
passes 59 tests, including interleaving, legacy migration, failure retry, and
unfinished-task cases. See the [cursor audit](next-generation-f4-reflection-cursor.md).

Direct `evolve runtime learn` can now opt into the existing bounded semantic
task segmentation with `--semantic-segments`; the default remains deterministic
batching. Boundary calls are routed and accounted under the dedicated
`session-segmentation` purpose. CLI, bounded-segmentation, cursor, and runtime
accounting regressions pass; boundary quality and latency remain unmeasured.
This adapts EverOS's boundary-first episode split, while keeping CommonTrace's
scope-bound watermark semantics. See the
[semantic segmentation note](next-generation-f4-semantic-segmentation.md).

The F3 query-planning review isolated dense projection as the local hybrid
latency bottleneck: synthetic 1M p95 is 2,848.97 ms for SQLite and 223.07 ms
for Lance, while FTS5 stays below 0.6 ms. A competitor review found no safe
query-only candidate: parallel FTS cannot remove the dense cost, a new hosted
SQL fusion would need a governed freshness projection, and reducing ANN
refinement has no adequate recall evidence. No candidate cap or dense-skip
change was justified because it could remove eligible dense-only hits. A
reusable Lance handle also failed the current lifecycle bar: the default missed
a cross-process write, strict refresh checks each read, and post-fork probes
were inconsistent. The local latency gate remains open. See the [query-planning
audit](next-generation-f3-query-planning-audit.md), [Lance lifecycle
investigation](next-generation-f3-lance-handle-reuse.md), and [follow-up no-go
note](next-generation-f3-local-latency-no-go.md).

A safe adjacent optimization now batches the post-ANN canonical hydration of
sparse and dense candidate IDs. A controlled 10K ABBA diagnostic preserved
ordered rows, scores and signals while lowering p50 from 15.011 to 13.893 ms;
the full 1M Lance projection miss remains. See the [hydration note](next-generation-f3-query-optimization.md).

The initial hosted F3 review found that `MemoryRecordEvent` lacked a
tenant-ordered projection cursor and the route had no query-embedding path. The
2026-10-12 hosted continuation below resolves that boundary for versioned Hub
records; the earlier note remains the design history. The new route still has
no measured 1M server latency or model-backed quality result. See the
[original boundary note](next-generation-f3-hosted-event-boundary.md) and the
[implemented route evidence](next-generation-hosted-hybrid-route.md).

F3 now keeps lexical retrieval available when an optional local dense projection
or embedding stage fails, while preserving errors from a caller-supplied vector
search. Cache-only model-load failure under `COMMONTRACE_OFFLINE` also stops
before the download retry. The retrieval/journal/observation/report slice passes
101 tests with one optional skip; no dense quality or latency improvement is
claimed. See the [local fallback note](next-generation-f3-local-dense-fallback.md).

The optional FastEmbed BGE-small provider is now the configured local retrieval
default for conversation, fact, and graph paths. Default model construction is
cache-only; missing dependencies or weights leave lexical results available.
Conversation `--rerank auto` follows the same rule, while an explicitly selected
cross-encoder retains online first-use behavior. The 121-test retrieval/config/
reranker slice passed with one optional skip. The full 1,540-question LoCoMo
paired run raised evidence-in-context from 78.69% to 82.06% (+3.37 points,
95% paired CI +1.99 to +4.54) and all gold evidence IDs from 72.33% to 75.46%.
Candidate recall p95 was 37.8 ms on the full set versus 67.7 ms in the earlier
200-question pilot; repeatable local latency is not established. No model answer
or judge was run, and the 1M local hybrid latency gate remains open. See the
[default note](next-generation-f3-default-local-retrieval.md) and its [full
paired reports](measurements/target-f3-locomo-fastembed-small-full-seed0.json).

The Hindsight comparison confirmed that source-grounded local observations and
wiki pages already disappear from current retrieval after their backing fact is
withdrawn. Added regressions cover that behavior while keeping journal history
and explicit page audit available. These checks are included in the 101-test
local slice; no runtime behavior change or answer-quality claim is made. See the
[retraction note](next-generation-hindsight-retraction-grounding.md).

Hosted memory expiration now has a tenant-bound `record-reap-due` route,
bounded to 100 records per call, with a version check before hard erasure and
the existing derived-record/vector cleanup. Hub requires `SCOPE_ADMIN` because
the call spans user scopes; the Python and TypeScript clients expose the
operation; the TypeScript SDK suite passes 38 tests. The hosted reaper unit
contract passes, while database-backed Hub checks remain skipped without
PostgreSQL. The SQL query compiles against the PostgreSQL dialect, but a live
hosted database run was not performed. See the
[expiration note](next-generation-hosted-expiration.md).

An F1 near-term frequency-batching candidate reduced seven token-stat queries
to one bound query in a crafted contract fixture. The 30K storage harness uses
unique numeric blocks and returns before that token-stat query, so its 2,163.626
facts/s baseline and 2,159.799 candidate figures did not exercise the change.
The candidate was removed as irrelevant to the measured path; a fresh profile
is needed to select an optimization. The 1M write gate remains open. See the
[candidate note](next-generation-f1-term-frequency-batching.md).

The TypeScript Vercel AI SDK adapter now uses the V4 middleware contract and
captures only final successful responses; the Mastra adapter uses tagged
`MessageList` input/output processors with request-scoped thread resolution.
Mastra scope is resolved per request, stale memory is removed when identity is
missing, and completion capture runs detached. After a clean exact-lockfile
install, the SDK suite passes 37 tests. Isolated runtime smokes passed through
the Vercel AI SDK's real `generateText` and `streamText` paths and Mastra's real
tagged `MessageList` API. A full provider-free Mastra Agent run also passed
with a mock model and local gateway; end-to-end hook p95 remains unmeasured. See
the [TypeScript middleware note](next-generation-typescript-framework-middleware.md).

OpenAI Agents now has a per-`Runner.run` input-filter adapter with user/session
scope and final-result hooks; Pydantic AI uses current `ProcessHistory` and
after-run capabilities to dedupe per-run retrieval and capture completion.
Provider-free lifecycle smokes passed with OpenAI Agents `ScriptedModel` and
Pydantic AI `TestModel` on `openai-agents==0.23.1` and `pydantic-ai==2.55.0`.
The installed-SDK adapter suite passed 21 tests; the base environment passed 13
and skipped 8 optional tests. Real provider transport remains unmeasured.

Actual LangGraph 1.2.1 graph execution exposed a configuration-injection bug:
the generic callback annotation left `config` unset and silently skipped recall.
The adapter now uses the SDK-recognized annotation, scoped human-turn causal
IDs and bounded tool-loop context reuse. Its 21-test installed-SDK suite covers
sync/async graph runs, actual tool dispatch, checkpoints and outcome joins.
Conversation `messages` stay clean; the auxiliary `llm_input_messages` channel
may still be checkpointed. See the [LangGraph runtime note](next-generation-langgraph-pre-model-hook.md).

Microsoft Agent Framework 1.21.0 loop execution exposed synthetic continuation
nudges being captured as new user messages, losing final response capture.
The provider now retains the original turn across iterations. Ten installed-SDK
tests pass, including scoped concurrent sessions and a real local tool loop.
The loop marker is an experimental SDK dependency, explicitly documented in
the [Microsoft runtime note](next-generation-microsoft-agent-framework-provider.md).

The native-memory CI job now has separate exact-pinned SDK lanes for LangGraph,
Microsoft, OpenAI Agents and Pydantic AI. Local executions of the CI test lists
passed 35 tests (7 optional skips) for LangGraph and 24 (7 optional skips) for
Microsoft; the combined OpenAI/Pydantic environment passed 35 (1 optional skip).
These use deterministic models and establish SDK lifecycle behavior, not
provider transport, answer quality or latency.

Optional protocol sections 16–18 and independent vector files now cover
retention/erasure, injection acknowledgements and learning quarantine/retries,
without changing the legacy causal vectors. The public opt-in pytest command
is documented beside protocol conformance. A combined envelope/policy/vector
and offline-retrieval/reranker/worker regression run passes 178 tests with two
optional skips. This includes
a reproduced cross-encoder offline download retry, now fixed while retaining
cached scoring and normal online loading. Offline reranking bypasses a warm
worker that could have inherited an earlier online environment; see the
[fallback note](next-generation-f3-local-dense-fallback.md).

The handwritten Go, Rust and JVM allowlists previously rejected five current
operations that generated clients already exposed. They now admit all thirteen,
with record put/search/forget/reap and turn wrappers in Go, Rust, Java and Kotlin.
Loopback tests first reproduced the rejected-operation failure, then passed
three tests per SDK build, including both JVM facades and Go's race detector.
Actual request paths, bearer authentication, pinned scopes, versioned payloads
and reaper limits are checked; see the [SDK parity note](next-generation-sdk-record-parity.md).

The previously skipped Hub record/REST slice now passes all 50 tests on a fresh
disposable PostgreSQL 16.15 cluster, with a non-superuser/non-bypass application
owner and an explicitly prepared nonowner RLS reader. This covers expiry,
version-checked cascading erasure, durable vector-erasure receipts/retries and
REST admin/tenant boundaries. No runtime or test fix was required. The owned
server was stopped afterward, and retained databases were untouched. The vector
engine remains mocked; neither pgvector/HNSW nor hosted hybrid latency is
established. Reproduction is in the [hosted expiration note](next-generation-hosted-expiration.md).

The 2026-10-11 continuation follows the attached build prompt's phase gates.
The latest million-fact run on `aeadf5d` records 3.86 ms single-write p95
(passes <20 ms) and 1,167 facts/s batched (misses ≥2,000). The earlier
`54681d0` optimization measured 3.23 ms and 1,181 facts/s.
The earlier continuation recorded 31.43 ms and 759 facts/s; both runs included
concurrent validation and are not a controlled causal comparison. Phase advancement stops;
P1–P4 and every unverified acceptance criterion remain part of the objective.
The two named CodeQL alerts (#10 and #1)
are now dismissed after source review and authentication tests (119 passed,
3 skipped). Security-extended queries remain enabled. The storage reproducer's
exit-status gate is fixed with five boundary checks. See
[the continuation audit](next-generation-p0-audit.md) and
[write optimization evidence](next-generation-p0-write-optimization.md).

The measured storage commit's full application suite completed with 6 failures,
6,666 passes and 118 skips. Shared connection ownership, abort-path cursor
cleanup and test isolation fix the six reproduced failures in 135 focused checks;
the frozen `fa1ef0c` full suite passed 6,678 tests with 118 skips. The later
Lance changes have separate optional-engine validation. Native transport delivered 50/50
directives but measured 152.27 ms p95, missing <50 ms. Follow-up Graphify records
297 modules and 303 reachability candidates within the unchanged ceilings.
The optional Lance engine passes four real-engine/harness checks. At 10K
synthetic vectors its ANN p95 is 7.15 ms with recall@10 1.0; this excludes full
hybrid retrieval. The [local vector audit](next-generation-p0-vector-audit.md)
records the scope and reproducible commands. At 1M vectors the ANN component
measured 13.04 ms p95, but mean recall@10 0.91 misses the existing 0.97 target.
Refinement 4 subsequently raised recall to 0.98 on a matching retained index;
20 held-out queries reached 0.995. Three committed product-query rounds stayed
below 25 ms p95. A fresh million-vector reproducer measured 19.69 ms p95 and
mean recall 0.995, passing its explicit component targets. Ten real-engine,
harness and boundary checks pass after the new regressions failed first.
Full hybrid latency and zero-reembedding restart remain unverified.

The existing native bridge is replaced by an isolated stdlib Python transport
on `4b1d2eb`, retaining one broker/core and a compatibility launcher for older
commands. Three frozen 50-call runs delivered all directives and measured
51.59, 46.64 and 48.64 ms p95. One round misses <50 ms, so native latency remains
open. The historical 152.27 ms Node report has limited dependency provenance;
it is not a controlled comparison. Scoped validation passes 129 tests with
8 skips and installed-wheel capture/delivery checks. The frozen `4b1d2eb` full
application and end-to-end suite passes 6,709 tests with 126 skips in 4,094.07 seconds.
Graphify now reports 298 modules and 300 candidates within the unchanged
306/303 limits. See [native transport evidence](next-generation-native-transport.md).

The later global near-term projection on `19c914b` upgrades a private 1,044,020-fact
copy in 55.65 seconds, reducing lexicon rows from 9,396,194 to 3,132,080. Every
authoritative event and fact payload checksum remains identical. Storage,
deduplication, evidence, scopes and rollback pass 192 scoped checks; nine new
near-index contracts cover migration, concurrent readers/openers, injected
failure, brute-force recall, rebuild and uncapped strongest-match selection.
The ordinary fact snapshot optimization has four compatibility contracts and
80 final write/evidence checks. Its scalar serialization profile rises from
96.5–99.0K to 173.1–176.1K calls/s; nested values retain the recursive path.
These are component diagnostics, not a new million-fact write acceptance run.
The pinned 154-question LoCoMo retrieval regression passes at 1,500 and 4,000
tokens against the frozen earlier report; no reader/judge answer accuracy is measured.
The frozen full application/e2e suite for storage/snapshot source `d50d54b`
passes 6,722 tests with 126 skips and 26 warnings in 4,145.44 seconds (exit 0).
It precedes the later staged-write and lexical/snapshot reuse changes.
See [global near-index evidence](next-generation-global-near-index.md).

The later production write path keeps FTS eager and batches lexical projections
and validated events (`ce1abe2`), adds bounded transaction-local lexical reuse
(`64b64f4`), and reuses unchanged staged snapshots (`cd8c1dc`). All growing-copy
diagnostic batches miss 2,000 facts/s: candidate pairs are 1,601/1,645,
1,741/1,756 and 1,722/1,653. All baseline rounds and misses are retained;
concurrent validation prevents causal throughput claims. The final source
passes 73 focused and 149 broader contracts, CI source/dependency checks,
the pinned 154-question retrieval regression and Graphify (298 modules,
300 candidates, zero dangling endpoints). The frozen `cd8c1dc` full suite
completed with 6,748 passed, 126 skipped and one failure in `FactMap.flush()`
when a fact ID changes inside a mutation. The descendant source binds removal
history to the old ID and insertion history to the new ID; 132 focused
storage/evidence contracts pass after the fix. The integrated descendant full
suite completed with 6,821 passed and 116 skipped; its sole failure was a static
test requiring `pytest.importorskip("hub...")` in the new config tests. Adding
those guards makes the config file and that static test pass 31 targeted tests.
The full suite was not repeated after this test-only correction. The fresh
exact-1M workload on `7e0a224` measures 4.69 ms
single-write p95 and 1,739 facts/s batched: the single gate passes and batch
gate still misses. See [write CPU evidence](next-generation-p0-write-cpu.md)
and [identity mutation evidence](next-generation-fact-identity-mutation.md).
See [staged-write evidence](next-generation-staged-write-evidence.md).

This continuation also fixes three adjacent requirements. OAuth and Hub KMS now
use one resolved YAML/profile source, and environment migration respects the
selected config path, mounted-secret precedence and store boundary (179 focused
configuration/authentication/Hub tests pass). Learner prompts are checked in
their complete serialized form; the 32-test learner contract passes and an
oversized indivisible capture remains retryable instead of being sealed unseen.
Hybrid graph search seeds from admitted sparse and dense results, and bounded
multi-round retrieval preserves distinct query blocks and revalidates facts
after judging. Its 19 retrieval contracts pass; a deterministic synthetic
comparison recovers 2/2 distinct facets rather than 1/2 and excludes withdrawn
facts and stale seeds. These are component results, not answer-quality or
latency acceptance. The integrated `tests/ e2e_tests/` run completed with 6,821
passed, 116 skipped and one failure: a repository static test requires tests
that import Hub modules to call `pytest.importorskip("hub...")`. The two new
config tests now include those guards; the config file plus the failing static
regression pass 31 tests. The full suite was not repeated after this
test-only fix. The separate Hub suite had 2,506 passed and 5 skipped; its sole
failure was the local test role lacking `CREATEROLE`, and the RLS test passed
when rerun with a temporary superuser matching the CI service role.

The combined branch contains P0–P4 implementation; this audit does not establish
their acceptance gates. The earlier memory-evolution code
was already squashed into main; its branch and `codex/next-generation-p0` are both
ancestors of `codex/next-generation-complete`.

| Brief | Implemented | Acceptance limits |
| --- | --- | --- |
| A: storage | Compact SQLite WAL/event ledger, bounded idle connection reuse, exact/near/FTS candidates, transactional migration/rebuild, durable content-hash vector projection; optional background Lance HNSW with backend-specific watermarks; tenant-pinned PostgreSQL record ledger, FTS, optional pgvector/HNSW | Fresh 1M snapshot-reuse candidate: 1.797 ms single-write p95 / 2,071.287 facts/s in one 1,000-fact batch (meets both write thresholds, with a 3.6% batch margin; no same-HEAD 1M baseline). Earlier sparse-slot, clean baseline and probe-cache runs range from 1,843.770–1,962.476 facts/s and do not establish causality. Full `search_recipes.search` at 1M with a synthetic adapter measures SQLite p95 2,828.93 ms and Lance p95 200.82 ms; both miss the local <50 ms gate. The fixture restart passes with zero document re-embeddings and zero projection replay. Server hybrid is unsupported by the current facts API; real-model latency remains unmeasured. See [snapshot-reuse note](next-generation-f1-snapshot-reuse.md), [1M report](measurements/f1-snapshot-reuse-candidate-1m.json), [hybrid acceptance note](next-generation-hybrid-acceptance.md), and its [SQLite](measurements/f1-hybrid-1m-synthetic-sqlite.json) / [Lance](measurements/f1-hybrid-1m-synthetic-lance.json) reports |
| B: records | Seven kinds, five scope dimensions plus labels, six lifecycle states, version history, bi-temporal validity, typed relationships, evidence and revision-grounded derived reads | Hosted legacy SDK operations still use the Gateway; Hub implements envelope CRUD, not all legacy operations |
| C: extraction | Dated speaker/transition extraction with explicit assistant and tool attribution, no assistant echo, exact/entropy/MinHash/dense/joint-judge candidate tiers, bounded per-entity/facet observations, existing grounded mental-model refresh | Optional model quality and extraction-cost reduction need real model-backed evaluations |
| D: learning | Durable event capture, bounded session segmentation and complete serialized prompts, explicit tool/run outcomes, clustered cases, curator/writer proposals, quarantine/shadow/causal activation and retirement, four-format transcript parsing/import, reflection arena and DPO export; prior lessons are selected by captured task intent before the bounded top ten; CodingLessonBench reports a fixed-weight task-stratified multi-task effect with a simultaneous anytime-valid interval | Automatic, project-verified host discovery currently imports Claude/Codex only. Cursor/Gemini have explicit unsupported coverage records because the inspected stores lack an authoritative per-session project cwd; project exports remain importable. Oversized indivisible captures can block their session until a larger explicit budget is used; real SWE uplift and harmful-retirement N remain unmeasured pending labeled real-runner evaluation. See the [Cursor/Gemini attribution note](next-generation-f4-cursor-gemini-history-scope.md), [prior-selection note](next-generation-everos-prior-relevance.md), and [stratified uplift note](next-generation-codinglesson-stratified-uplift.md) |
| E: integration | Shared ten-event core, eight native-agent serializers, framework model hooks, RRF admission, persistent once/inherit decisions, delivery acknowledgements, escaped templates, unextracted buffer, background broker; Google ADK before-model/after-agent callbacks and CrewAI kickoff callbacks delegate to the one-call middleware; LangGraph, Microsoft Agent Framework, OpenAI Agents run hooks, Pydantic AI capabilities, Vercel AI SDK, and Mastra integrations are available | Provider-free lifecycle/runtime checks pass for Google ADK, CrewAI, LangGraph, Microsoft Agent Framework, OpenAI Agents, Pydantic AI, Vercel AI SDK, and Mastra. CrewAI's `kickoff_async()` keeps its loop responsive through the synchronous callbacks; native `akickoff()` calls them directly and blocks. Kimi now updates its active host config in place; Cline needs Hooks enabled, Windsurf hooks capture only. Provider transport, cold startup and loaded-system latency remain unmeasured. See the [Kimi hook contract note](next-generation-kimi-hook-contract.md) and [CrewAI runtime evidence](next-generation-crewai-async-callbacks.md) |
| F: retrieval | Indexed sparse and incremental dense candidates, batched canonical hydration of sparse/dense IDs, first-pass graph seeds, temporal/evidence/scope/authority filtering, utility explainers, bounded reranking and three-round per-query completeness traces; optional Zep-style category-level `COMPLETE` / `PARTIAL` / `INSUFFICIENT` context judge; opt-in OTel spans report raw weighted-RRF top score and margin plus heuristic confidence/abstention; cache-only BGE-small/base FastEmbed CPU default | Full 1,540-question LoCoMo paired retrieval shows +3.37 pp evidence-in-context for BGE-small (95% clustered CI +1.99 to +4.54); no reader/judge answer score. Candidate p95 varied from 37.8 ms full-set to 67.7 ms in the 200-question pilot, and the 1M full-search synthetic-adapter gate still misses on SQLite and Lance. The context judge path has 314 benchmark-related test passes and 7 skips but no live LLM grades; Hub cache provisioning remains unmeasured. Raw ranking scores are not treated as calibrated hits. See the [F3 default note](next-generation-f3-default-local-retrieval.md), [F3 optimization note](next-generation-f3-query-optimization.md), and [context evaluator](next-generation-context-completeness.md) |
| G: profiles | Explicit static assertions, dynamic weighted/decaying preferences whose reinforcement applies decay before new evidence, custom profiles, profile returned with one-call save/retrieve, critical preferences always eligible; scoped, source-bound user-authored dynamic preferences now rerank the already-bounded local and hosted search result list | Reranking is functionally tested but has no held-out ranking or answer-quality measurement; context budgets use an estimate unless the caller supplies exact counting. See the [preference-ranking note](next-generation-profile-preference-ranking.md) and [reinforcement-decay note](next-generation-profile-reobservation-decay.md) |
| H: privacy | Pinned scope, pre-model and post-model redaction, recursive hard erasure across local history/FTS/vectors/derived records, retention reaper and reason audit, telemetry off, poison CI | External backups/mirrors require operator erasure; PostgreSQL vector cleanup failures remain explicitly pending in a tenant-pinned retry ledger until its worker runs |
| I: product | `up` setup/server, request-local YAML profiles and secret references, per-profile credential references, OAuth/KMS YAML resolution and bounded credential-safe config migration, history import, existing-lesson preview plus explicit review-only project-rule staging, HTTP/SDK record parity, signed aggregate leaderboard, Graphify CI budgets and pinned native SDK smoke tests; 29 CLI wrappers merged into three modules, retaining all 69 commands | No registry/service publication; first-rule staging, approval, and offline native injection are functionally tested, but elapsed human review and cold-install timing remain unmeasured; latest Graphify counts 300 modules; first-class setup docs list 25 environment overrides (<40), while 155 legacy alias entries remain in the migration schema. See the [first-rule note](next-generation-first-reviewed-project-rule.md) |

Measured results and commands are in [next-generation-evidence.md](next-generation-evidence.md)
and the [signed leaderboard](leaderboard/index.html). Historical status sections
below describe earlier work and do not establish this brief's acceptance gates.

## Part G: master prompt

| Item | Status | Where |
| --- | --- | --- |
| 0.1 Scoreboard with dense + cross-encoder on | **Done (this pass)**: LoCoMo (dense, dense + cross-encoder, dense + adaptive), BEAM (dense, dense + adaptive), LongMemEval (dense, paired 100-question subset) | `benchmarks/conversation_bench.py --embedder arctic-m` |
| 0.2 Model-backed answer accuracy | **Done (this pass)**: Gemma 4 31B reader and LLM judge (LoCoMo judge profile) on a 200-question LoCoMo subset, 68.5% [61.8, 74.5] with lexical memory at 1,500 tokens (see below); plus the in-process 0.5B local reader with the exact judge | `--answer --judge exact`, `local_llm.py`, `benchmarks/judges/` |
| 0.3 Competitors through the same harness | **Partial**: local raw-source mem0, Graphiti and Hindsight profiles; only mem0 and Graphiti have benchmark runs. Hindsight's public chunks-only adapter passes mocked contract tests but its local API/model dependencies are absent here, so it has no measured score | `benchmarks/vendor_adapters.py`; [Hindsight adapter limits](next-generation-hindsight-adapter.md) |
| 0.4 Agentic benchmarks (MemoryArena, AMA-Bench, MemGym, Evo-Memory) | **Partial (this pass)**: AMA-Bench open-ended QA (208 agent trajectories, derived step evidence) and MemoryAgentBench Accurate Retrieval (answer-in-context) run through the harness; interactive environments (MemoryArena, MemGym, Evo-Memory) still need their own agent loops | `--dataset ama`, `--dataset mab`, `benchmarks/dolphinbench/` |
| 0.5 Leaderboard from signed manifests, `reproduce.sh` | **Done** | `benchmarks/phase0*.py`, `reproduce.sh` |
| WS1 Multi-signal fusion, recipes, MMR, decision reranker | **Done** | `search_recipes.py` |
| WS1 Named second-stage rerankers | **Done (this pass)** | `providers.reranker("mmr" \| "cross-encoder" \| ...)` |
| WS1 Query planning, entity resolution, GLiNER, additive extraction | **Done** | `query_plan.py`, `local_extraction.py`, `additive_extract.py` |
| WS1 Adaptive context budget by question shape | **Done (this pass)** | `conversation.search.budget_for`, `--budget auto` |
| WS1 Store abstraction and scale backends (pgvector HNSW, LanceDB, Neo4j/FalkorDB) | **Done** | `store.py`, `vector_lance.py`, `graph_backends.py` |
| WS2 Exploration slots, SNIPW, OPE release gate | **Done** | `causal_policy.py`, `policy.py`, `gate --policy` |
| WS2 Interaction effects for co-firing lessons; auto-drafted narrower `applies_when` | **Done (this pass)**: factorial reading of independent per-lesson arms (SYNERGY / INTERFERENCE), and `draft_narrowing` writes a review-status lesson scoped to where it helps | `interactions.py`, `heterogeneity.draft_narrowing` |
| WS3 DPO export | **Done (this pass)**: evidence-linked preference pairs | `compression.export_preferences` |
| WS5 Publisher revenue share | **Done (this pass)**: fees per install/month/year and an outcome share paid only on the buyer's own HELPS verdict at the lower bound, in a hash-chained settlement ledger | `market_settlement.py`, `market settle` |
| WS10 Robot fleet demo with a causal report | **Done (this pass)**: multimodal episodes, protected safety memory, sim/real pooling refused, harm withdrawal, latency budget | `benchmarks/robot_fleet_demo.py`, `docs/benchmarks/robot-fleet.md` |
| Console executive view, release diffs, experiment designer, forensics, digest | **Done (this pass)** | `ledger_views.py`, `/v1/ledger/*`, console "Learning Ledger" |
| Every HTTP API in OpenAPI | **Done (this pass)**: 58 gateway operations with typed schemas validated against live responses; 106 Hub operations (OpenAPI 3.1, served at `/api/v1/openapi.json`); both generators refuse undocumented routes | `openapi/README.md` |
| WS2 Adaptive allocation | **Done**: era schedules (explore 50% until proven, monitor 5% after), hash-chained and published before the occasions they govern; AIPW scores with an asymptotic confidence sequence (running intersection), used for every scheduled assignment; schedule-aware integrity audit; proof packages carry and re-check the schedules; conformance vectors (PROTOCOL 13.5); `commontrace allocate` | `allocation.py`, `docs/benchmarks/adaptive-allocation.md` |
| WS2 Heterogeneous effects | **Done (this pass)** | `heterogeneity.py`, `experiment --by` |
| WS2 Attribute-before-memorize, hindsight probes, forensics | **Done** | `assurance.py`, `memory_control.py` |
| WS2 Neutral referee | **Done** | `measure.CausalMemory`, `memory_adapters.py` |
| WS3 Compression ladder, skill crystallization, causal promotion gate | **Done** | `compression.py`, `experience_skills.py` |
| WS4 Origin-bound authority, action policy, Ed25519, action-vote smoothing, collusion signals | **Done** | `origin.py`, `memory_authority.py`, `assurance.py` |
| WS4 Red-team suite | **Done (this pass)**: PoisonBench, nine attacks through the real write and recall paths; 0% attack success with an authority policy at 100% clean utility | `benchmarks/poisonbench.py`, `docs/benchmarks/poisonbench.md` |
| WS4 Multi-principal (GateMem-style) benchmark | **Done (this pass)**: GovBench through the gateway; 100% utility, 0 leaks, 0 forgotten records delivered, certificate covers derived records | `benchmarks/govbench.py`, `docs/benchmarks/govbench.md` |
| WS5 Federated commons, randomized response, replicated lift | **Done** (experimental privacy, stated as such) | `federation.py` |
| WS5 Lesson marketplace | **Done (this pass)**: fleets sign holdout lift for the exact lesson text, a referee pools 2+ independent orgs into a certificate, publishers sign listings with licence and price, buyers verify Ed25519 signatures against keys they trust and install only into review; the Hub stores verified listings (RLS: public read, owner write). Payment collection and revenue share are not implemented | `marketplace.py`, `market_listing.py`, `commontrace market`, `/v1/market/*`, `hub/market.py` |
| WS6 Foresight and sleep-time refresh | **Done** | `memory_control.offline_pass`, `dream` |
| WS7 PyPI and npm publishing | **Done (this pass)** (workflow; first publish needs registry configuration) | `.github/workflows/release.yml` |
| WS7 Agent self-signup, LLM wrappers, hooks, frameworks, connectors, generated SDKs | **Done** | `onboarding.py`, `completion_wrappers.py`, `frameworks.py`, `connectors/`, `sdk/` |
| WS7 Remote MCP with OAuth 2.1 | **Done (this pass)**: JWT access-token resource server (RS256/PS256/ES256/EdDSA, issuer/audience/expiry/scope, JWKS rotation), RFC 9728 metadata, for the gateway and HTTP MCP; the Hub keeps API keys | `oauth.py`, `mcp_transport.py` |
| WS8 Console, Memory Palace, Needs Attention, Learning Ledger | **Done (this pass)**: overview, Needs Attention review queue with revision-checked actions, memories, live activity, experiment readouts, command center, and the Learning Ledger (proven value and lift-per-token frontier, release diffs, experiment designer, occasion forensics, weekly digest) | `ui/`, `/v1/palace`, `/v1/ledger/*` |
| WS9 CausalMemBench | **Done (this pass)** | `benchmarks/causalmembench.py`, `docs/benchmarks/causalmembench.md` |
| WS9 Paper draft | **Done (this pass)**: every number reproducible from a named command | `docs/research/causal-memory.md` |
| Process: per-workstream plans and a scorecard | **Done (this pass)** | `docs/plans/`, `docs/strategy/scorecard.md` |
| WS10 Embodied fleets (sim/real separation, protected memories) | **Done**; multimodal episodes via ingestion | `fleet.py`, `gateway.py`, `ingest/multimodal.py` |

### Part G items not yet done

Checked against the code on 2026-10-09. Each is either blocked by something this
environment does not have or is open work.

| Item | Why not |
| --- | --- |
| Target A: accuracy parity with re-run competitors, LLM-judged, with CIs | No hosted reader or judge credentials here; only the local 0.5B reader with the exact judge was run |
| BEAM 1M and 10M | Not run (CPU-only machine; the 100K split is measured) |
| 0.3 adapters for Cognee, Hindsight, EverOS, Supermemory local, Letta | Hindsight's raw-source profile is implemented but has no native result because its local API/model dependencies are absent. Cognee's public search path includes `cognify()` extraction/graph work, while its no-cognify BM25 path uses internal APIs, so no fair adapter was added. Supermemory's raw-text route is promising, but the supplied checkout has no pinned local-server launcher for isolating its persistent data, models, credentials, and telemetry. Letta is deferred because the supplied checkout is a landing page, current public local SDK behavior does not establish raw-source search, and legacy passage APIs are deprecated. EverOS's supported conversation-ingest path requires an unavailable extraction service, while deferred raw messages are only returned for a session-scoped query and do not support comparable cross-session ranking. See the [Hindsight adapter note](next-generation-hindsight-adapter.md), [Cognee audit](next-generation-cognee-adapter.md), [Supermemory assessment](next-generation-supermemory-adapter-no-go.md), [Letta assessment](next-generation-letta-adapter-blocker.md), and [EverOS blocker](next-generation-everos-adapter-blocker.md) |
| 0.4 MemoryArena, MemGym, Evo-Memory, DolphinBench completion | Need interactive agent environments; DolphinBench harness is a stub |
| Target B: top-3 on two agentic leaderboards | Not established; AMA-Bench and MemoryAgentBench are measured as evidence, not judged score |
| Target C: >= 90% evidence within 1,000 tokens on LongMemEval-S | **Not met; best 86.56%.** At 1,000 tokens on the seeded 100-question subset: lexical 74.66%, dense 77.89%, dense + cross-encoder 84.18% (default blend 1), 86.05% (blend 0), 86.56% (blend 3), all about 975 tokens. Session recall@10 is 98%; the gap is ranking gold turns within the right sessions (turn recall@10 87.8%). Blend 3 did not hold up on held-out LoCoMo (78.52% -> 79.01% at 1,000 tokens, 83.90% -> 83.43% at 1,500), so the default stays at 1. Cross-encoder recall costs about 1.2-1.4 s p50 on this 4-core CPU. The lexical 74.66% result was reproduced on the official cleaned artifact; a separate session-prior candidate changed evidence by +0.51pp (paired 95% CI −1.02 to +2.55) and reduced turn recall@5 by 3.40pp (CI −6.80 to −0.34), so it was rejected. See the [source-verified rerun](next-generation-target-c-longmemeval-blocker.md) and [session-prior no-go](next-generation-target-c-session-prior-no-go.md) |
| Target D: hybrid p50 < 50 ms / p95 < 150 ms at 1M memories | **Vector path met** (`docs/benchmarks/vector-scale.md`): 1M synthetic 384-d vectors through `PostgresVectorIndex`, HNSW m 24 / ef_construction 200, ef_search 100: p50 7.2 ms, p95 10.7 ms, recall@10 1.000 (default m 16 build plateaus at 0.945). Hybrid lexical + vector at 1M and real embeddings not measured. The run found and fixed a generic-plan bug that made searches 1.2 s at 20k vectors |
| Target E: +5pp at 80% power within 2,000 occasions | **Empirical coverage clears 95%; power not met; formal AIPW coverage remains asymptotic** (seeded simulation, 400 runs per cell): fixed-prior mixture horizon 99; empirical time-uniform coverage >= 96.75% across 45 cells and null any-time false positives <= 3.0%. +5pp AIPW power is 0.30-0.33 with flat outcomes, 0.39-0.46 with moderate strata, and 0.64-0.70 with strong-strata models; +10pp adaptive power is 0.96-1.00. |
| Target G: `pip install` / `npm i` / `docker run` to first recall in < 60 s | A clean Python 3.12 venv, cached editable install, and verified offline `init`/`fact add`/`recall` took 12.59 s; local npm tarball install through verified add/reflect took 7.94 s; a prebuilt local Docker image added and recalled an exact fact offline in 2.077 s from first container start. The Docker timer excludes image build/pull, and the legacy builder required a temporary isolated context because it ignores the Dockerfile-specific ignore file. `up` auto-detects mapped agents and wires project hooks; `--project-rule FILE` plus explicit approval produced a first offline Codex SessionStart injection in the nine-case temporary-project fixture. Human review, cold-cache install, and registry publication/install remain unmeasured. See the [local smoke report](next-generation-first-recall-smoke.md), [reviewed-rule flow](next-generation-first-reviewed-project-rule.md), and [agent-detection note](next-generation-up-agent-detection.md) |

## Memory-engine feature matrix

The fifteen capabilities on the state-of-the-art checklist, audited 2026-10-10.
Every row has code and tests in this repository. "Measured" lists only numbers
taken on this machine; where no quality benchmark has been run, the row says so.

| # | Capability | Status | Where | Measured |
| --- | --- | --- | --- | --- |
| 1 | Semantic embeddings, multiple providers | **Done** | `embeddings.py`: tags `[provider:]model[@dims]` for local sentence-transformers, OpenAI, Gemini, Voyage, Cohere, Ollama, OpenAI-compatible; query vs document modes; SQLite content-hash vector cache for conversations and facts | Request shapes tested against fakes; no hosted call made in tests |
| 2 | Hybrid search (semantic + BM25) | **Done** | Conversations: keyword + dense arms; facts: `search_recipes` (BM25 + dense + entity + temporal) and multi-channel `recall` (lexical + dense reciprocal-rank fusion when `COMMONTRACE_FACT_EMBEDDER` is set) | Conversation arms on LoCoMo/BEAM/LongMemEval (see below); facts hybrid not yet benchmarked |
| 3 | Entity extraction and linking | **Done** | `entities.py` (keyword type classification during extraction), `entity_resolution.py` (embedding or trigram similarity, type-blocked, evidence-recorded merges), `graph resolve` | Tested with fakes; thresholds not tuned on data |
| 4 | Temporal validity windows | **Done** | Bi-temporal facts: valid time (`valid_from`/`valid_until`, `as_of`) and transaction time (`created_at`/`retracted_at`, `known_at`); contradiction detection on write (`fact_conflicts.py`) | 1,500-fact batch with detection: 3.6 s, 0 false conflicts (before the batch post-pass change) |
| 5 | Knowledge graph with relationships | **Done** | `relation_extraction.py` (LLM with domain/range/quote checks, or offline patterns with negation/hedge skips), typed bi-temporal Neo4j/FalkorDB mirror with `typed_neighbors`/`multi_hop` | Native mirror matches the local graph at five valid/record-time points on Neo4j 5.26 and FalkorDB; no extraction precision/recall benchmark |
| 6 | Multi-strategy retrieval fusion | **Done** | Weighted RRF across lessons/facts/graph/conversations; search recipes; rerank blend (`reranking.stage`) | Not separately benchmarked |
| 7 | Cross-encoder reranking | **Done** | Local (`cross-encoder`, `bge-reranker-v2-m3`, `mxbai-rerank`), hosted (`cohere`, `voyage`, `jina`), listwise `llm`; on recall, fact search and conversation recall | LoCoMo dense + cross-encoder row below; new rerankers not benchmarked |
| 8 | Session context layer | **Done** | Session/agent-scoped memory blocks, hash-chained rolling summaries, `working_memory.assemble`, gateway `/v1/working-memory` | 3,000 turns: first summary 89 ms, +10 turns 43 ms (3 ms tail check), rough (measured under load) |
| 9 | Knowledge consolidation | **Done** | Lessons: `consolidate`; facts: `fact_consolidation.py` (MinHash-LSH + optional embeddings, review proposals only) | Not benchmarked |
| 10 | Provenance tracking | **Done** | `provenance.lineage` up/down over runs, sources, facts, evidence, supersession, observations, proposals and lessons; `graph provenance --lineage` | Not applicable |
| 11 | Batch processing optimization | **Done** | One statement index per fact batch (exact hash + blocked near-duplicate postings + contradiction slots); ingest embedding stage with bounded concurrency; numpy fact cosine path | 1,500-fact batch with near-dup dedup: 2.0 s (was 33.6 s); 10,000 x 768 warm fact query 0.09 s (was 1.1 s) |
| 12 | Ontology-based classification | **Done** | `ontology_classify.py`: alias, keyword, embedding, optional LLM; declared types only, most specific wins | Tested with fakes |
| 13 | Adaptive budgeting | **Done** | Conversations by question shape; multi-channel `recall(adaptive_budget=True)` and `reflect` grow while evidence is weak | Conversation numbers below; multi-channel not benchmarked |
| 14 | Content completeness evaluation | **Done** | `completeness.py` grade on every recall (`explain.completeness`, question-term coverage, not answer correctness); conversation second pass for missing terms | Benchmark grader output identical across 5,000 randomized inputs |
| 15 | Hash-based deduplication | **Done** | Facts: exact hash index, near-duplicate (content Jaccard >= 0.85, identical numbers and negations), optional semantic (cosine >= 0.95); vectors cached by content hash | See row 11 |

## Competitive intelligence report

### Feature and architectural gaps

| Report item | Status | Where |
| --- | --- | --- |
| MCP server | **Done** (local stdio/HTTP/SSE and Hub, with `search_traces`, `contribute_trace`, `get_trace`, `vote_trace`, `amend_trace`); containerized local MCP over streamable HTTP added **(this pass)** | `mcp_server.py`, `hub/server.py`, `Dockerfile.local` |
| CLI with agent signup | **Done**; `init --agent-caller NAME` added **(this pass)** | `commands/init_cmd.py` |
| Dashboard | **Done** (no-build console, not Next.js, by design) | `commontrace/ui/` |
| User profiles (static + dynamic) | **Done** | `MemoryClient.profile`, `profile_activity.py` |
| Framework integrations | **Done**: LangChain/LangGraph, AutoGen/AG2, CrewAI, LlamaIndex, Google ADK `LlmAgent.before_model_callback`, Strands, OpenAI Agents per-run input filter/hooks, Pydantic AI `ProcessHistory`/after-run capabilities; Vercel AI SDK V4 and Mastra `MessageList` input/output middleware in TS | `frameworks.py`, `integrations/google_adk.py`, `integrations/`, `sdk/typescript`; provider-free runtime checks pass for Google ADK, CrewAI, LangGraph, Microsoft Agent Framework, OpenAI Agents, Pydantic AI, Vercel AI SDK, and Mastra; remaining tool-only integrations and real provider transport still need validation |
| Observations and reflection | **Done** | `observations.py`, `memory_control.py` |
| Knowledge wiki | **Done** | `wiki.py`, `knowledge_pages.py` |
| Data connectors | **Done**: GitHub, Slack, Drive, Gmail, Notion, OneDrive, Confluence, Jira, Linear, Zendesk, ServiceNow, Salesforce, Intercom, Greenhouse | `connectors/knowledge.py` |
| Multi-signal retrieval | **Done** | `search_recipes.py`, conversation hybrid recall |
| Bi-temporal fact invalidation | **Done**; `fact invalidate` (end validity without replacement or deletion, `--at`, as-of history kept) added **(this pass)** | `hierarchical.py`, `graph.py` |
| Pipeline recovery | **Done**: per-file ledger, durable job queue with lease reclaim; `ingest --background` and a SIGTERM-draining `jobs run --watch` added **(this pass)** | `ingest/pipeline.py`, `jobs.py` |
| Provider pattern | **Done**; reranker registry added **(this pass)** | `providers.py` |
| Multi-tenancy isolation | **Done** (Hub RLS; per-owner/dataset local vector stores). **Fixed (this pass)**: the `traces` policy's commons-sharing clause also applied to DELETE, so any org could delete another org's shared rows through a query missing its own org filter; sharing is now a SELECT-only policy | `hub/`, `providers.BackendFactory`, migration `c1d4e8f2a9b6` |
| Markdown-first with file watcher | **Done**; `lesson edit` added **(this pass)** | `watch.py`, `commands/lesson_cmd.py` |
| Code graph | **Done** | `code_graph.py` |
| Local/offline mode | **Done (this pass)**: `--offline` / `COMMONTRACE_OFFLINE`, and `--local` (offline plus the in-process model) | `offline.py`, `local_llm.py` |
| Migration from Mem0/Letta/Zep/Graphiti, COGX | **Done** | `migration.py`, `interop.py` |

### Reliability and operations

| Report item | Status | Where |
| --- | --- | --- |
| Four-branch exception hierarchy with CLI remediation | **Done (this pass)**: generic CLI errors now use the shared deterministic hint; `remediation.hint` remains a compatibility re-export | `exceptions.py`, `cli.py`, `remediation.py` |
| Structured logging, rotating file | **Done** | `telemetry.py`, `COMMONTRACE_LOG_FILE` |
| Health endpoints | **Done** for the Hub; gateway `/v1/health/live` and `/v1/health/ready` added **(this pass)** | `hub/observability.py`, `gateway.py` |
| Security scanning (CodeQL, Scorecard, Trivy, SBOM) | **Done** | `.github/workflows/` |
| Threat model in SECURITY.md | **Done (this pass)** | `SECURITY.md` |
| Helm chart (probes, limits, PDB, NetworkPolicy, ServiceMonitor) | **Done** | `deploy/helm/commontrace-hub/` |
| Docker Compose for local development | **Done (this pass)**: `--profile local` adds the gateway and MCP server (read-only, loopback ports, health checks, optional `.env`); CI builds, scans and starts the image | `docker-compose.yml`, `Dockerfile.local` |
| Tiered tests, 80% coverage gate, Makefile | **Done** | `pyproject.toml`, `Makefile`, CI |
| `_FILE` secrets | **Done**; Hub client API key now honours it **(this pass)** | `secrets_provider.py` |
| Rate-limit auto-detection | **Done** | `overload.py` |
| CI path filtering | **Done (this pass)**: a `changes` job classifies the diff; documentation-only changes skip the 13 expensive jobs (SDK builds, images, compose stack, optional engines, perf and quality gates). Core, dev, Hub, coverage, security and deploy-asset jobs always run, since tests read the docs; unknown history never skips | `scripts/ci_changes.py`, `.github/workflows/ci.yml` |

### Benchmark recommendations

| Recommendation | Status and measured effect |
| --- | --- |
| P1 Semantic embeddings | Already supported; **measured for the first time (this pass)**: see the dense rows below |
| P2 Entity linking | **Done** (entity boost in conversation recall; mem0-style entity signal in `search_recipes`) |
| P3 Adaptive token budget | **Done (this pass)**: BEAM event ordering 59.6% → 96.9%, summarization 28.5% → 50.5% evidence at a 1,500 floor |
| P4 Cross-encoder reranking | Already supported; **measured (this pass)**: LoCoMo 82.00% -> 83.90% evidence at 1,500 tokens over dense alone |
| Preference following | **Measured (this pass)**: dense retrieval raises LongMemEval preference evidence 52.94% -> 69.61% (@1.5K) and 52.94% -> 75.49% (@4K) on the paired subset. A lexical alternative (weighting the user's own turns on advice questions) lowered it from 61.7% to 52.8% on the full split and was removed |
| Gap bridging (`Options.bridge_turns`, `--bridge-turns 6`) | **Measured (this pass), kept opt-in**: fills the turns between two nearby hits of one session at the same token budget. AMA-Bench evidence 66.90% -> 67.26% (complete 39.38% -> 42.26%) @1.5K; LoCoMo 78.69% / 86.31% -> 79.14% / 87.02% (multi-hop 52.08% -> 52.56%); BEAM 70.87% / 80.67% -> 70.92% / 80.74%; but LongMemEval 79.60% / 85.92% -> 78.86% / 85.62%. Not a uniform win, so off by default; turn it on for agent trajectories and step-range questions |
| Advice-question budget class | **Tried and rejected (this pass)**: doubling the budget for advice/recommendation questions raised LongMemEval preference evidence 61.67% -> 65.00% but lowered BEAM 79.37% -> 79.30% and moved LoCoMo only 80.29% -> 80.42%, at more tokens everywhere |
| Summarization pipeline | Adaptive budget measured above; model summaries exist (`conversation summarize --model`) |
| Temporal hints, event ordering | **Done** (`temporal_intent.py`; adaptive budget for ordering questions) |
| Benchmark CI on PRs | **Done** (stratified LoCoMo gate) |

## Measured results

Evidence = share of gold source turns present in the delivered context (not
answer accuracy). All runs: full datasets (LoCoMo 1,540, LongMemEval-S 500,
BEAM 100K 400), keyword arm unless noted, budgets 1,500 and 4,000 tokens,
`benchmarks.conversation_bench`, frozen worktrees, dataset SHA-256 as in
`docs/performance.md`.

| Run | LoCoMo @1.5K / @4K | LongMemEval @1.5K / @4K | BEAM @1.5K / @4K |
| --- | --- | --- | --- |
| Lexical, fixed (reproduces README) | 78.69% / 86.31% (1,441 / 3,852 tok) | 79.60% / 85.92% (1,463 / 3,926 tok) | 70.87% / 80.67% (1,465 / 3,923 tok) |
| Lexical, adaptive budget | 80.29% / 87.30% (1,697 / 4,347 tok) | 83.02% / 90.02% (2,447 / 6,181 tok) | 79.37% / 83.16% (2,804 / 6,916 tok) |
| Dense (arctic-m), fixed | 82.00% / 91.36% (1,452 / 3,887 tok) | see subset below | 71.21% / 82.99% (1,469 / 3,939 tok) |
| Dense + cross-encoder, fixed | 83.90% / 91.51% (1,452 / 3,887 tok) | not run | not run |
| Dense, adaptive budget | 83.73% / 92.53% (1,711 / 4,559 tok) | not run | 80.15% / 86.10% (2,819 / 7,053 tok) |

LoCoMo multi-hop evidence, the report's largest gap: 52.08% / 66.61% lexical,
60.73% / 79.47% dense, 66.66% / 83.99% dense with the adaptive budget. Turn
Recall@5 rises from 54.78% (lexical) to 60.96% (dense) and 65.46% (dense +
cross-encoder). The cross-encoder's p50 recall latency on this shared 4-core CPU
was about 480 ms against about 55 ms dense-only; choose it where accuracy matters
more than latency.

LongMemEval-S dense, on a seeded stratified 100-question subset (embedding the
full split on this 4-core CPU would take over a day), paired with a lexical run on
the identical subset:

| Subset run (n=100) | Evidence @1.5K / @4K | Preference @1.5K / @4K | Turn Recall@10 |
| --- | --- | --- | --- |
| Lexical | 79.76% / 84.52% | 52.94% / 52.94% | 75.00% |
| Dense (arctic-m) | 81.80% / 89.12% | 69.61% / 75.49% | 82.14% |

### Local reader answer accuracy

A seeded, stratified 100-question LoCoMo subset at a 1,500-token floor, answered
in-process by Qwen2.5-0.5B-Instruct (greedy, 64-token cap) and graded by the
deterministic `exact` judge (gold contained in the answer, or token F1 >= 0.5).
A 0.5B model on CPU and a strict string judge both understate accuracy, so these
numbers compare configurations; they are not comparable to published LLM-judged
scores.

| Run | Accuracy | Evidence | Single-hop | Multi-hop | Temporal | Open-domain |
| --- | --: | --: | --: | --: | --: | --: |
| No memory | 4% | - | 7.7% | 0.0% | 0.0% | 8.7% |
| Lexical, fixed (1,441 tok) | 19% | 61.33% | 34.6% | 11.5% | 20.0% | 8.7% |
| Dense + adaptive (1,788 tok) | 26% | 75.82% | 38.5% | 23.1% | 24.0% | 17.4% |

The ordering matches the evidence measurements: better-delivered context gives a
better answer from the same reader.

### Model-judged answer accuracy (Gemma 4 31B)

Gemma 4 31B (`gemma-4-31b-it`, Google AI free tier) as both reader and judge,
the LoCoMo judge profile (`locomo-downstream-binary-v1`), a seeded stratified
200-question LoCoMo subset, keyword retrieval at a 1,500-token budget
(1,435 tokens delivered on average), 800 calls, $0 spend, frozen worktree:

| Run | Accuracy [95% CI] | Single-hop | Temporal | Multi-hop | Open-domain |
| --- | --: | --: | --: | --: | --: |
| No memory | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| Lexical memory, 1,500 tokens | 68.5% [61.8, 74.5] | 86.3% (n=51) | 78.4% (n=51) | 73.1% (n=52) | 32.6% (n=46) |

The reader prompt tells the model to say it does not know when the context
lacks the answer, so the no-memory arm abstains on every question (it does not
guess); the lift is the whole 68.5 points. Full-context is not run: a LoCoMo
conversation exceeds the free tier's 16K input tokens per minute. The same
model reads and judges, which can flatter it; these numbers are not comparable
to published scores that use other readers and judges.

### Agentic benchmarks

AMA-Bench (arXiv 2602.22769) open-ended QA over 208 long agent trajectories
(actions and observations, 29,888 fields; the 63 over 100K characters are
clipped). It ships no evidence labels, so evidence is *derived*: the action and
observation of every step a question or reference answer names. 2,496 questions
name a step; the rest are excluded from evidence, never counted as misses.
Keyword arm:

| AMA-Bench run | Evidence @1.5K / @4K | Recall | Causal inference | State updating | State abstraction |
| --- | --- | --- | --- | --- | --- |
| Fixed budget | 66.90% / 78.62% (1,435 / 3,663 tok) | 71.48% / 81.15% | 64.64% / 77.95% | 63.91% / 74.47% | 64.26% / 80.85% |
| Adaptive budget | 72.87% / 83.93% (2,258 / 5,122 tok) | 75.61% / 84.47% | 70.68% / 85.03% | 69.11% / 81.24% | 76.28% / 85.77% |

MemoryAgentBench (arXiv 2507.05257) Accurate Retrieval split: 22 contexts of
roughly 200K-420K tokens each. Only answer-in-context is scored (is an accepted
answer present in the delivered context); EventQA is multiple choice over
paraphrased event summaries that never appear verbatim, so its 1,500 questions
are left unscored, leaving 398 scorable questions:

| MemoryAgentBench run | Answer in context @1.5K / @4K | RULER QA | LongMemEval (MAB) |
| --- | --- | --- | --- |
| Fixed budget | 64.82% / 71.11% (1,456 / 3,843 tok) | 79.59% / 86.22% | 50.50% / 56.44% |
| Adaptive budget | 67.84% / 76.63% (2,776 / 7,596 tok) | 79.59% / 87.76% | 56.44% / 65.84% |

The LongMemEval-derived contexts are repr'd chat histories; reading them as
dated user/assistant sessions instead of 2,000-character blobs raised that
slice from 37.13% / 42.57% (fixed) and 40.59% / 52.48% (adaptive) to the
numbers above, and the scorable total from 58.04% / 64.07%.

The Conflict Resolution split (FactConsolidation, later facts supersede earlier
ones) also loads, one ordered turn per fact, but containment cannot score it:
its answers are entity names that recur in a median of 1-24 unrelated facts per
context, so "answer present" says nothing about whether the latest fact was
found (it reads 100% on single-hop). It is supported for model-judged runs
(`--answer`) only. Test-Time Learning (label and item ids) and Long-Range
Understanding (summaries) are not supported for the same reason.

Neither is the benchmarks' official, model-judged score.

Dense retrieval closes most of the preference gap the report identified; the
lexical arm cannot match "battery life" to a stored "power bank". The dense run's
1,500-token p50 (65 s) includes building each question's index on first recall
and is not a steady-state latency.

On BEAM 100K dense retrieval adds little at 1,500 tokens (70.87% -> 71.21%) and
more at 4,000 (80.67% -> 82.99%); the adaptive budget is the larger lever there.
Latency figures from runs that shared the CPU with concurrent embedding jobs (the
dense adaptive BEAM run reported a 1.1 s p50 at the 1,500 floor) are not
latency measurements.

At equal mean tokens on LoCoMo (about 1,700), adaptive allocation scores 80.29%
against 79.76% for a uniform budget: a small gain that comes from spending tokens
on list and multi-hop questions. On BEAM the 1,500-floor adaptive run reaches 98%
of the fixed-4,000 evidence with 71% of the tokens.

CausalMemBench (10 seeds): see [causalmembench.md](../benchmarks/causalmembench.md).
