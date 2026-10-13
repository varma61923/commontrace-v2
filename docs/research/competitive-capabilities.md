# Competitive capability evidence

Review date: 2026-10-13. This is a bounded source review of the eight competitor
checkouts supplied in the workspace, and CommonTrace's current implementation.
Graphify is an exploration tool in this workspace and is excluded from the eight
products. No competitor service was provisioned and no comparative accuracy,
latency, cost, reliability, or security benchmark was run for this document.
Feature names do not establish behavioral parity or superiority.

## Evidence boundaries and revisions

**Present** means an implementation was inspected; it does not mean its behavior
was independently validated in production. **Partial** means the inspected
implementation covers a narrower use case than the category suggests.
**Documented** means the checkout describes a capability whose service behavior
was not inspected. **Not verified** means this review has insufficient evidence;
it must not be read as an assertion that the product lacks the capability.
Tests cited below are evidence of intended contracts, not a claim that this
review executed them.

All eight competitor checkouts had clean tracked working trees when inspected.
The full Git revisions below pin the source evidence. Links into sibling
checkouts are local workspace references and require those checkouts to exist.
CommonTrace changes in this iteration are an uncommitted overlay on the stated
baseline; the final resulting commit must be used when reproducing the changes.

| Checkout | Pinned revision | Source availability |
| --- | --- | --- |
| EverOS | `a345d08e5cdfc8db77504da89fffaf3aee43018b` | Runtime and tests |
| Cognee | `0ec7a9fa61c9ff04bf7e02e0d57af363a993a5e1` | Runtime and tests |
| Graphiti | `2f0fb35b749f1ff17377892e099882176e453393` | Graph library, server, MCP, tests |
| Hindsight | `f843f173bb41f001e322cc83c917371639e47fbf` | API runtime, extensions, clients, tests |
| Letta | `5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a` | Migration README; active runtime is elsewhere |
| Mem0 | `d26ba5e4518cef7fff3c66d4f715b21adf9feabe` | OSS runtime, SDKs, integrations; hosted platform docs also included |
| Supermemory | `87af03a55f6a211683a54e083f1708e1619aa06d` | MCP, integrations, visualization, hosted API docs; reviewed paths do not establish hosted engine internals |
| Zep | `cfa2ab2bbc3c7085d636479b719bcf64608162cc` | Current examples, ingestion and integrations; legacy server is deprecated |
| CommonTrace baseline | `a54c42dd25488832aee225a9ced865e7ef967e4f` | Runtime, protocol, SDKs, tests, plus this iteration's working changes |

## Competitor evidence

| Product | Concrete inspected evidence | Scope of conclusion |
| --- | --- | --- |
| EverOS | [Scheduled reflection strategy](../../../EverOS/src/everos/memory/strategies/reflect_episodes.py), [reflection behavior and archival](../../../EverOS/docs/reflection.md), [multimodal normalization](../../../EverOS/src/everos/memory/extract/ingest/multimodal.py), [parser prerequisites and formats](../../../EverOS/docs/multimodal.md), [LongMemEval adapter](../../../EverOS/benchmarks/adapters/longmemeval.py) | **Present:** cron strategy delegates per-owner reflection; multimodal items become text with provenance tags and missing-parser counts. **Documented:** merging and archiving episodes, image/PDF/audio/office parsing with optional providers and conversion tools. **Not verified:** API authorization enforcement, connector completeness, model quality and benchmark comparability. Owner fields alone do not establish access control. |
| Cognee | [Temporal event/interval models](../../../cognee/cognee/tasks/temporal_graph/models.py), [entity consolidation pipeline](../../../cognee/cognee/memify_pipelines/consolidate_entities.py), [image loader](../../../cognee/cognee/infrastructure/loaders/core/image_loader.py), [tenant access tests](../../../cognee/cognee/tests/api/test_multi_tenant_access_control_e2e.py), [provider connectors](../../../cognee/cognee/tasks/ingestion/connectors/), [evaluation framework](../../../cognee/cognee/eval_framework/README.md) | **Present:** temporal data models; authorized-dataset consolidation with dry-run and graph mutation; vision extraction with optional OCR, metadata and perceptual deduplication. **Present as source/test surfaces:** tenant controls, ingestion connectors and evaluations. **Not verified:** their full deployment configuration, connector synchronization guarantees or measured answer quality. |
| Graphiti | [EntityEdge temporal fields](../../../graphiti/graphiti_core/edges.py), [temporal normalization](../../../graphiti/graphiti_core/utils/maintenance/temporal_edge_utils.py), [group-aware ingestion/search](../../../graphiti/graphiti_core/graphiti.py), [episode types](../../../graphiti/graphiti_core/nodes.py), [MCP server](../../../graphiti/mcp_server/README.md), [OpenTelemetry example](../../../graphiti/examples/opentelemetry/README.md), [evaluation entry point](../../../graphiti/tests/evals/eval_cli.py) | **Present:** graph partitions, validity/invalidation/expiry fields, temporal normalization, hybrid search/reranking orchestration. **Partial:** reviewed episode types are message, text, JSON and fact triples; native media ingestion was not established here. **Not verified:** group identifiers as a complete authentication boundary, connector coverage or equivalent reflection behavior. |
| Hindsight | [Temporal extraction](../../../hindsight/hindsight-api-slim/hindsight_api/engine/search/temporal_extraction.py), [reflect agent](../../../hindsight/hindsight-api-slim/hindsight_api/engine/reflect/agent.py), [consolidator](../../../hindsight/hindsight-api-slim/hindsight_api/engine/consolidation/consolidator.py), [static-key tenant extension](../../../hindsight/hindsight-extensions/static-keys-tenant/hindsight_ext_static_keys_tenant/extension.py), [multimodal extraction tests](../../../hindsight/hindsight-api-slim/tests/test_multimodal_extraction_llm.py), [language clients](../../../hindsight/hindsight-clients/), [metrics](../../../hindsight/hindsight-api-slim/hindsight_api/metrics.py) | **Present:** temporal query extraction and a bank-specific agentic reflection implementation with tool budgets and structured output; consolidation, tenant-extension and metrics source. **Present as test surfaces:** multimodal extraction and scoped access. **Not verified:** configured tenant policy, provider/model behavior, connector breadth or comparative retrieval quality. |
| Letta | [Checkout README and source migration notice](../../../letta/README.md) | **Documented:** stateful agents, App Server, channels and Agent SDK. **Not verified:** all seven capability categories in the current runtime, because this checkout redirects to `letta-ai/letta-code`. Historical caches or an archive branch are not evidence for the active product. This checkout cannot support a feature-complete comparison. |
| Mem0 | [OSS memory implementation](../../../mem0/mem0/memory/main.py), [session-scope tests](../../../mem0/tests/memory/test_session_scope.py), [temporal parameter rejection tests](../../../mem0/tests/memory/test_temporal_feature_notice.py), [OSS multimodal guide](../../../mem0/docs/open-source/features/multimodal-support.mdx), [TypeScript OSS package](../../../mem0/mem0-ts/src/oss/README.md), [integration packages](../../../mem0/integrations/), [telemetry](../../../mem0/mem0/memory/telemetry.py) | **Present:** user/agent/run scope filters, memory update operations and vision-message handling in OSS. **Partial:** OSS temporal support must not be inferred from hosted docs: inspected tests explicitly require rejection of `timestamp` and `reference_date` arguments. **Present as source surfaces:** SDKs, agent integrations and telemetry. **Not verified:** hosted temporal behavior, equivalent consolidation/reflection, production authorization or comparative quality. |
| Supermemory | [Namespace-scoping MCP tests](../../../supermemory/apps/mcp/e2e/space-scope.test.ts), [MCP tools](../../../supermemory/apps/mcp/src/server/tools/), [hosted connectors API](../../../supermemory/apps/docs/v5/api-reference/connectors.mdx), [memory/history/deletion API](../../../supermemory/apps/docs/v5/api-reference/content-management.mdx), [SDK guide](../../../supermemory/skills/supermemory/references/sdk-guide.md), [memory graph package](../../../supermemory/packages/memory-graph/README.md) | **Present:** MCP/tool and graph visualization code; namespace isolation has test coverage in the checkout. **Documented:** hosted connector OAuth, sync lifecycle, namespaces, memory history and semantic forgetting. **Not verified:** hosted extraction, consolidation, temporal semantics, multimodal implementation, infrastructure observability or service performance. A connector guide does not prove operational connector parity. |
| Zep | [Repository scope notice](../../../zep/README.md), [ingestion pipeline contracts](../../../zep/ingestion/README.md), [framework integrations](../../../zep/integrations/README.md), [evaluation harness](../../../zep/zep-eval-harness/README.md) | **Present as source surfaces:** bulk ingestion, framework adapters and evaluation tooling. **Documented:** timestamps, graph ownership, ontology setup, chunking, batch fallback and resumable ingestion contracts. **Not verified:** current hosted memory engine, auth enforcement, reflection and multimodal internals. The repository explicitly excludes the current product/service; deprecated `legacy/` behavior is not current Zep behavior. Graphiti is reviewed independently above. |

## CommonTrace by capability category

| Category | Current local evidence | Status and practical limit |
| --- | --- | --- |
| Temporal memory | [Atomic facts and historical filters](../../commontrace/hierarchical.py), [temporal intent parser](../../commontrace/temporal_intent.py), [fact contract tests](../../tests/test_hierarchical_facts.py) | **Present:** validity windows, expiry, supersession, transaction-time retraction and `known_at`/`as_of` retrieval surfaces. **Partial:** natural-language intent parsing is a bounded heuristic; an implementation does not establish correct temporal reasoning on ambiguous dates, corrections or multihop questions. |
| Scopes and authentication | [Scope matching and memory controls](../../commontrace/memory_control.py), [JWT/OAuth validation](../../commontrace/oauth.py), [gateway](../../commontrace/gateway.py), [OAuth tests](../../tests/test_oauth.py), [connector acknowledgment/isolation tests](../../tests/test_connector_acknowledgments.py) | **Present:** scoped records and authenticated gateway machinery; this iteration isolates connector state and source retirement by scope. **Not verified:** comprehensive endpoint-by-endpoint authorization under every deployment configuration or tenant-scale isolation under adversarial concurrency. |
| Consolidation and reflection | [Fact clustering and proposals](../../commontrace/fact_consolidation.py), [lesson consolidation report](../../commontrace/consolidate.py), [reflection and offline work](../../commontrace/memory_control.py), [fact consolidation tests](../../tests/test_fact_consolidation.py) | **Present:** lexical/optional embedding clustering; review-status proposals; curated/consolidated/raw reflection with bounded evidence selection; background maintenance surfaces. **Partial:** fact proposals deliberately do not automatically supersede originals. Budgeted evidence assembly is not equivalent to Hindsight's tool-using reflect agent or EverOS's scheduled narrative replacement. |
| Multimodal memory | [Image ingestion](../../commontrace/vision.py), [ingestion orchestration](../../commontrace/ingest/pipeline.py), [vision tests](../../tests/test_vision_mcp.py) | **Present:** opt-in image description with configured provider and size bound; text ingestion can index the result. **Partial:** describing images is narrower than a verified audio/video/office parsing system or native joint image/text retrieval. This iteration routes image providers through existing resilience controls and keeps provider credentials distinct in circuit selection. |
| Connectors | [Local directory](../../commontrace/connectors/local_dir.py), [web crawler](../../commontrace/connectors/web_crawler.py), [pinned provider readers](../../commontrace/connectors/knowledge.py), [stream and webhook support](../../commontrace/connectors/knowledge_streams.py), [connector tests](../../tests/test_connectors.py) | **Present:** local and seed-web ingestion, provider readers, selected stream/webhook paths and resumable cursors. **Partial:** operator-supplied OAuth tokens and provider-specific resource fetches do not establish hosted OAuth onboarding, complete bidirectional sync, all deletions or provider-wide backfills. Seed-page fetching is narrower than recursive site crawling. |
| Integrations and SDKs | [Python client](../../commontrace/client.py), [SDK inventory and contracts](../../sdk/README.md), [framework adapters](../../commontrace/integrations/), [framework SDK tests](../../tests/test_new_framework_sdks.py), [optional live contracts](../../tests/test_optional_sdk_live_contracts.py) | **Present:** Python, TypeScript, Go, Rust and JVM client source plus selected agent-framework adapters. **Not verified:** every published package/version, every optional framework's live behavior, or behavioral equivalence across languages. Importability and mocked contracts are narrower than release compatibility. |
| Evaluation and observability | [Telemetry and Prometheus/OTel surfaces](../../commontrace/telemetry.py), [evaluation tooling](../../benchmarks/), [performance evidence](../performance.md), [operations guidance](../operations.md), [storage workload](../../benchmarks/next_generation_storage.py) | **Present:** redacted request context, spans, metrics and workload/evaluation tooling. **Partial:** existing measurements have explicit dataset, budget and implementation boundaries. They do not establish a universal 10× gain, competitor quality superiority, hosted availability or lower model cost. |

## Changes in this iteration

These changes improve concrete contracts; they do not close every gap above.
The [operations guide](../operations.md) and [performance evidence](../performance.md)
provide the existing deployment and measurement context.

- **Crawler network boundaries:** [implementation](../../commontrace/connectors/web_crawler.py)
  and [regressions](../../tests/test_web_crawler_security.py) enforce offline policy
  on redirects, reject cross-origin redirects into non-public networks, strip
  cross-origin credential headers, validate HTTP(S) URLs and reject oversized
  responses. Explicit localhost/intranet seeds remain supported. DNS rebinding
  and initial private seeds require deployment egress controls; this is not a
  complete untrusted-URL sandbox.
- **Retry and scope isolation:** [connector base](../../commontrace/connectors/base.py),
  [local connector](../../commontrace/connectors/local_dir.py), [crawler](../../commontrace/connectors/web_crawler.py)
  and [acknowledgment tests](../../tests/test_connector_acknowledgments.py) retain
  unfinished work after persistence failures and bind source/cursor identity to
  scope. A successful fetch alone no longer advances the crawler checkpoint.
- **Provider resilience:** [circuit breaker](../../commontrace/circuit_breaker.py),
  [LLM requests](../../commontrace/llm.py), [vision requests](../../commontrace/vision.py)
  and [resilience tests](../../tests/test_circuit_breaker.py) tighten transient
  failure handling and provider circuit identity. See [runtime upgrade context](../runtime-upgrade.md).

## Bounded remaining work

1. **Make comparisons executable before declaring a winner.** Pin candidate
   revisions, use identical source datasets, gold labels, context budgets and
   provider settings, and record answer correctness, abstention, p50/p95 latency
   and cost. Publish failures and uncertainty alongside successful workloads;
   extend the [existing performance methodology](../performance.md).
2. **Validate connector lifecycles per provider.** Prioritize one complete
   provider flow: initial import, pagination, edits, deletions, revoked credentials,
   rate limits, failed writes, and restart recovery. Establish those acceptance
   contracts before treating a provider constant as a complete integration.
3. **Close media gaps with explicit contracts.** Select an audio or document
   format supported by the intended users, retain source provenance, bound bytes
   and conversion time, and verify offline/error behavior. Benchmark extraction
   fidelity and retrieval rather than counting file extensions.
4. **Exercise tenant boundaries across public entry points.** Build an endpoint
   matrix covering reads, mutations, history, connector tokens, retirement and
   background jobs with two conflicting principals and scopes. Keep explicit
   local deployment behavior separate from authenticated shared deployments.
5. **Measure consolidation's effect on evidence.** Compare review proposals,
   accepted consolidations and original facts on contradiction retention, source
   attribution and recall. Scheduled destructive/archival merging should require
   measured benefit and a reproducible recovery path.
6. **Complete unavailable-source reviews separately.** Obtain the active Letta
   runtime and approved hosted evaluation access for Zep/Supermemory/Mem0 platform
   before asserting their internals or complete capability coverage. The current
   local checkouts support only the narrower conclusions recorded above.
