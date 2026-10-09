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
| 0.1 Scoreboard with dense + cross-encoder on | **Done (this pass)** for LoCoMo (dense, dense + cross-encoder, dense + adaptive); BEAM/LongMemEval dense pending, see below | `benchmarks/conversation_bench.py --embedder arctic-m` |
| 0.2 Model-backed answer accuracy | **Partial**: harness, judges and cost guards exist; not run (no model credentials in this environment) | `--answer`, `benchmarks/judges/` |
| 0.3 Competitors through the same harness | **Partial**: local raw-source mem0 and Graphiti profiles; managed services not reproduced | `benchmarks/vendor_adapters.py` |
| 0.4 Agentic benchmarks (MemoryArena, AMA-Bench, MemGym, Evo-Memory) | **Not done**: they need their own agent environments; DolphinBench harness present | `benchmarks/dolphinbench/` |
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
| WS4 Red-team suite, multi-principal (GateMem-style) benchmark | **Partial**: regression tests for laundering and injection; no GateMem/PiSAs dataset run | `tests/test_learning_assurance.py` |
| WS5 Federated commons, randomized response, replicated lift | **Done** (experimental privacy, stated as such) | `federation.py` |
| WS5 Marketplace with revenue share | **Not done**: needs a hosted service and commercial terms | - |
| WS6 Foresight and sleep-time refresh | **Done** | `memory_control.offline_pass`, `dream` |
| WS7 PyPI and npm publishing | **Done (this pass)** (workflow; first publish needs registry configuration) | `.github/workflows/release.yml` |
| WS7 Agent self-signup, LLM wrappers, hooks, frameworks, connectors, generated SDKs | **Done** | `onboarding.py`, `completion_wrappers.py`, `frameworks.py`, `connectors/`, `sdk/` |
| WS7 Remote MCP with OAuth 2.1 | **Not done**: bearer tokens only; see `hub/README.md` "OAuth/JWT" | - |
| WS8 Console, Memory Palace, Needs Attention | **Done** | `ui/`, `/v1/palace` |
| WS9 CausalMemBench | **Done (this pass)** | `benchmarks/causalmembench.py`, `docs/benchmarks/causalmembench.md` |
| WS10 Embodied fleets (sim/real separation, protected memories) | **Done**; multimodal episodes via ingestion | `fleet.py`, `gateway.py`, `ingest/multimodal.py` |

## Competitive intelligence report

### Feature and architectural gaps

| Report item | Status | Where |
| --- | --- | --- |
| MCP server | **Done** (local stdio/HTTP/SSE and Hub) | `mcp_server.py`, `hub/server.py` |
| CLI with agent signup | **Done**; `init --agent-caller NAME` added **(this pass)** | `commands/init_cmd.py` |
| Dashboard | **Done** (no-build console, not Next.js, by design) | `commontrace/ui/` |
| User profiles (static + dynamic) | **Done** | `MemoryClient.profile`, `profile_activity.py` |
| Framework integrations | **Done**: LangChain/LangGraph, AutoGen/AG2, CrewAI, LlamaIndex, Google ADK, Strands, OpenAI Agents, Pydantic AI; Vercel AI and Mastra in TS | `frameworks.py`, `integrations/`, `sdk/typescript` |
| Observations and reflection | **Done** | `observations.py`, `memory_control.py` |
| Knowledge wiki | **Done** | `wiki.py`, `knowledge_pages.py` |
| Data connectors | **Done**: GitHub, Slack, Drive, Gmail, Notion, OneDrive, Confluence, Jira, Linear, Zendesk, ServiceNow, Salesforce, Intercom, Greenhouse | `connectors/knowledge.py` |
| Multi-signal retrieval | **Done** | `search_recipes.py`, conversation hybrid recall |
| Bi-temporal fact invalidation | **Done** | `hierarchical.py`, `graph.py` |
| Pipeline recovery | **Done** | `ingest/pipeline.py`, `jobs.py` |
| Provider pattern | **Done**; reranker registry added **(this pass)** | `providers.py` |
| Multi-tenancy isolation | **Done** (Hub RLS; per-owner/dataset local vector stores) | `hub/`, `providers.BackendFactory` |
| Markdown-first with file watcher | **Done**; `lesson edit` added **(this pass)** | `watch.py`, `commands/lesson_cmd.py` |
| Code graph | **Done** | `code_graph.py` |
| Local/offline mode | **Done (this pass)**: `--offline` / `COMMONTRACE_OFFLINE` | `offline.py` |
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
| Tiered tests, 80% coverage gate, Makefile | **Done** | `pyproject.toml`, `Makefile`, CI |
| `_FILE` secrets | **Done**; Hub client API key now honours it **(this pass)** | `secrets_provider.py` |
| Rate-limit auto-detection | **Done** | `overload.py` |
| CI path filtering | **Partial**: security workflow is path-filtered; main CI runs everything on purpose so generated-doc and benchmark gates cannot be skipped | - |

### Benchmark recommendations

| Recommendation | Status and measured effect |
| --- | --- |
| P1 Semantic embeddings | Already supported; **measured for the first time (this pass)**: see the dense rows below |
| P2 Entity linking | **Done** (entity boost in conversation recall; mem0-style entity signal in `search_recipes`) |
| P3 Adaptive token budget | **Done (this pass)**: BEAM event ordering 59.6% → 96.9%, summarization 28.5% → 50.5% evidence at a 1,500 floor |
| P4 Cross-encoder reranking | Already supported; **measured (this pass)**: LoCoMo 82.00% -> 83.90% evidence at 1,500 tokens over dense alone |
| Preference following | **Tried and rejected (this pass)**: weighting the user's own turns on advice questions lowered LongMemEval preference evidence from 61.7% to 52.8%, so it was removed. The misses are vocabulary mismatches (a "battery life" question answered by a "power bank" statement); dense retrieval is the measured lever |
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
| Dense (arctic-m), fixed | 82.00% / 91.36% (1,452 / 3,887 tok) | running | 71.21% / 82.99% (1,469 / 3,939 tok) |
| Dense + cross-encoder, fixed | 83.90% / 91.51% (1,452 / 3,887 tok) | not run | not run |
| Dense, adaptive budget | 83.73% / 92.53% (1,711 / 4,559 tok) | not run | 80.15% / 86.10% (2,819 / 7,053 tok) |

LoCoMo multi-hop evidence, the report's largest gap: 52.08% / 66.61% lexical,
60.73% / 79.47% dense, 66.66% / 83.99% dense with the adaptive budget. Turn
Recall@5 rises from 54.78% (lexical) to 60.96% (dense) and 65.46% (dense +
cross-encoder). The cross-encoder's p50 recall latency on this shared 4-core CPU
was about 480 ms against about 55 ms dense-only; choose it where accuracy matters
more than latency.

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
