# Scorecard: targets A-G

Each target from the master prompt, its status on this branch, the measured number and the command that
produced it. "Met" needs a measurement; a feature that exists but has not been measured is "not measured".
Details and the full audit are in [implementation-status.md](implementation-status.md).

| Target | Status | Measured | Command |
| --- | --- | --- | --- |
| **A.** Accuracy parity with re-run competitors, LLM-judged, with CIs | Not met | Evidence share on full LoCoMo at 1,500 tokens: 78.69% lexical, 82.00% dense, 83.90% dense + cross-encoder. LLM-judged: Gemma 4 31B as reader and judge, 200 LoCoMo questions, lexical memory at 1,500 tokens: 68.5% [61.8, 74.5] (single-hop 86.3%, temporal 78.4%, multi-hop 73.1%, open-domain 32.6%). Competitors not re-run through the same reader and judge | `python -m benchmarks.conversation_bench --dataset locomo ...` |
| **B.** Top-3 on two agentic leaderboards, with cost and latency | Not established | AMA-Bench evidence 66.90% / 78.62% (fixed), 72.87% / 83.93% (adaptive); MemoryAgentBench answer-in-context 64.82% / 71.11% and 67.84% / 76.63% at 1.5K / 4K. Not the benchmarks' official model-judged scores | `--dataset ama`, `--dataset mab` |
| **C.** >= 90% evidence within 1,000 tokens on LongMemEval-S | Not met | At 1,000 tokens (100-question subset): lexical 74.66%, dense 77.89%, dense + cross-encoder 84.18%, 86.56% with the cross-encoder weighted 3x (not confirmed on held-out LoCoMo, so not the default). The remaining gap is ranking gold turns inside correctly found sessions | `--dataset longmemeval --budget 1000 --embedder arctic-m --rerank cross-encoder --limit 100` |
| **D.** Hybrid recall p50 < 50 ms / p95 < 150 ms at 1M memories, recall@10 >= 0.97 of exact | Met for the vector path; hybrid not measured | 1M synthetic 384-d vectors, pgvector HNSW (m 24, ef_construction 200), ef_search 100: p50 7.2 ms, p95 10.7 ms, recall@10 1.000. Default build (m 16): recall plateaus at 0.945 | `python -m benchmarks.vector_scale_bench --n 1000000` |
| **E.** +5pp at 80% power within 2,000 occasions, anytime-valid coverage >= 95% | Coverage met; power not met | Time-uniform coverage >= 96.7% in all 45 cells; +5pp power 0.31-0.33 (no predictive stratum) to 0.64-0.70 (strong); +10pp 0.96-1.00 with adaptive allocation | `python -m benchmarks.adaptive_allocation_bench --reps 400` |
| **F.** 0% attack success on poisoning suites at >= 95% clean utility | Met (on PoisonBench) | 0% across nine attacks with an authority policy, 100% clean utility; without one, all six origin attacks succeed | `python -m benchmarks.poisonbench --variants 5` |
| **G.** `pip install` / `npm i` / `docker run` to first recall in < 60 s | Not measured | Release workflow, generated SDKs and local image exist; nothing published to registries | — |


## Next-generation release gates (2026-10-10)

This is a historical snapshot. The continuation measurements below supersede
its current write and native-latency acceptance claims.

| Gate | Status | Evidence |
| --- | --- | --- |
| Single-write p95 <20 ms at 1M | **Measured pass** | 5.031 ms, real hierarchical APIs, 20 trials, synthetic selective workload |
| Batch >=2,000 facts/s at 1M | **Measured pass** | 3,052 facts/s, one 1,000-fact batch |
| Hybrid p95 <50 ms local / <150 ms server at 1M | **Unverified** | 64-fact synthetic-adapter `search_recipes.search` smoke: 14.16 ms p95 over two samples; plumbing only, not the 1M or server gate |
| Restart without re-embedding | **Fixture contract pass** | 64-fact fresh-process smoke retained both hybrid candidates, excluded the scoped-out row, replayed zero projection events and made zero document-embedding calls; 1M restart timing remains unverified |
| Injector precision >=0.80 | **Fixture measured** | Actual BGE-small, maintainer-labelled coding relevance; not production/SWE precision |
| Warm hook p95 <50 ms | **Load-sensitive; not established universally** | See signed report for idle and concurrent runs; native socket deadline is separate from total host startup |
| Fresh native process p95 <50 ms | **Fixture measured pass** | 29.75 ms, 50 trials, all delivered; warmup separate, no semantic model. Retained baseline: 52.50 ms miss |
| LoCoMo >=0.85 GPT-class at <=1,500 tokens | **Unverified** | No configured GPT-class reader/judge credentials or endpoint |
| LongMemEval-S >=0.90; BEAM 100K/1M and categories | **Unverified** | Model-backed required evaluations not run |
| Coding uplift >=5pp, sequential lower CI >0 | **Unverified** | Real patch/test randomized harness implemented; no actual SWE model run |
| Harmful-retirement N and lower extraction cost | **Unverified** | CodingLessonBench drives per-task randomized assignments through the real journal retirement path and reports N only on a SOUND transition; fixtures validate lifecycle wiring, but no labeled real-runner N or extraction-cost comparison has been measured |
| Poison resistance | **Fixture tested** | Eleven injector poison/status cases; no successful injection, clean utility retained; novel attacks not covered |
| First injected lesson <2 minutes | **Unverified** | Setup previews eligible existing lessons; empty stores require reviewed/evaluated evidence |
| F7: fewer modules; <40 documented config vars | **Not met** | 300 Python modules in the 2026-10-12 Graphify audit; 25 primary variables and 155 schema alias entries overlap on 24 names, leaving 156 unique supported environment variables |
| Signed public aggregate report | **Implemented** | Ed25519 signature with repository-pinned session public key; no private trajectories |

Historical P0 disk exhaustion remains recorded in
[measurements/p0-storage.json](measurements/p0-storage.json). Compact schema v2
resolved that failure in the measured 1M run. See
[next-generation-evidence.md](next-generation-evidence.md) for scope and reproduction.

## P0 continuation gate check (2026-10-11)

The first continuation run missed both write targets. Stop phase advancement as the attached
build prompt requires; earlier release measurements remain historical evidence.

| Gate | Measured here | Decision |
| --- | --- | --- |
| Million-fact single write | 31.43 ms p95; target <20 ms | **Miss** |
| Million-fact batch | 759 facts/s; target ≥2,000 | **Miss** |
| Sparse-overlap search | 27.68 ms p95 | Does not verify hybrid latency |
| Hybrid/server latency and zero-re-embedding restart | Not exercised | **Unverified** |
| Named CodeQL false positives | Alerts #10 and #1 dismissed after review and authentication tests | **Resolved** |
| Pinned LoCoMo retrieval regression | 154 questions; no change at 1,500 or 4,000 tokens | **Pass**, not answer accuracy |
| Full application / PostgreSQL Hub suites | Baseline application run superseded and interrupted; Hub 2,510 passed, 2 skipped | Storage candidate failed; frozen follow-up `fa1ef0c` passes 6,678 tests with 118 skips |

Commands, signed reports, source hashes and environment limitations are in
[the P0 continuation audit](next-generation-p0-audit.md). This run used concurrent
validation on the current host; it does not establish a causal performance change
from the older release. The full next-generation objective remains incomplete.

## P0 write optimization (2026-10-11)

| Gate | Measured here | Decision |
| --- | --- | --- |
| Million-fact single write | 3.23 ms p95; target <20 ms | **Pass** |
| Million-fact batch | 1,181 facts/s; target ≥2,000 | **Miss** |
| Sparse-overlap search | 0.261 ms p95 | Does not verify hybrid latency |
| Hybrid/server latency and zero-reembedding restart | Not exercised | **Unverified** |
| Storage/cursor/concurrency/erasure contracts | 92 passed | **Pass** |
| Pinned LoCoMo retrieval regression | 154 questions; no regression at 1,500 or 4,000 tokens | **Pass**, not answer accuracy |
| Measured storage source full suite | 6 failed, 6,666 passed, 118 skipped | **Failed** |
| Follow-up storage/journal/hook contracts | 135 passed; all six failures reproduce before the fixes and pass afterward | **Focused pass** |
| Frozen follow-up full application suite | `fa1ef0c`: 6,678 passed, 118 skipped, 26 warnings | **Pass**; later Lance changes separately covered by ten scoped checks |
| Latest fresh million-fact writes | Frozen `7e0a224`: 4.69 ms single-write p95, 1,739 facts/s batched | **Single pass; batch miss**; benchmark exits 1; no controlled gain over `aeadf5d` established |
| Bounded-FTS batch candidate (2026-10-12) | 4.263 ms single-write p95; 1,819 facts/s batched at 1M | **Single pass; batch miss**; source hashes and command in [candidate measurement](next-generation-p0-batch-projection.md); no controlled gain claimed |
| Sparse conflict-slot fast path (2026-10-12) | 6.127 ms single-write p95; 1,962.476 facts/s batched at 1M | **Single pass; batch miss** by 37.524 facts/s; source hash and command in [measurement](measurements/p0-batch-conflict-slot-candidate.json) and rationale in [candidate note](next-generation-p0-conflict-slot-fastpath.md); separate runs do not establish a causal gain |
| Rejected sparse-slot batch-prefetch candidate (2026-10-12) | 3.757 ms single-write p95; 1,841.019 facts/s batched at 1M | **Not integrated; batch miss**; 30K smoke was flat versus same-HEAD baseline. Exact command and source hashes in [measurement](measurements/p0-batch-sparse-slot-batching-candidate.json) |
| Historical Node transport | 152.27 ms p95, 50/50 directives delivered; target <50 ms | **Latency miss**; reported checkout 54681d0, imported dependency binding incomplete |
| Isolated Python transport, frozen `4b1d2eb` | Three runs: 51.59, 46.64, 48.64 ms p95; 150/150 delivered | **Latency remains open**: one of three rounds misses; full suite 6,709 passed / 126 skipped |
| Global near-term migration, frozen `19c914b` | 1,044,020 facts; 9,396,194 → 3,132,080 lexicon rows; 55.65 s conversion; all event/payload checksums unchanged | **Migration diagnostic pass**; 192 scoped checks; frozen `d50d54b` full suite passes 6,722 /126 skipped before later staging/reuse changes |
| Corrected staged/bulk/cache prototype | 1,870–1,895 facts/s; FIFO/encoder experiment 1,896–2,130 | **Unaccepted**: misses retained, staged semantic admission unverified, concurrent validation; earlier mixed-layout timings excluded |
| Scalar fact snapshots | 96.5–99.0K → 173.1–176.1K calls/s in a serialization-only profile; complex payloads retain recursive semantics | **Component diagnostic**; does not establish batched ingest throughput |
| Production eager-FTS staging, `ce1abe2` | Candidate rounds 1,601 / 1,645 facts/s; frozen baseline 1,177 / 1,236 | **Batch miss**; growing million-copy under concurrent validation |
| Transaction-local lexical reuse, `64b64f4` | Candidate 1,741 / 1,756 facts/s; baseline 1,619 / 1,667 | **Batch miss**; bounded entries/weight and physical cleanup tested |
| Guarded staged snapshots, `cd8c1dc` | Candidate 1,722 / 1,653 facts/s; baseline 1,730 / 1,129 | **Batch miss; no causal speed claim**; 73 focused +149 broader contracts pass; frozen suite's ID mutation failure was fixed on the descendant |
| Latest staged-write Graphify | 298 modules, 300 candidates, zero dangling endpoints or policy violations | **Pass**, unchanged 306/303 ceilings; [signed diagnostic evidence](next-generation-staged-write-evidence.md) |
| Follow-up Graphify | 297 modules, 303 reachability candidates, zero dangling endpoints | **Pass**, unchanged 306/303 ceilings |
| Optional Lance at 10K vectors | ANN p95 7.15 ms, recall@10 1.0 on 20 paired synthetic queries | **Component measured**; million-fact hybrid unverified |
| Optional Lance at 1M vectors | ANN p95 13.04 ms; mean recall@10 0.91, minimum 0.70 | **Recall target missed**; component only, full hybrid unverified |
| Optional Lance refinement on retained 1M index | Same-index recall 0.905 → 0.98; held-out recall 0.995; three committed product rounds p95 <25 ms | **Component targets pass** on these synthetic probes; full hybrid unverified |
| Optional Lance fresh refined 1M index | ANN p95 19.69 ms; mean recall@10 0.995, minimum 0.90; explicit reproducer exits 0 | **Component targets pass**; separate index build, full hybrid unverified |

Phase advancement remains stopped. The benchmark exited 1 on the batch miss.
These are separate runs on the same host with concurrent validation, not a
controlled causal comparison. Commands, source hashes and signed aggregates are
in the [write optimization report](next-generation-p0-write-optimization.md).

## Continued feature slices (2026-10-11)

| Feature slice | Measured here | Decision |
| --- | --- | --- |
| OAuth/KMS and config migration consistency | 179 focused configuration, authentication, gateway, Hub and KMS tests passed | **Component pass**; full suite pending |
| Learner prompt bounds | 32 learner contract tests passed, including whole-prompt wrapper sizing and cursor preservation | **Component pass**; default UTF-8 text bound excludes provider templates; model-token usage and SWE lift remain unmeasured |
| Hybrid graph seeds and multi-round evidence | 19 focused tests passed; paired synthetic baseline/candidate raised distinct facets 1/2 → 2/2, prevented withdrawn-fact return, and reduced repeated-query judge calls 3 → 2 | **Policy behavior pass**; model answer quality and 1M hybrid latency remain unmeasured |
| Integrated application/e2e suite after these slices | 6,821 passed, 116 skipped, one failure in the static core-test Hub-import guard; affected config file plus guard regression pass 31 tests after adding the required `pytest.importorskip` calls | **Broad run complete with test-only failure fixed in focused rerun; full suite not repeated after the fix** |
| PostgreSQL Hub suite | 2,506 passed, 5 skipped, one RLS test failed because the local `commontrace_dev` test role lacks `CREATEROLE`; that test passes under a temporary CI-equivalent superuser | **Environment mismatch isolated; whole Hub suite was not rerun under the superuser role** |

The retrieval comparison is a deterministic policy harness over temporary SQLite
stores, with synthetic dense scores and judge responses. Its artifact includes
the measured sources and hashes at
[hybrid-evidence.json](measurements/hybrid-evidence.json); it is not a quality or
latency benchmark.

## Integrated continuation (2026-10-12)

| Feature / gate | Measured here | Decision |
| --- | --- | --- |
| Bounded FTS batch projection at 1M | Single-write p95 4.263 ms; batch 1,819 facts/s | **Single-write pass; batch miss** against <20 ms / ≥2,000 facts/s. Separate runs do not establish a causal throughput gain; see [candidate report](next-generation-p0-batch-projection.md) |
| Hybrid search and restart at 1M | Synthetic-adapter production API, 30 trials per backend: SQLite p50/p95 2,797.91/2,828.93 ms; Lance 169.16/200.82 ms. Both miss the local p95 <50 ms gate. | **Latency miss** on both local backends. Fresh-process restart passed on the fixture for each backend: 1M cached vectors, zero projection replay and document embeddings, one query embedding, expected candidates retained, and out-of-scope ID excluded. Real-model latency and server latency remain unmeasured; Postgres hybrid is unsupported by the current API. See [acceptance note](next-generation-hybrid-acceptance.md) and [reports](measurements/f1-hybrid-1m-synthetic-sqlite.json), [Lance](measurements/f1-hybrid-1m-synthetic-lance.json) |
| Sparse conflict-slot fast path at 1M | Single-write p95 6.127 ms; batch 1,962.476 facts/s | **Single-write pass; batch miss** against <20 ms / ≥2,000 facts/s; 37.524 facts/s short. See [measurement](measurements/p0-batch-conflict-slot-candidate.json) and [candidate note](next-generation-p0-conflict-slot-fastpath.md) |
| Retrieval telemetry regression | 154 stratified LoCoMo questions; retrieval aggregates unchanged at 1,500 and 4,000 tokens | **Pass**, retrieval-only; judged answer accuracy remains unmeasured. [Paired report](measurements/retrieval-telemetry-regression.json) |
| Harmful lesson retirement lifecycle | Randomized assignment uses isolated shadow memory and journal; fixtures require actual SOUND retirement before reporting N | **Implementation and fixture pass**; real-runner N and extraction-cost comparison remain unmeasured |
| Integrated application and e2e suite | 6,825 passed, 126 skipped, 26 warnings; 4,194.17 seconds | **Pass**; `env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT /tmp/commontrace-goal-env/bin/python -m pytest tests/ e2e_tests/ -q` |
| Native hook transport | Baseline p95: 55.16, 49.51, 49.02 ms; candidate p95: 43.47, 42.58, 43.37 ms; 150/150 deliveries per candidate round; 56 focused tests pass | **Transport fixture pass**: candidate passes <50 ms in all three rounds; cold broker startup and broad host-load variation remain unmeasured. See [measurement and analysis](next-generation-hook-startup.md) |
| Google ADK and CrewAI kickoff integrations | One-call scoped ADK before-model adapter; CrewAI sync before/after kickoff callbacks; combined hook, middleware, hybrid, and integration contracts: 94 passed, 6 skipped | **Component contract pass**; live framework execution remains unverified. Native CrewAI `akickoff()` may block its event loop with synchronous callbacks; see [ADK note](next-generation-google-adk-callback.md) and [CrewAI note](next-generation-crewai-kickoff-memory.md) |

Security scanning, strict typing, OpenAPI generation checks, docs drift checks,
and dependency audits also passed on the integrated source. The complete
feature objective remains open: the 1M batch-rate threshold, the local hybrid
latency gate (missed on both tested backends), server retrieval, real-model
retrieval quality, real model-backed SWE outcomes, and universal hook latency
are not established. The 1M restart behavior passed only on the synthetic
fixture.

## Beyond the targets

| Area | Measured | Command |
| --- | --- | --- |
| Confounded withdrawal | Correlational withdrawal lost a helpful memory in 10/10 runs; randomized withdrawal never withdrew a memory that does not hurt | `python -m benchmarks.causalmembench --seeds 10 ...` |
| Multi-principal governance | 100% utility, 0 leaks in 117 checks, 0 forgotten records delivered in 36 | `python -m benchmarks.govbench` |
| Embodied fleet | Real-floor shortcut HURTS -0.186 [-0.328, -0.037] (truth -0.200); sim/real pooling refused; withdrawn 100/100 | `python -m benchmarks.robot_fleet_demo --occasions 1200` |
| API contract | 58 gateway operations validated against live responses; 106 Hub operations in OpenAPI 3.1 | `python scripts/export_openapi.py --check`, `python scripts/export_hub_openapi.py --check` |
