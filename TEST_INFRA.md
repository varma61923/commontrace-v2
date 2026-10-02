# CommonTrace v2 Test Infrastructure & E2E Testing Specification (`TEST_INFRA.md`)

## 1. Test Philosophy: Opaque-Box, Requirement-Driven

The CommonTrace v2 End-to-End (E2E) testing framework strictly follows an **opaque-box, requirement-driven** methodology:

1. **Client / Runtime Perspective**: Tests treat CommonTrace as an external black box accessed strictly through public interfaces:
   - CLI commands (`commontrace block`, `fact`, `graph`, `query`, `capture`, `dream`, `ingest`, `doctor`)
   - Protocol MCP server tools (`read`, `update`, `list`, `delete`, `record_fact`, `query_facts`, `graph_query`, `graph_neighbors`)
   - Standard file store artifacts on disk (`memory/blocks/`, `memory/facts/`, `memory/graph/`, `memory/traces/`, `memory/profile.md`)
   - Enterprise Hub REST APIs (`/api/v1/traces`, `/api/v1/traces/search`) and database schema/indexes.
2. **Authoritative Specification Sources**: Every test assertion is derived directly from requirements in `ORIGINAL_REQUEST.md` and feature contracts in `PROJECT.md § Feature Inventory` and `§ Interface Contracts`.
3. **No Facade or Tautological Tests**: Tests verify actual observable side effects: file updates, revision hash generation, line counts, error exit codes, bitemporal filtering accuracy, and precision@1 guarantees.
4. **Progressive Testability & Isolation**:
   - Each test operates within an isolated sandbox directory (`tmp_path`) with its own instantiated CommonTrace store.
   - Tests do not leak state, depend on execution ordering, or assume pre-existing database rows.
   - For features tied to sequential milestones (e.g. M1 -> M2 -> M3 -> M4), tests dynamically adapt or skip with structured progress indicators if prerequisite milestone components are not yet compiled, ensuring that the test runner provides clean, actionable diagnostic feedback during iterative development.

---

## 2. Feature Inventory Mapping

| # | Feature Name | Core Area | Milestone | Specification Contract | E2E Tier | Test Module |
|---|--------------|-----------|-----------|------------------------|----------|-------------|
| 1 | Working Memory Block CRUD & Quotas | Memory Blocks | M1 | Bounded named blocks with strict character quota ceilings | Tier 1, 2 | `tier1_features/test_tier1_memory_blocks.py`, `tier2_boundaries/test_tier2_block_boundaries.py` |
| 2 | Atomic Substring Replacement | Memory Blocks | M1 | Safe, unambiguous replacement of unique strings within blocks | Tier 1, 2 | `tier1_features/test_tier1_memory_blocks.py`, `tier2_boundaries/test_tier2_block_boundaries.py` |
| 3 | SHA-256 Revision History | Memory Blocks | M1 | Audit logging in `memory/blocks/history.jsonl` with hash chaining | Tier 1 | `tier1_features/test_tier1_memory_blocks.py` |
| 4 | Atomic File Writes | Memory Blocks | M1 | Temporary file write + `os.replace` to prevent corrupted partial writes | Tier 1 | `tier1_features/test_tier1_memory_blocks.py` |
| 5 | Memory Block MCP Tools | Memory Blocks | M1 | Stdio MCP tools (`read`, `update`, `list`, `delete`) | Tier 1 | `tier1_features/test_tier1_memory_blocks.py` |
| 6 | Bitemporal Atomic Fact Lifecycle | Atomic Facts | M1 | Propositions with `ADD`, `NOOP`, `UPDATE`, `SUPERSEDE`, `DELETE` | Tier 1, 3 | `tier1_features/test_tier1_atomic_facts.py`, `tier3_combinations/test_tier3_bitemporal_supersession_pipeline.py` |
| 7 | Bitemporal `as_of` Querying | Atomic Facts | M1 | Accurate historical snapshot filtering (`valid_from <= as_of < valid_until`) | Tier 1, 2, 3 | `tier1_features/test_tier1_atomic_facts.py`, `tier2_boundaries/test_tier2_fact_boundaries.py` |
| 8 | Atomic Facts MCP Tools | Atomic Facts | M1 | Stdio MCP tools (`record_fact`, `query_facts`) | Tier 1 | `tier1_features/test_tier1_atomic_facts.py` |
| 9 | Temporal Knowledge Graph | Knowledge Graph | M1 | Directed entity-relationship graph with multi-hop BFS traversal | Tier 1, 2 | `tier1_features/test_tier1_knowledge_graph.py`, `tier2_boundaries/test_tier2_graph_boundaries.py` |
| 10 | Distance-Decayed Graph Boosting | Retrieval | M1 | Proximity boosting for candidate lessons ($0.250 / (1 + \text{hop})$) | Tier 1, 3 | `tier1_features/test_tier1_retrieval_boost.py`, `tier3_combinations/test_tier3_graph_augmented_lesson_lifecycle.py` |
| 11 | CLI Query Graph Boost Integration | Retrieval | M1 | Wire `graph_boost_for_lessons` and `--graph-weight` into `query` CLI | Tier 1, 3 | `tier1_features/test_tier1_retrieval_boost.py` |
| 12 | Knowledge Graph MCP Tools | Knowledge Graph | M1 | Stdio tools (`graph_query`, `graph_neighbors`, `commontrace://graph`) | Tier 1 | `tier1_features/test_tier1_knowledge_graph.py` |
| 13 | Formal Cognitive Memory Schemas | Schemas | M1 | JSON schemas for memory blocks, atomic facts, and graph models | Tier 1 | `tier1_features/test_tier1_memory_blocks.py`, `test_tier1_atomic_facts.py` |
| 14 | Retrieval Benchmark Verification | Retrieval | M1 | 100% Precision@1 and worst-field pollution <= 1.06x | Tier 1 | `tier1_features/test_tier1_retrieval_boost.py` |
| 15 | Code Repository Ingestion | Ingestion | M2 | AST parsing, symbol extraction to graph, architectural docstrings | Tier 1, 3 | `tier1_features/test_tier1_ingestion_connectors.py`, `tier3_combinations/test_tier3_ingest_to_facts_and_graph.py` |
| 16 | Markdown Documentation Connector | Ingestion | M2 | Hierarchical heading chunking, guideline extraction to atomic facts | Tier 1, 3 | `tier1_features/test_tier1_ingestion_connectors.py`, `tier3_combinations/test_tier3_ingest_to_facts_and_graph.py` |
| 17 | JSON Structured Log Connector | Ingestion | M2 | Error clustering, fingerprinting, service/error nodes & edges | Tier 1, 3 | `tier1_features/test_tier1_ingestion_connectors.py`, `tier3_combinations/test_tier3_graph_augmented_lesson_lifecycle.py` |
| 18 | Failure Transcript Connector | Ingestion | M2 | Turn parsing, failure isolation, candidate lesson drafting | Tier 1, 4 | `tier1_features/test_tier1_ingestion_connectors.py`, `tier4_real_world/test_tier4_fleet_incident_postmortem.py` |
| 19 | Ingestion Pipeline Orchestrator | Ingestion | M2 | Secret redaction, routing to facts, graph, and lessons | Tier 1, 2 | `tier1_features/test_tier1_ingestion_connectors.py`, `tier2_boundaries/test_tier2_ingest_boundaries.py` |
| 20 | Ingestion CLI Command | Ingestion | M2 | `commontrace ingest` unified command with format & filter flags | Tier 1 | `tier1_features/test_tier1_ingestion_connectors.py` |
| 21 | Autonomous Agent Execution Loop | Agent Loop | M3 | Multi-turn Letta-pattern agent loop (`commontrace/agent_loop.py`) | Tier 1, 4 | `tier1_features/test_tier1_agent_loop_dreaming.py`, `tier4_real_world/test_tier4_autonomous_coding_agent_lifecycle.py` |
| 22 | Context Dynamic Assembly | Agent Loop | M3 | Re-assembling prompt context with active blocks & profile | Tier 1, 3 | `tier1_features/test_tier1_agent_loop_dreaming.py`, `tier3_combinations/test_tier3_agent_memory_to_dream.py` |
| 23 | Execution Trace Logging | Agent Loop | M3 | Persisting execution outcome and occasion lineage to `memory/traces/` | Tier 1, 3 | `tier1_features/test_tier1_agent_loop_dreaming.py` |
| 24 | Dreaming Trace Graph Mining | Dreaming | M3 | Scanning `.md` (and `.json`) traces to mine graph relationships | Tier 1, 3 | `tier1_features/test_tier1_agent_loop_dreaming.py`, `tier3_combinations/test_tier3_agent_memory_to_dream.py` |
| 25 | Dreaming Signal Consolidation | Dreaming | M3 | Fleet failure distillation, lesson conflict & staleness resolution | Tier 1 | `tier1_features/test_tier1_agent_loop_dreaming.py` |
| 26 | Active Space Profile Synthesis | Dreaming | M3 | Consolidation of active blocks, facts, and health in `memory/profile.md` | Tier 1, 3 | `tier1_features/test_tier1_agent_loop_dreaming.py`, `tier3_combinations/test_tier3_agent_memory_to_dream.py` |
| 27 | Agent CLI Command | Agent Loop | M3 | `commontrace agent run` entrypoint for multi-turn task execution | Tier 1 | `tier1_features/test_tier1_agent_loop_dreaming.py` |
| 28 | Hub PostgreSQL GIN Indexing | Hub Parity | M4 | Inverted indexing on `traces.scopes` for $O(\log N)$ scoped queries | Tier 1, 4 | `tier1_features/test_tier1_hub_parity.py`, `tier4_real_world/test_tier4_multi_tenant_scoped_governance.py` |
| 29 | Alembic Migration for Scopes & Bounds | Hub Parity | M4 | Migration adding `scopes`, `valid_from`, `valid_until`, GIN index | Tier 1 | `tier1_features/test_tier1_hub_parity.py` |
| 30 | Hub Bitemporal & Scoped Queries | Hub Parity | M4 | Querying traces with bitemporal `as_of` and scope containment | Tier 1, 3 | `tier1_features/test_tier1_hub_parity.py`, `tier3_combinations/test_tier3_hub_sync_local_parity.py` |
| 31 | Hub Trace Amendment Preservation | Hub Parity | M4 | Carrying forward `scopes`, `valid_from`, `valid_until` on amendment | Tier 1, 2 | `tier1_features/test_tier1_hub_parity.py`, `tier2_boundaries/test_tier2_hub_boundaries.py` |
| 32 | Hub REST API Scoped Endpoints | Hub Parity | M4 | `/api/v1/traces` and `/api/v1/traces/search` scope & bitemporal support | Tier 1 | `tier1_features/test_tier1_hub_parity.py` |
| 33 | Hub MCP Server Parameter Parity | Hub Parity | M4 | Exposing `scope`/`scopes`, `as_of`, `valid_from`, `valid_until` on MCP | Tier 1 | `tier1_features/test_tier1_hub_parity.py` |
| 34 | Protocol & Client Trace Schema Parity | Protocol | M4 | Updating `trace.schema.json` with scopes, valid_from, valid_until | Tier 1 | `tier1_features/test_tier1_hub_parity.py` |
| 35 | CLI Capture Scope & Temporal Flags | CLI | M4 | Adding `--scope`/`--scopes`, `--valid-from`, `--valid-until` to `capture` | Tier 1, 3 | `tier1_features/test_tier1_hub_parity.py`, `tier3_combinations/test_tier3_hub_sync_local_parity.py` |
| 36 | Client Hub Sync Scope Forwarding | Client Sync | M4 | Propagating scopes and validity windows in `hub_client.py` | Tier 1, 3 | `tier1_features/test_tier1_hub_parity.py`, `tier3_combinations/test_tier3_hub_sync_local_parity.py` |
| 37 | System Diagnostics & Linter Quality | Diagnostics | M4 | `commontrace doctor` cognitive checks and `ruff check` verification | Tier 1 | `tier1_features/test_tier1_hub_parity.py` |

---

## 3. Test Architecture & Runner Instructions

### 3.1 Directory Layout
```
e2e_tests/
├── __init__.py
├── conftest.py                     # Global pytest fixtures, sandbox management, DB helpers
├── harness/
│   ├── __init__.py
│   ├── cli_runner.py               # Robust subprocess CLI execution wrapper
│   ├── store_fixtures.py           # Synthetic store generation & sample generators
│   └── hub_runner.py               # Hub API & direct session helpers
├── tier1_features/                 # Core functional coverage (>= 5 cases per core area)
│   ├── test_tier1_memory_blocks.py
│   ├── test_tier1_atomic_facts.py
│   ├── test_tier1_knowledge_graph.py
│   ├── test_tier1_retrieval_boost.py
│   ├── test_tier1_ingestion_connectors.py
│   ├── test_tier1_agent_loop_dreaming.py
│   └── test_tier1_hub_parity.py
├── tier2_boundaries/               # Boundary conditions, quotas, corrupt data, error exits
│   ├── test_tier2_block_boundaries.py
│   ├── test_tier2_fact_boundaries.py
│   ├── test_tier2_graph_boundaries.py
│   ├── test_tier2_ingest_boundaries.py
│   └── test_tier2_hub_boundaries.py
├── tier3_combinations/             # Cross-feature pipelines & multi-system integration
│   ├── test_tier3_ingest_to_facts_and_graph.py
│   ├── test_tier3_agent_memory_to_dream.py
│   ├── test_tier3_graph_augmented_lesson_lifecycle.py
│   ├── test_tier3_bitemporal_supersession_pipeline.py
│   └── test_tier3_hub_sync_local_parity.py
├── tier4_real_world/               # Complex end-to-end fleet and agent scenarios
│   ├── test_tier4_fleet_incident_postmortem.py
│   ├── test_tier4_autonomous_coding_agent_lifecycle.py
│   └── test_tier4_multi_tenant_scoped_governance.py
└── run_e2e.py                      # Standalone test runner with rich terminal reporting
```

### 3.2 Running the Tests

#### Running via Standard Pytest
```bash
# Run the complete E2E test suite
PYTHONPATH=. pytest -v e2e_tests/

# Run a specific tier
PYTHONPATH=. pytest -v e2e_tests/tier1_features/
PYTHONPATH=. pytest -v e2e_tests/tier2_boundaries/
PYTHONPATH=. pytest -v e2e_tests/tier3_combinations/
PYTHONPATH=. pytest -v e2e_tests/tier4_real_world/

# Run with keyword filter
PYTHONPATH=. pytest -v -k "bitemporal or quota" e2e_tests/
```

#### Running via Master E2E Runner
```bash
# Run all tiers with structured breakdown
PYTHONPATH=. python3 e2e_tests/run_e2e.py

# Run only specific tiers
PYTHONPATH=. python3 e2e_tests/run_e2e.py --tier 1
PYTHONPATH=. python3 e2e_tests/run_e2e.py --tier 1,2

# Output machine-readable JSON summary
PYTHONPATH=. python3 e2e_tests/run_e2e.py --json
```

---

## 4. 4-Tier Test Methodology

### Tier 1: Feature Coverage (Core Areas, >= 5 cases each)
- Validates the primary happy path and direct functional contracts across all 7 core subsystem areas:
  1. **Working Memory Blocks**: Block creation, retrieval, appending, atomic replacement, deletion, and SHA-256 audit revision tracking.
  2. **Hierarchical Atomic Facts**: Proposition addition, reinforcement counter bumping (`NOOP`), point-in-time supersession (`SUPERSEDE`), soft deletion, and bitemporal `as_of` querying.
  3. **Temporal Knowledge Graph**: Entity node registration, typed relationship edges, multi-hop BFS traversal, entity name/ID extraction from task text, and Mermaid visualization.
  4. **Graph-Boosted Retrieval**: Querying lessons with distance-decayed boost, `--graph-weight` scaling, and cross-field precision@1 benchmark gating.
  5. **Multimodal Ingestion Connectors**: Parsing code AST, markdown documentation hierarchies, structured JSON log clusters, and conversational failure transcripts into facts, graph nodes, and lesson drafts.
  6. **Autonomous Agent Loop & Dreaming**: Multi-turn agent execution, dynamic working memory updates, episodic trace generation, dreaming trace mining (`.md`), and active space profile synthesis (`memory/profile.md`).
  7. **Enterprise Hub Parity & Protocol**: Scoped trace contribution, GIN containment search, bitemporal `as_of` query filtering, and amendment metadata preservation.

### Tier 2: Boundary & Corner Conditions
- Rigorously stresses limits and tests graceful error handling:
  - **Memory Quotas**: Hard block quota rejections (`QuotaExceededError`), zero-length name rejection, and path traversal protection (`../../etc/passwd`).
  - **Ambiguity Checks**: Ambiguous substring replacement rejection (target appears multiple times) and non-existent substring error.
  - **Temporal & Confidence Boundaries**: Confidence score clamping `[0.0, 1.0]`, inverted bitemporal ranges (`valid_from > valid_until`), extreme timestamps (e.g. `1970-01-01T00:00:00Z` and `2099-12-31T23:59:59Z`).
  - **Graph Resilience**: Circular dependencies, disconnected graphs, excessive hop queries, and unknown relation types.
  - **Ingestion & Secret Redaction**: Rejection of files exceeding size limits (50 MiB / 500 MiB), malformed syntax, corrupt JSON lines, and automatic redaction of API keys (`sk-ant-...`, `Bearer ...`).

### Tier 3: Cross-Feature Combinations
- Validates interactions where outputs from one subsystem flow directly into another:
  - **Ingest -> Facts & Graph**: Ingesting technical documentation and codebases creates atomic constraints and graph entities that mutually reinforce each other.
  - **Agent Loop -> Traces -> Dynamic Dreaming -> Profile**: Agent multi-turn runs leave trace breadcrumbs; dreaming scans these traces, updates graph relationships, and synthesizes `memory/profile.md`.
  - **Error Log Ingestion -> Lesson Proposal -> Graph Linking -> Boosted Retrieval**: An ingested production error generates a candidate lesson; linking it in the knowledge graph boosts it to rank #1 for subsequent incident queries.
  - **Bitemporal Fact Supersession -> Time-Travel Retrieval**: Historical query snapshotting guarantees that past queries see original facts while current queries see modern facts.
  - **Local Capture -> Hub Sync -> Scoped Retrieval Parity**: Traces captured with local scopes synchronize to the Hub and return identical search results under GIN scope queries.

### Tier 4: Real-World Scenarios
- End-to-end simulations of actual multi-agent fleet operations:
  - **Fleet Incident & Postmortem Lifecycle**: Multi-agent fleet encounters cascading failures, ingests post-mortems, automatically runs dreaming passes, promotes governed lessons, and verifies that subsequent fleet tasks retrieve and apply preventative measures.
  - **Autonomous Coding Agent Lifecycle**: Developer agent initialized with `persona`, `human`, and `project` blocks navigates codebase graph, reads architectural facts, writes code changes, updates working memory blocks, and records auditable revision history.
  - **Multi-Tenant Scoped Governance**: Enterprise deployment spanning `backend`, `frontend`, and `data-infra` teams with strict scope isolation, verifying zero cross-tenant contamination while sharing global organizational standards.

---

## 5. Pass/Fail Criteria & Gating

- **Pass Rate**: 100% of applicable tests must pass without errors or regressions.
- **Execution Time**: The complete E2E test suite must finish in under 3 minutes.
- **Zero Pollution**: No tests may leave orphan files in repository working directories or fail to truncate test database tables.
- **Lint & Diagnostics**: Codebase must pass `ruff check` and `commontrace doctor`.
