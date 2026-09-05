# Project: commontrace-v2 Audit & Remediation

## Architecture
- `commontrace/`: CLI entry point (`cli.py`), commands (`capture`, `lesson`, `query`, `index`, `bench`, `doctor`, `serve`, `init`, `install`, `pilot`, `taxonomy`), I/O utilities (`frontmatter.py`, `trace_io.py`, `evidence_io.py`, `validate.py`, `overlap.py`, `report_html.py`).
- `memory/`: Durable agent memory layer. Attention mechanism (`memory/attention/build_index.py`, `memory/attention/query.py`), stored lessons (`memory/lessons/`), episodes (`memory/episodes/`), index (`memory/INDEX.md`).
- `protocol/`: Protocol specification (`protocol/PROTOCOL.md`) and canonical JSON schemas (`protocol/schemas/trace.schema.json`, `protocol/schemas/lesson.schema.json`).
- `commontrace/reference/`: Packaged benchmark and evaluation engine (`measure_performance.py`, `pilot_metrics.py`).
- `hub/`: Multi-tenant server and storage engine (`server.py`, `crud.py`, `models.py`, `auth.py`, `abuse.py`, `outcomes.py`, `search.py`, `schema_validation.py`).
- `commons/`: Decentralized trace and lesson exchange (`eval/`, `seed/`).
- `tests/`: Pytest test suite covering CLI, frontmatter, benchmark, MCP server, and reference implementations.

## Feature & Defect Inventory
| # | Finding ID | Description | Milestone | Source |
|---|---|---|---|---|
| 1 | S1-BUG-1 | `memory/attention/build_index.py:274`: Unhandled `FileNotFoundError`/`OSError` during staleness check | M1 | Survey 1 |
| 2 | S1-BUG-5 | `commontrace/trace_io.py:20`: Greedy `_SECTION_RE` regex lookahead swallows extra markdown sections into `context` | M1 | Survey 1 |
| 3 | S1-BUG-6 | `commontrace/evidence_io.py:27`: `load_active_lessons` does not filter by `status == "active"` | M1 | Survey 1 |
| 4 | S1-BUG-7 | `commontrace/commands/taxonomy_cmd.py:21`: Unvalidated `--similarity-threshold` float parameter | M1 | Survey 1 |
| 5 | S1-BUG-8 | `commontrace/cli.py:120`: Catch broader operational exceptions (`TypeError`, `IndexError`) gracefully | M1 | Survey 1 |
| 6 | S1-PERF-1 | `commontrace/commands/capture_cmd.py:126`: Full linear trace directory walk on capture with `--occasion-id` | M1 | Survey 1 |
| 7 | S1-PERF-3 | `memory/attention/query.py:102, 241`: Redundant dual-pass scan of lessons during retrieval staleness check | M1 | Survey 1 |
| 8 | S1-PERF-4 | `commontrace/commands/_shellout.py`: Subprocess overhead on `commontrace query` and `index` | M1 | Survey 1 |
| 9 | S1-PERF-5 | `commontrace/overlap.py:175`: Unvectorized pure Python loop in MinHash Jaccard similarity | M1 | Survey 1 |
| 10 | S1-PERF-6 | `commontrace/frontmatter.py:178`: Redundant probe file creation on every write to detect umask | M1 | Survey 1 |
| 11 | S1-CODE-1 | `commontrace/commands/query_cmd.py`: Missing `--include-importance-floor` CLI argument | M1 | Survey 1 |
| 12 | S1-CODE-2 | `commontrace/commands/query_cmd.py:189`: Unwired `--agent-type` flag in semantic retrieval | M1 | Survey 1 |
| 13 | S1-CODE-3 | `commontrace/commands/serve_cmd.py:21`: Inaccurate help text pointing to nonexistent `--mcp` flag | M1 | Survey 1 |
| 14 | S1-CODE-4 | `commontrace/commands/init_cmd.py`: Missing `memory/attention` initialization and scaffolding | M1 | Survey 1 |
| 15 | S2-PROTO-1 | `commontrace/commands/capture_cmd.py:25`: Restrictive `choices=paths.AGENT_TYPES` violates open protocol vocabulary | M1 | Survey 2 |
| 16 | S1-BUG-2 | `commontrace/reference/pilot_metrics.py:56`: Unhandled `OSError` in `load_traces` | M2 | Survey 1 |
| 17 | S1-BUG-3 | `commontrace/reference/measure_performance.py:984`: Naive vs aware datetime comparison in `compute_freshness` | M2 | Survey 1 |
| 18 | S1-BUG-4 | `commontrace/reference/measure_performance.py:919`: Inconsistent ASCII-only regex `_TOKEN_RE` stripping Unicode | M2 | Survey 1 |
| 19 | S1-PERF-2 | `commontrace/commands/pilot_cmd.py:74`: Redundant quad-walk of trace directory parsing files 4 times | M2 | Survey 1 |
| 20 | S3-BUG-1 | `commontrace/reference/measure_performance.py:1059`: Collision sorting flaw reversing chronological order in `load_stored_reports` | M2 | Survey 3 |
| 21 | S3-BUG-2 | `commontrace/reference/measure_performance.py:1065`: Non-atomic TOCTOU file write in `persist_report` and HTML | M2 | Survey 3 |
| 22 | S3-BUG-3 | `commontrace/reference/measure_performance.py:1662`: Emits plain text instead of valid JSON on empty corpus under `--json` | M2 | Survey 3 |
| 23 | S3-PERF-1 | `commontrace/reference/measure_performance.py:537`: Missing memoization in `resolve_project` in `compute_transfer_gap` | M2 | Survey 3 |
| 24 | S3-BUG-4 | `commontrace/reference/measure_performance.py:1404`: HTML quote unescaping (`quote=False`) causing malformed tags | M2 | Survey 3 |
| 25 | S3-BUG-5 | `commontrace/reference/measure_performance.py`: False positive `--strict` threshold alerts on clean installations | M2 | Survey 3 |
| 26 | S3-SEC-1 | `commontrace/commands/install_cmd.py:268`: Missing `args.dest` in `paths.resolve_root()`, hardcoding caller cwd | M2 | Survey 3 |
| 27 | S3-TEST-1 | `commontrace/commands/index_cmd.py`: Zero test coverage across test suite | M2 | Survey 3 |
| 28 | S3-TEST-2 | `commontrace/commands/install_cmd.py`: Untested target variants (`cursor`, `windsurf`, `devin`, `generic`) | M2 | Survey 3 |
| 29 | S3-TEST-3 | `commontrace/report_html.py` & `commontrace/evidence_io.py`: Missing unit tests for error paths | M2 | Survey 3 |
| 30 | S2-BUG-1 | `hub/outcomes.py:213`: `validate_outcome` rejects `None` for outcome fields allowed by `trace.schema.json` | M3 | Survey 2 |
| 31 | S2-SCHEMA-1 | `memory/lessons/lesson_template.md:8`: `importance_rationale: ""` violates `minLength: 1` in `lesson.schema.json` | M3 | Survey 2 |
| 32 | S2-SEC-1 | `hub/crud.py:801`: `search_traces` updates `retrievals` without `Trace.org_id == org_id` in SQL `where` clause | M3 | Survey 2 |
| 33 | S2-BUG-2 | `hub/crud.py:1692`: `amend_trace` omits `profile` from inbound validation `wire` object | M3 | Survey 2 |
| 34 | S2-BUG-3 | `commons/eval/representations.py:65`: ASCII-only word regex `_WORD` strips Unicode non-Latin characters | M3 | Survey 2 |
| 35 | S2-SCHEMA-2 | `protocol/schemas/trace.schema.json`: Document `shared_with_commons`, `quarantined`, `quarantine_reason` properties | M3 | Survey 2 |
| 36 | S2-PERF-1 | `hub/abuse.py:519`: `_SharedPgPool` background thread leak on startup timeout | M3 | Survey 2 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Core CLI, Evidence/Trace I/O, and Memory/Attention Engine | Findings 1-15 | none | PLANNED |
| M2 | Benchmark Engine, Reference Metrics, HTML Reporting, and Test Expansion | Findings 16-29 | M1 | PLANNED |
| M3 | Protocol Schemas, Memory Templates, Hub Invariants, and Commons | Findings 30-36 | none | PLANNED |
| M4 | Final System-Wide Verification & Forensic Audit | Verification gates, full pytest, bench, doctor, audit | M1, M2, M3 | PLANNED |

## Interface Contracts
### `commontrace.evidence_io` ↔ Callers
- `load_active_lessons(root: str, status: str = "active") -> list[dict]`
- Returns only lessons matching `status` (default `"active"`). Callers can pass `status=None` or `status="all"` if they need all lessons.

### `commontrace.reference.measure_performance` ↔ External Tools / MCP
- JSON outputs (`--json`) must always return valid JSON objects or arrays, even on empty corpora (`{"status": "empty", "episodes": 0, "message": "..."}`).
- Report timestamps must use ISO-8601 UTC datetimes or consistent filename formatting where sort order strictly equals chronological order.

### `hub.outcomes` ↔ Protocol Schemas
- `validate_outcome(outcome: dict)` accepts `None` for nullable fields (`resolved`, `escalated`, `repeated_error`, `frustration_signal`, `tokens_used`, `llm_calls`), matching `trace.schema.json`.

## Code Layout
- `commontrace/`: CLI commands, frontmatter parser, trace/evidence I/O, schemas, validation.
- `memory/`: Attention indexing, query, lesson templates, markdown lessons.
- `protocol/`: Canonical JSON schemas and PROTOCOL.md.
- `hub/`: Multi-tenant server, CRUD, models, auth, abuse, outcomes.
- `commons/`: Evaluation harnesses, seed datasets.
- `tests/`: Pytest tests.
