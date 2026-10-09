# Implementation status: master prompt (Part G) and competitive report

Audited 2026-10-09 against `claude/commontrace-competitive-analysis-rnt3xr`
(which includes `feat/commontrace-memory-evolution`). "Done" means code, tests
and docs exist in this repository. Numbers are measured on this machine with the
commands in [Measured results](#measured-results). Nothing here is a claim about
hosted infrastructure, live customers or model-graded answer accuracy, because
none of those were available to measure.

Legend: **Done** · **Done (this pass)** (added or fixed in this audit) ·
**Partial** · **Not done** (with the reason).

## Part G: master prompt

| Item | Status | Where |
| --- | --- | --- |
| 0.1 Scoreboard with dense + cross-encoder on | **Done (this pass)**: LoCoMo (dense, dense + cross-encoder, dense + adaptive), BEAM (dense, dense + adaptive), LongMemEval (dense, paired 100-question subset) | `benchmarks/conversation_bench.py --embedder arctic-m` |
| 0.2 Model-backed answer accuracy | **Partial (this pass)**: no hosted credentials here, so an in-process local reader (`COMMONTRACE_LLM_PROVIDER=local`, Qwen2.5-0.5B-Instruct) with the deterministic `exact` judge was run on a 100-question LoCoMo subset (see below); hosted-model, LLM-judged accuracy is still not measured | `--answer --judge exact`, `local_llm.py`, `benchmarks/judges/` |
| 0.3 Competitors through the same harness | **Partial**: local raw-source mem0 and Graphiti profiles; managed services not reproduced | `benchmarks/vendor_adapters.py` |
| 0.4 Agentic benchmarks (MemoryArena, AMA-Bench, MemGym, Evo-Memory) | **Partial (this pass)**: AMA-Bench open-ended QA (208 agent trajectories, derived step evidence) and MemoryAgentBench Accurate Retrieval (answer-in-context) run through the harness; interactive environments (MemoryArena, MemGym, Evo-Memory) still need their own agent loops | `--dataset ama`, `--dataset mab`, `benchmarks/dolphinbench/` |
| 0.5 Leaderboard from signed manifests, `reproduce.sh` | **Done** | `benchmarks/phase0*.py`, `reproduce.sh` |
| WS1 Multi-signal fusion, recipes, MMR, decision reranker | **Done** | `search_recipes.py` |
| WS1 Named second-stage rerankers | **Done (this pass)** | `providers.reranker("mmr" \| "cross-encoder" \| ...)` |
| WS1 Query planning, entity resolution, GLiNER, additive extraction | **Done** | `query_plan.py`, `local_extraction.py`, `additive_extract.py` |
| WS1 Adaptive context budget by question shape | **Done (this pass)** | `conversation.search.budget_for`, `--budget auto` |
| WS1 Store abstraction and scale backends (pgvector HNSW, LanceDB, Neo4j/FalkorDB) | **Done** | `store.py`, `vector_lance.py`, `graph_backends.py` |
| WS2 Exploration slots, SNIPW, OPE release gate | **Done** | `causal_policy.py`, `policy.py`, `gate --policy` |
| WS2 Adaptive allocation | **Partial (this pass)**: graduation of proven memories (`CausalMemory(graduate=True)`); no bandit reallocation of the holdout rate | `measure.py` |
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
| WS8 Console, Memory Palace, Needs Attention | **Partial**: overview, Needs Attention review queue with revision-checked actions, memories, live activity, experiment readouts, command center; no executive lift-per-token view, release diffs, experiment designer, forensics report view or weekly digest export | `ui/`, `/v1/palace` |
| WS9 CausalMemBench | **Done (this pass)** | `benchmarks/causalmembench.py`, `docs/benchmarks/causalmembench.md` |
| WS10 Embodied fleets (sim/real separation, protected memories) | **Done**; multimodal episodes via ingestion | `fleet.py`, `gateway.py`, `ingest/multimodal.py` |

### Part G items not yet done

Checked against the code on 2026-10-09. Each is either blocked by something this
environment does not have or is open work.

| Item | Why not |
| --- | --- |
| Target A: accuracy parity with re-run competitors, LLM-judged, with CIs | No hosted reader or judge credentials here; only the local 0.5B reader with the exact judge was run |
| BEAM 1M and 10M | Not run (CPU-only machine; the 100K split is measured) |
| 0.3 adapters for Cognee, Hindsight, EverOS, Supermemory local, Letta | Only local mem0 and Graphiti profiles exist |
| 0.4 MemoryArena, MemGym, Evo-Memory, DolphinBench completion | Need interactive agent environments; DolphinBench harness is a stub |
| Target B: top-3 on two agentic leaderboards | Not established; AMA-Bench and MemoryAgentBench are measured as evidence, not judged score |
| Target C: >= 90% evidence within 1,000 tokens on LongMemEval-S | Not met; measured 79.60% at 1,500 tokens (lexical), 83.02% adaptive |
| Target D: hybrid p50 < 50 ms / p95 < 150 ms at 1M memories | Not benchmarked at 1M |
| Target E: +5pp at 80% power within 2,000 occasions | Not demonstrated; CausalMemBench shows the 10% default is slow, and bandit reallocation of the holdout rate is not built (it would change the estimator; needs a design review) |
| Target G: `pip install` / `npm i` / `docker run` to first recall in < 60 s | Release workflow and images exist; nothing is published to PyPI, npm or a registry yet |
| WS2 interaction effects for co-firing lessons; auto-drafted narrower `applies_when` | Subgroup effects flag `CROSSING`; no factorial interaction reading or automatic draft |
| WS3 DPO export | SFT export only (`evidence-linked-sft`), no DPO pairs |
| WS5 publisher revenue share | Licence and price are signed into listings; payment and revenue share are not wired into value billing |
| WS8 items listed above | Open |
| WS9 paper draft in `docs/research/` | Not written |
| WS10 end-to-end robot fleet demo with a causal report | Not built; the gateway, fleet and multimodal pieces exist |
| Process: `docs/plans/<ws>.md` per workstream and `docs/strategy/scorecard.md` | Not created; this status document plays the scorecard role |

## Competitive intelligence report

### Feature and architectural gaps

| Report item | Status | Where |
| --- | --- | --- |
| MCP server | **Done** (local stdio/HTTP/SSE and Hub, with `search_traces`, `contribute_trace`, `get_trace`, `vote_trace`, `amend_trace`); containerized local MCP over streamable HTTP added **(this pass)** | `mcp_server.py`, `hub/server.py`, `Dockerfile.local` |
| CLI with agent signup | **Done**; `init --agent-caller NAME` added **(this pass)** | `commands/init_cmd.py` |
| Dashboard | **Done** (no-build console, not Next.js, by design) | `commontrace/ui/` |
| User profiles (static + dynamic) | **Done** | `MemoryClient.profile`, `profile_activity.py` |
| Framework integrations | **Done**: LangChain/LangGraph, AutoGen/AG2, CrewAI, LlamaIndex, Google ADK, Strands, OpenAI Agents, Pydantic AI; Vercel AI and Mastra in TS | `frameworks.py`, `integrations/`, `sdk/typescript` |
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
| Four-branch exception hierarchy with remediation | **Done** | `exceptions.py`, `remediation.py` |
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
