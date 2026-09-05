# Project: commontrace-v2 Remediation & Hardening Master Plan

## Architecture & System Overview
CommonTrace is an open, agent-agnostic memory protocol and reference implementation:
1. **Protocol Core (`protocol/`)**: JSON schemas (`trace.schema.json`, `lesson.schema.json`) and architectural specification (`PROTOCOL.md`).
2. **CLI & Local Store (`commontrace/`)**: Standard subcommands (`capture`, `lesson`, `query`, `index`, `bench`, `pilot`, `taxonomy`, `doctor`, `serve`, `install`, `init`).
3. **Memory & Attention Subsystem (`memory/`)**: Attention mechanism (`memory/attention/`), embedding index, cosine similarity query, lesson and episode stores.
4. **Hub Subsystem (`hub/`)**: Multi-tenant server with trace contribution, search, amend, outcome validation, and rate limiting.
5. **Commons & Research Subsystem (`commons/`)**: Global substrate and MinHash evaluation benchmarks.
6. **Benchmark & Test Suite (`benchmark/`, `commontrace/reference/`, `tests/`)**: Regression testing and performance profiling.

---

## Feature & Defect Inventory

| # | Item ID | Category | Component | Description & Target Location | Assigned Milestone |
|---|---|---|---|---|---|
| 1 | R1-1 | Bug | Memory/Attention | Unhandled `FileNotFoundError`/`OSError` during index staleness check in `memory/attention/build_index.py:274` | M1 |
| 2 | R1-2 | Bug | Pilot Metrics | Unhandled `OSError`/`PermissionError` in `commontrace/reference/pilot_metrics.py:56` (`load_traces`) | M1 |
| 3 | R1-3 | Bug | Benchmark/Freshness | Offset-naive vs offset-aware datetime comparison crash in `commontrace/reference/measure_performance.py:984` | M1 |
| 4 | R1-4 | Bug | Benchmark/Lexical | ASCII-only token regex in `commontrace/reference/measure_performance.py:919` stripping Unicode words | M1 |
| 5 | R1-5 | Bug | Trace IO | Regex positive lookahead swallowing subsequent sections into context in `commontrace/trace_io.py:20` | M1 |
| 6 | R1-6 | Bug | Evidence IO | `load_active_lessons` in `commontrace/evidence_io.py:27` fails to filter `status == "active"` | M1 |
| 7 | R1-7 | Bug | CLI Taxonomy | Unvalidated float threshold accepting `<=0` and `nan` in `commontrace/commands/taxonomy_cmd.py:21` | M1 |
| 8 | R1-8 | Bug | CLI Main | Unhandled unexpected runtime exceptions in `commontrace/cli.py:120` | M1 |
| 9 | R2-1 | Perf | CLI Capture | O(N) linear disk scan on every capture with `--occasion-id` in `commontrace/commands/capture_cmd.py:126` | M1 |
| 10 | R2-2 | Perf | CLI Pilot | Redundant quad-walk reading trace directory 4 separate times in `commontrace/commands/pilot_cmd.py:74` | M1 |
| 11 | R2-3 | Perf | Memory/Query | Dual disk scan during `load_importances` and `check_staleness` in `memory/attention/query.py:102, 241` | M1 |
| 12 | R2-5 | Perf | Overlap MinHash | Unvectorized pure Python loop in MinHash Jaccard comparison in `commontrace/overlap.py:175` | M1 |
| 13 | R2-6 | Perf | Frontmatter | Redundant umask probe file creation on every write in `commontrace/frontmatter.py:178` | M1 |
| 14 | R4-1 | Unwired | CLI Query | `--include-importance-floor` parameter supported by `query.py` but unwired in `commontrace/commands/query_cmd.py:27` | M1 |
| 15 | R4-3 | Unwired | CLI Serve | Inaccurate help text pointing to nonexistent `--mcp` flag in `commontrace/commands/serve_cmd.py:21` | M1 |
| 16 | R4-4 | Unwired | CLI Init | Missing `memory/attention/` scaffolding and index setup in `commontrace/commands/init_cmd.py:33` | M1 |
| 17 | REC-01 | Bug/Schema | Hub Outcomes | `hub/outcomes.py:214-236` `validate_outcome` rejects `None` for nullable outcome fields (`resolved`, `escalated`, `tokens_used`) | M2 |
| 18 | REC-02 | Schema | Memory Template | `memory/lessons/lesson_template.md:8` has empty `importance_rationale: ""`, violating `minLength: 1` | M2 |
| 19 | REC-03 | Security | Hub CRUD | `hub/crud.py:801-804` `search_traces` updates retrievals without `Trace.org_id == org_id` in SQL where clause | M2 |
| 20 | REC-04 | Protocol | CLI Capture | `commontrace/commands/capture_cmd.py:25` restricts open-vocabulary `--agent-type` to hardcoded choices | M2 |
| 21 | REC-05 | Bug | Hub CRUD | `hub/crud.py:1692-1700` `amend_trace` omits `"profile": original.profile` in wire dict validated by `validate_trace` | M2 |
| 22 | REC-06 | Bug | Commons Eval | `commons/eval/representations.py:65` uses ASCII-only regex stripping Unicode | M2 |
| 23 | REC-07 | Schema | Protocol Schemas | `protocol/schemas/trace.schema.json` omits wire fields `shared_with_commons`, `quarantined`, `quarantine_reason` | M2 |
| 24 | REC-08 | Robustness | Hub Abuse | `hub/abuse.py:519-525` `_SharedPgPool` leaks background thread on connection pool startup timeout | M2 |
| 25 | EXP3-1 | Bug | Benchmark | Benchmark report collision sorting flaw in `commontrace/reference/measure_performance.py:1059` (`-` < `.`) | M3 |
| 26 | EXP3-2 | Security | Benchmark | TOCTOU non-atomic write in `measure_performance.py:1065` report persistence | M3 |
| 27 | EXP3-3 | Contract | Benchmark/Pilot | JSON mode output violation on empty corpus in `measure_performance.py` and `pilot_metrics.py` | M3 |
| 28 | EXP3-4 | Security | Benchmark | HTML quote escaping defect in `measure_performance.py:1404` (`quote=False`) | M3 |
| 29 | EXP3-5 | Perf | Benchmark | Missing memoization in `resolve_project` during `compute_transfer_gap` in `measure_performance.py:537` | M3 |
| 30 | EXP3-6 | Security | CLI Install | Root path resolution in `install_cmd.py:268` ignores `args.dest`, hardcoding cwd into `.mcp.json` | M3 |
| 31 | EXP3-7 | Robustness | Shellout | `PYTHONUTF8=1` missing on streaming subprocess execution in `commontrace/commands/_shellout.py:124` | M3 |
| 32 | EXP3-8 | Coverage | CLI Index | `commontrace/commands/index_cmd.py` completely untested in `tests/` | M3 |
| 33 | EXP3-9 | Coverage | CLI Install | Untested target platforms in `install_cmd.py` (`cursor`, `windsurf`, `devin`, `generic`) | M3 |
| 34 | EXP3-10 | Coverage | Internal Modules | Dedicated unit tests for `commontrace/report_html.py` and `commontrace/evidence_io.py` | M3 |
| 35 | VERIF-1 | Quality Gate | Verification | Full regression test suite pass (`pytest tests/ -v`) with 0 failures | M4 |
| 36 | VERIF-2 | Quality Gate | Benchmark | `commontrace bench` executes successfully without performance degradation | M4 |
| 37 | VERIF-3 | Quality Gate | Diagnostics | `commontrace doctor` passes all checks | M4 |
| 38 | VERIF-4 | Quality Gate | Schemas | All lessons and trace samples validate against JSON schemas | M4 |
| 39 | VERIF-5 | Quality Gate | Adversarial Review | Independent Reviewers and Challengers confirm correctness | M4 |
| 40 | VERIF-6 | Quality Gate | Forensic Audit | Forensic Auditor confirms clean implementation with 0 integrity violations | M4 |

---

## Milestones Decomposition

| Milestone | Name | Target Scope & Files | Dependencies | Status |
|---|---|---|---|---|
| **M1** | Core CLI, Evidence IO & Memory/Attention | `commontrace/`, `memory/attention/`, `tests/` | None | PLANNED |
| **M2** | Hub, Commons, & Protocol Conformance | `hub/`, `commons/`, `protocol/`, `memory/lessons/lesson_template.md`, `tests/` | None | DONE |
| **M3** | Benchmark Integrity, Security & Test Gaps | `commontrace/reference/`, `commontrace/commands/install_cmd.py`, `_shellout.py`, `tests/` | M1, M2 | PLANNED |
| **M4** | Verification, Adversarial Review & Forensic Audit | Entire workspace, review & audit tools | M1, M2, M3 | PLANNED |

---

## Interface Contracts & Guidelines
- **Zero Breaking Changes**: All public CLI interfaces, function signatures, and JSON schema properties must maintain 100% backward compatibility.
- **Strict Typing & Nullability**: Outcome metrics (`resolved`, `escalated`, `tokens_used`, etc.) must accept both valid typed values and `None` (JSON null).
- **Atomic File Writes**: File saving operations (`measure_performance.py`, `frontmatter.py`) must use atomic replace or exclusive locks.
- **Dedicated Regression Tests**: Every defect remediation MUST include a corresponding test case in `tests/` verifying the failure mode before the fix and passing after.

---

## Code Layout Boundaries
- **Milestone 1 Worker Ownership**:
  - `memory/attention/build_index.py`
  - `commontrace/reference/pilot_metrics.py` (load_traces only)
  - `commontrace/trace_io.py`
  - `commontrace/evidence_io.py`
  - `commontrace/commands/taxonomy_cmd.py`
  - `commontrace/cli.py`
  - `commontrace/commands/capture_cmd.py`
  - `commontrace/commands/pilot_cmd.py`
  - `memory/attention/query.py`
  - `commontrace/overlap.py`
  - `commontrace/frontmatter.py`
  - `commontrace/commands/query_cmd.py`
  - `commontrace/commands/serve_cmd.py`
  - `commontrace/commands/init_cmd.py`
  - `tests/test_m1_regressions.py`
- **Milestone 2 Worker Ownership**:
  - `memory/lessons/lesson_template.md`
  - `hub/outcomes.py`
  - `hub/crud.py`
  - `hub/abuse.py`
  - `commons/eval/representations.py`
  - `protocol/schemas/trace.schema.json`
  - `commontrace/schemas/trace.schema.json`
  - `tests/test_m2_protocol_hub_regressions.py`
- **Milestone 3 Worker Ownership**:
  - `commontrace/reference/measure_performance.py`
  - `commontrace/commands/install_cmd.py`
  - `commontrace/commands/_shellout.py`
  - `commontrace/commands/index_cmd.py`
  - `tests/test_index_cmd.py`
  - `tests/test_install_targets.py`
  - `tests/test_m3_benchmark_regressions.py`
