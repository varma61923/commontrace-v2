# Project: CommonTrace v2 Enterprise Cognitive Governed Memory Platform

## Architecture
CommonTrace v2 is a governed cognitive memory and verification architecture for autonomous agent fleets.
It unifies four major memory subsystems:
1. **Working Memory Engine (`commontrace/memory_blocks.py`)**: Letta-style bounded, named memory blocks (`persona`, `human`, `project`) with character quotas, atomic substring replacements, and SHA-256 revision history in `memory/blocks/history.jsonl`.
2. **Hierarchical Atomic Facts Engine (`commontrace/hierarchical.py`)**: Mem0/EverOS bitemporal atomic facts with lifecycle transitions (`ADD`, `NOOP`, `UPDATE`, `SUPERSEDE`, `DELETE`), confidence scoring, scoped routing, and point-in-time `as_of` querying in `memory/facts/facts.jsonl`.
3. **Temporal Property Graph (`commontrace/graph.py`)**: Zep/Cognee entity-relationship graph connecting services, tools, errors, concepts, and lessons with multi-hop BFS traversal and distance-decayed relevance boosting in `memory/graph/nodes.jsonl` and `edges.jsonl`.
4. **Multimodal Ingestion Pipeline (`commontrace/ingest/`)**: Cognee/Supermemory multimodal connectors parsing code repositories (AST), Markdown documentation (hierarchical headers), JSON structured logs (error clustering), and execution failure transcripts into governed lessons, atomic facts, and graph nodes via `commontrace ingest`.
5. **Autonomous Agent Runtime Loop (`commontrace/agent_loop.py`)**: Multi-turn agent execution coordinator dynamically assembling bounded memory blocks, querying graph/facts via MCP tools, writing episodic execution traces, and triggering dynamic dreaming passes (`commontrace dream`).
6. **Enterprise Hub & Protocol Layer (`hub/`, `protocol/`)**: Centralized multi-tenant Hub with PostgreSQL GIN indexing on scopes, bitemporal `as_of`/`valid_from`/`valid_until` queries across REST (`/api/v1/traces`) and MCP tools, backed by reversible Alembic migrations.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Working Memory Block CRUD & Quotas | Bounded named blocks with strict character quota ceilings | M1 | survey_1 |
| 2 | Atomic Substring Replacement | Safe, unambiguous replacement of unique strings within blocks | M1 | survey_1 |
| 3 | SHA-256 Revision History | Cryptographic revision audit logging in `memory/blocks/history.jsonl` | M1 | survey_1 |
| 4 | Atomic File Writes for Memory Blocks | Temporary file write + `os.replace` to prevent corruption | M1 | survey_1 |
| 5 | Memory Block MCP Tools | Stdio MCP tools (`read`, `update`, `list`, `delete`) | M1 | survey_1 |
| 6 | Bitemporal Atomic Fact Lifecycle | EverOS/Mem0 propositions with ADD, NOOP, UPDATE, SUPERSEDE, DELETE | M1 | survey_1 |
| 7 | Bitemporal `as_of` Querying | Accurate historical snapshot filtering (`valid_from <= as_of < valid_until`) | M1 | survey_1 |
| 8 | Atomic Facts MCP Tools | Stdio MCP tools (`record_fact`, `query_facts`) | M1 | survey_1 |
| 9 | Temporal Knowledge Graph | Zep/Cognee graph with nodes, edges, and multi-hop BFS | M1 | survey_1 |
| 10 | Distance-Decayed Graph Boosting | Proximity boosting for candidate lessons ($0.250 / (1 + \text{hop})$) | M1 | survey_1 |
| 11 | CLI Query Graph Boost Integration | Wiring `graph_boost_for_lessons` and `graph_weight` into `query_cmd.py` | M1 | survey_1 |
| 12 | Knowledge Graph MCP Tools & Resources | Stdio tools (`graph_query`, `graph_neighbors`, `commontrace://graph`) | M1 | survey_1 |
| 13 | Formal Cognitive Memory Schemas | JSON schemas for memory blocks, atomic facts, and graph models | M1 | survey_1 |
| 14 | Retrieval Benchmark Verification | Gating 100% Precision@1 and worst-field pollution <= 1.06x | M1 | survey_1 |
| 15 | Code Repository Ingestion Connector | AST parsing, symbol extraction to graph, architectural docstrings | M2 | survey_2 |
| 16 | Markdown Documentation Connector | Hierarchical heading chunking, guideline extraction to atomic facts | M2 | survey_2 |
| 17 | JSON Structured Log Connector | Error message clustering, fingerprinting, service/error nodes & edges | M2 | survey_2 |
| 18 | Failure Transcript Connector | Turn parsing, failure point isolation, candidate lesson drafting | M2 | survey_2 |
| 19 | Ingestion Pipeline Orchestrator | Secret redaction, routing to facts, graph, and lessons | M2 | survey_2 |
| 20 | Ingestion CLI Command | `commontrace ingest` unified command with format & filter flags | M2 | survey_2 |
| 21 | Autonomous Agent Execution Loop | Multi-turn Letta-pattern agent loop (`commontrace/agent_loop.py`) | M3 | survey_2 |
| 22 | Context Dynamic Assembly | Re-assembling prompt context with active blocks & profile | M3 | survey_2 |
| 23 | Execution Trace Logging | Persisting execution outcome and occasion lineage to `memory/traces/` | M3 | survey_2 |
| 24 | Dreaming Trace Graph Mining Fix | Scanning `.md` (and `.json`) traces to mine graph relationships | M3 | survey_2,3 |
| 25 | Dreaming Signal Consolidation | Fleet failure distillation, lesson conflict & staleness resolution | M3 | survey_2 |
| 26 | Active Space Profile Synthesis | Consolidation of active blocks, facts, and health in `memory/profile.md` | M3 | survey_2 |
| 27 | Agent CLI Command | `commontrace agent run` entrypoint for multi-turn task execution | M3 | survey_2 |
| 28 | Hub PostgreSQL GIN Indexing | Inverted indexing on `traces.scopes` for $O(\log N)$ scoped queries | M4 | survey_3 |
| 29 | Alembic Migration for Scopes & Validity | Migration adding `scopes`, `valid_from`, `valid_until`, and GIN index | M4 | survey_3 |
| 30 | Hub Bitemporal & Scoped Trace Queries | Querying traces with bitemporal `as_of` and scope containment | M4 | survey_3 |
| 31 | Hub Trace Amendment Data Preservation | Carrying forward `scopes`, `valid_from`, `valid_until` on amendment | M4 | survey_3 |
| 32 | Hub REST API Scoped Endpoints | `/api/v1/traces` and `/api/v1/traces/search` scope & bitemporal support | M4 | survey_3 |
| 33 | Hub MCP Server Parameter Parity | Exposing `scope`/`scopes`, `as_of`, `valid_from`, `valid_until` on MCP | M4 | survey_3 |
| 34 | Protocol & Client Trace Schema Parity | Updating `trace.schema.json` with scopes, valid_from, valid_until | M4 | survey_3 |
| 35 | CLI Capture Scope & Temporal Flags | Adding `--scope`/`--scopes`, `--valid-from`, `--valid-until` to `capture` | M4 | survey_3 |
| 36 | Client Hub Sync Scope Forwarding | Propagating scopes and validity windows in `hub_client.py` | M4 | survey_3 |
| 37 | System Diagnostics & Linter Quality | `commontrace doctor` cognitive checks and `ruff check` verification | M4 | survey_3 |
| 38 | E2E Test Suite (Tiers 1-4) | Comprehensive opaque-box requirement tests across all features | E2E / M5 | survey_1,2,3 |
| 39 | Adversarial Hardening (Tier 5) | White-box stress testing and edge-case bug hunting | M5 | survey_1,2,3 |
| 40 | Final Victory Audit | Multi-point forensic integrity audit verifying zero cheat/facades | M5 | Sentinel |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Cognitive Memory Hardening & Benchmark Verification | Memory blocks atomic writes, `as_of` bitemporal fact fix, CLI graph boost, MCP tools, schemas, benchmark gating | none | IN_PROGRESS |
| M2 | Enterprise Ingestion & Multimodal Connectors | Code AST, Markdown, JSON log, Transcript connectors, `commontrace ingest` CLI, pipeline tests | M1 | PLANNED |
| M3 | Autonomous Agent Loops & Self-Consolidation Runtime | Multi-turn `AgentLoop`, dreaming trace mining bug fix (`.md`), profile generation, agent CLI | M1, M2 | PLANNED |
| M4 | Enterprise Hub Parity, Protocol Release & Diagnostics | Alembic migration, `crud.amend_trace` fix, Hub MCP parity, `trace.schema.json`, capture CLI, client sync | M1 | PLANNED |
| M5 | Final Milestone: 100% E2E Pass & Adversarial Hardening | Pass 100% E2E test suite (Tiers 1-4), Tier 5 adversarial hardening, full system diagnostics, Victory Audit | M1, M2, M3, M4, TEST_READY | PLANNED |

In parallel:
- **E2E Testing Track**: Autonomous test orchestrator developing test harness and Tiers 1-4 test cases according to `TEST_INFRA.md`, publishing `TEST_READY.md`.

## Interface Contracts

### Cognitive Memory Engine ↔ CLI & MCP (`commontrace`)
- `hierarchical.list_facts(root, status="active", scope="", category="", as_of=None) -> list[AtomicFact]`
  - Contract: If `as_of` is provided, facts valid at that point in time (`valid_from <= as_of < valid_until`) MUST be included regardless of whether their current status is `superseded` or `deleted`.
- `retrieval.rank_lessons(task, active, ..., graph_boost_lookup=None, graph_weight=1.0)`
  - Contract: Integrated score formula is $\min(1.0, \max(0.0, \text{Rel} + w_{\text{rel}} A_{\text{rel}} + w_{\text{rec}} A_{\text{rec}} + w_{\text{graph}} A_{\text{graph}}))$.
  - CLI `commontrace query` and MCP `retrieve` MUST both pass `graph_boost_lookup` and respect `--graph-weight`.
- `memory_blocks.set_block(root, name, content, max_chars, actor, reason) -> MemoryBlock`
  - Contract: File operations MUST write to temporary file (`.tmp`) and atomically commit via `os.replace`.

### Ingestion Pipeline ↔ Memory Stores (`commontrace.ingest` ↔ `commontrace`)
- `IngestionPipeline.ingest_source(path, source_type, dest_root, scope="", **kwargs) -> IngestionResult`
  - Contract:
    - Code: Chunks bounded to 2,500 chars with AST breadcrumbs. Writes nodes (`file`, `service`, `tool`) and candidate lessons.
    - Markdown: Chunks bounded to 2,000 chars with heading hierarchy breadcrumbs. Writes facts under category `constraint`/`preference` and candidate lessons.
    - JSON Logs: Groups errors by fingerprint. Writes episodic traces to `memory/traces/` and `service`/`error` graph nodes.
    - Transcripts: Extracts prompts, tools, and failure outputs. Writes traces and drafts lessons with `status: review`.

### Autonomous Agent Runtime ↔ Memory & Dreaming (`commontrace.agent_loop` ↔ `commontrace`)
- `AgentLoop.run(prompt, max_turns=20, ...) -> AgentRunResult`
  - Contract: Assembles working memory blocks (`persona`, `human`, `project`), executes multi-turn tool cycle, logs SHA-256 block revision history to `memory/blocks/history.jsonl`, persists execution trace with YAML frontmatter in `memory/traces/`.
- `commontrace dream [--dest DEST]`
  - Contract: MUST parse both `.md` (via `frontmatter.read`) and `.json` files in `memory/traces/`, mining concepts and tags into graph edges. Synthesizes `memory/profile.md`.

### CommonTrace Hub ↔ Clients (`hub` ↔ `commontrace.hub_client` & MCP)
- `hub.crud.search_traces(db, org_id, query="", tags=None, scope="", as_of=None, ...)`
  - Contract: Bitemporal condition `valid_from <= as_of < valid_until` (null `valid_from` means $-\infty$, null `valid_until` means $+\infty$). Scope condition matches `scopes @> ARRAY[scope]` or `scopes == []`.
- `hub.crud.amend_trace(db, org_id, original_id, ...)`
  - Contract: MUST preserve `original.scopes`, `original.valid_from`, and `original.valid_until` unless explicitly overridden.
- MCP Tools (`search_traces`, `contribute_trace`, `amend_trace`):
  - Contract: Expose parameter parity matching REST API.

## Code Layout
```
commontrace/
├── agent_loop.py               # Autonomous agent multi-turn execution loop (M3)
├── commands/
│   ├── block_cmd.py            # CLI memory block commands (M1)
│   ├── capture_cmd.py          # CLI trace capture with scope/temporal flags (M4)
│   ├── doctor_cmd.py           # CLI diagnostic health checks (M4)
│   ├── dream_cmd.py            # CLI dynamic dreaming with .md trace mining (M3)
│   ├── fact_cmd.py             # CLI atomic facts commands (M1)
│   ├── graph_cmd.py            # CLI knowledge graph commands (M1)
│   ├── ingest_cmd.py           # CLI multimodal ingestion command (M2)
│   └── query_cmd.py            # CLI retrieval query with graph boost (M1)
├── graph.py                    # Knowledge graph engine (M1)
├── hierarchical.py             # Atomic facts engine with bitemporal as_of fix (M1)
├── hub_client.py               # Hub sync client with scope/temporal forwarding (M4)
├── ingest/                     # Multimodal ingestion connectors (M2)
│   ├── __init__.py
│   ├── code.py                 # AST code connector (M2)
│   ├── logs.py                 # JSON log connector (M2)
│   ├── markdown.py             # Hierarchical Markdown connector (M2)
│   ├── pipeline.py             # Unified ETL pipeline (M2)
│   └── transcript.py           # Failure transcript connector (M2)
├── mcp_server.py               # Local MCP server with cognitive tools (M1)
├── mcp_tools.py                # Local MCP tool declarations (M1)
├── memory_blocks.py            # Letta working memory blocks with atomic writes (M1)
├── retrieval.py                # Retrieval engine with graph boost & adaptive tail (M1)
├── retrieval_io.py             # Retrieval configuration (M1)
└── schemas/                    # JSON schema definitions (M1, M4)
    ├── atomic_fact.schema.json
    ├── graph_edge.schema.json
    ├── graph_node.schema.json
    ├── memory_block.schema.json
    └── trace.schema.json

hub/
├── alembic/versions/           # Alembic migrations (M4)
│   └── ..._traces_scopes_and_temporal_bounds.py
├── crud.py                     # Hub CRUD operations & amend preservation (M4)
├── models.py                   # SQLAlchemy ORM models (M4)
├── rest.py                     # REST endpoints (M4)
└── server.py                   # Hub MCP server with parameter parity (M4)

protocol/
└── schemas/
    └── trace.schema.json       # Protocol trace schema parity (M4)

tests/                          # Unit and integration test suites
├── test_agent_loop.py          # Agent loop tests (M3)
├── test_cross_field_retrieval.py # Benchmark gating (M1)
├── test_hierarchical_facts.py  # Atomic facts tests (M1)
├── test_ingestion_connectors.py # Ingestion connector tests (M2)
├── test_knowledge_graph.py     # Knowledge graph tests (M1)
├── test_mcp_cognitive_tools.py # MCP tools tests (M1)
└── test_memory_blocks.py       # Memory blocks tests (M1)

hub/tests/
├── test_rest.py                # REST API tests (M4)
└── test_scoped_temporal_traces.py # Scoped temporal traces tests (M4)

e2e_tests/                      # Requirement-driven E2E test suite (E2E Track)
├── harness/
├── tier1_features/
├── tier2_boundaries/
├── tier3_combinations/
└── tier4_real_world/
```
