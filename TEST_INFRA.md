# CommonTrace v2 Test Infrastructure & E2E Test Suite Specification

This document defines the automated End-to-End (E2E) testing architecture, fixture contracts, feature inventory mappings, and tier classifications for CommonTrace v2.

---

## 1. Test Architecture & Runner Invocation

The CommonTrace v2 E2E test suite evaluates the complete system as an opaque box across all 19 canonical features defined in `PROJECT.md`. Tests run against the public CLI (`commontrace ...`), JSON Schemas, filesystem stores, and the attention/memory subsystem.

### Invocation Commands

```bash
# Run the entire E2E test suite (Tiers 1-4)
python3 -m pytest tests/e2e/ -v

# Run specific tiers
python3 -m pytest tests/e2e/test_tier1_*.py -v
python3 -m pytest tests/e2e/test_tier2_*.py -v
python3 -m pytest tests/e2e/test_tier3_cross_feature.py -v
python3 -m pytest tests/e2e/test_tier4_real_world.py -v

# Run with fail-fast on first error
python3 -m pytest tests/e2e/ -x -v
```

### Environment Configuration & Pre-requisites

1. **Python Runtime**: Python 3.10+
2. **Environment Variables**:
   - `PYTHONUTF8=1`: Enforces strict UTF-8 decoding across CLI execution.
   - `PYTHONSAFEPATH=1`: Enforces subprocess security and prevents module hijacking.
   - `PYTHONPATH`: Prepends project root so editable install / source code is exercised.
3. **Core Dependencies**:
   - `pytest` (>= 7.0)
   - `pyyaml` (YAML frontmatter parsing)
   - `jsonschema` (Protocol validation)
   - `numpy` (Vector operations in attention layer)
   - `sentence-transformers` (Optional/neural semantic indexing, with deterministic mock fallbacks)

---

## 2. Directory Layout & Artifact Ownership

The E2E test suite is located entirely in `tests/e2e/` with dedicated files per feature group and tier:

```
tests/e2e/
├── conftest.py                             # Shared fixtures, CLI execution harness, synthetic factories
├── test_tier1_r1_protocol_cli.py           # Tier 1: M1 Protocol Core & CLI Robustness
├── test_tier1_r2_attention_memory.py       # Tier 1: M2 Attention & Memory Performance
├── test_tier1_r3_security_sandboxing.py    # Tier 1: M3 Security & Sandboxing
├── test_tier1_r4_verification_acceptance.py# Tier 1: M4 & M5 Verification & Acceptance
├── test_tier2_r1_boundaries.py             # Tier 2: M1 Boundary & Negative Corner Cases
├── test_tier2_r2_boundaries.py             # Tier 2: M2 Memory Scaling & Threshold Extremes
├── test_tier2_r3_boundaries.py             # Tier 2: M3 Traversal, Symlink & Path Boundaries
├── test_tier2_r4_boundaries.py             # Tier 2: M4 Schema, Benchmark & Payload Boundaries
├── test_tier3_cross_feature.py             # Tier 3: Cross-Feature State & Workflow Interactions
└── test_tier4_real_world.py               # Tier 4: Real-World Operational Simulations
```

---

## 3. Test Fixture & Harness Design (`conftest.py`)

All tests maintain strict process and filesystem isolation via fixtures:

| Fixture | Scope | Description |
|---|---|---|
| `cli_runner` | function | Subprocess invoker executing `python3 -m commontrace.cli <args>` with captured stdout/stderr, standardized exit codes, and sanitized environment (`PYTHONSAFEPATH=1`). Returns a `CLIResult`. |
| `isolated_store` | function | Generates an ephemeral store directory via `commontrace init --dest <tmp_path> --agent-type code` with clean `memory/` structure. |
| `lesson_factory` | function | Generates valid lesson markdown files with YAML frontmatter conforming to `lesson.schema.json`. |
| `trace_factory` | function | Generates valid trace markdown files with telemetry outcomes conforming to `trace.schema.json`. |
| `episode_factory` | function | Generates benchmark episode files with retrieval, hit, and proposal tracking. |

---

## 4. Feature Inventory Mapping Matrix

Each of the 19 features defined in `PROJECT.md` is covered across all four tiers:

| # | Feature ID | Feature Description | Tier 1 (Happy Path) | Tier 2 (Boundaries) | Tier 3 (Cross-Feature) | Tier 4 (Real-World) |
|---|---|---|---|---|---|---|
| 1 | **R1-F1** | CLI Subcommand Input Validation | `test_tier1_r1_protocol_cli.py::test_r1_f1_*` | `test_tier2_r1_boundaries.py::test_r1_f1_*` | `test_tier3_cross_feature.py::test_tier3_csv_import_with_custom_column_mapping` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 2 | **R1-F2** | Standardized Exit Codes (0, 1, 2, 130) | `test_tier1_r1_protocol_cli.py::test_r1_f2_*` | `test_tier2_r1_boundaries.py::test_r1_f2_*` | `test_tier3_cross_feature.py::test_tier3_sync_command_diagnostics_without_hub` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 3 | **R1-F3** | Pre-Write Schema Enforcement | `test_tier1_r1_protocol_cli.py::test_r1_f3_*` | `test_tier2_r1_boundaries.py::test_r1_f3_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 4 | **R1-F4** | Positional Argument Flag Delimiting | `test_tier1_r1_protocol_cli.py::test_r1_f4_*` | `test_tier2_r1_boundaries.py::test_r1_f4_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 5 | **R1-F5** | Repair `install.sh` Target Paths | `test_tier1_r1_protocol_cli.py::test_r1_f5_*` | `test_tier2_r1_boundaries.py::test_r1_f5_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 6 | **R2-F1** | Bounded-Memory Semantic Deduplication | `test_tier1_r2_attention_memory.py::test_r2_f1_*` | `test_tier2_r2_boundaries.py::test_r2_f1_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 7 | **R2-F2** | Incremental Embedding Indexing | `test_tier1_r2_attention_memory.py::test_r2_f2_*` | `test_tier2_r2_boundaries.py::test_r2_f2_*` | `test_tier3_cross_feature.py::test_tier3_doctor_diagnostics_across_corruption_and_repair` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 8 | **R2-F3** | Fast Query Metadata Co-location | `test_tier1_r2_attention_memory.py::test_r2_f3_*` | `test_tier2_r2_boundaries.py::test_r2_f3_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 9 | **R2-F4** | Inverted-Index Lexical Deduplication | `test_tier1_r2_attention_memory.py::test_r2_f4_*` | `test_tier2_r2_boundaries.py::test_r2_f4_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 10 | **R3-F1** | Centralized Workspace Boundary Enforcement | `test_tier1_r3_security_sandboxing.py::test_r3_f1_*` | `test_tier2_r3_boundaries.py::test_r3_f1_*` | `test_tier3_cross_feature.py::test_tier3_multi_store_isolation_and_cross_store_commons` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 11 | **R3-F2** | Validation Path Sandboxing | `test_tier1_r3_security_sandboxing.py::test_r3_f2_*` | `test_tier2_r3_boundaries.py::test_r3_f2_*` | `test_tier3_cross_feature.py::test_tier3_full_lifecycle_pipeline` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 12 | **R3-F3** | Safe Output Writing & Symlink Breaking | `test_tier1_r3_security_sandboxing.py::test_r3_f3_*` | `test_tier2_r3_boundaries.py::test_r3_f3_*` | `test_tier3_cross_feature.py::test_tier3_export_import_traces_roundtrip` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 13 | **R3-F4** | Subprocess Execution Hardening (`PYTHONSAFEPATH=1`) | `test_tier1_r3_security_sandboxing.py::test_r3_f4_*` | `test_tier2_r3_boundaries.py::test_r3_f4_*` | `test_tier3_cross_feature.py::test_tier3_pilot_evaluation_end_to_end` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 14 | **R4-F1** | Dedicated Schema Validation Test Suite | `test_tier1_r4_verification_acceptance.py::test_r4_f1_*` | `test_tier2_r4_boundaries.py::test_r4_f1_*` | `test_tier3_cross_feature.py::test_tier3_export_import_traces_roundtrip` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 15 | **R4-F2** | Security & Sandboxing Test Suite | `test_tier1_r4_verification_acceptance.py::test_r4_f2_*` | `test_tier2_r4_boundaries.py::test_r4_f2_*` | `test_tier3_cross_feature.py::test_tier3_multi_store_isolation_and_cross_store_commons` | `test_tier4_real_world.py::test_tier4_code_review_pipeline_simulation` |
| 16 | **R4-F3** | Performance & Scalability Test Suite | `test_tier1_r4_verification_acceptance.py::test_r4_f3_*` | `test_tier2_r4_boundaries.py::test_r4_f3_*` | `test_tier3_cross_feature.py::test_tier3_pilot_evaluation_end_to_end` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 17 | **R4-F4** | Diagnostic & Benchmark Health Verification | `test_tier1_r4_verification_acceptance.py::test_r4_f4_*` | `test_tier2_r4_boundaries.py::test_r4_f4_*` | `test_tier3_cross_feature.py::test_tier3_doctor_diagnostics_across_corruption_and_repair` | `test_tier4_real_world.py::test_tier4_large_corpus_stress_and_dosage_management` |
| 18 | **R4-F5** | Final E2E Test Suite Validation | `test_tier1_r4_verification_acceptance.py::test_r4_f5_*` | `test_tier2_r4_boundaries.py::test_r4_f5_*` | `test_tier3_cross_feature.py::*` | `test_tier4_real_world.py::*` |
| 19 | **R4-F6** | Adversarial Coverage Hardening | `test_tier1_r4_verification_acceptance.py::test_r4_f6_*` | `test_tier2_r4_boundaries.py::test_r4_f6_*` | `test_tier3_cross_feature.py::test_tier3_sync_command_handles_unreachable_hub_gracefully` | `test_tier4_real_world.py::test_tier4_fleet_telemetry_and_failure_distillation` |

---

## 5. Tier Breakdown

### Tier 1: Core Feature Coverage (Happy Path)
- **Scope**: Primary functionality of each feature under nominal inputs.
- **Test Modules**:
  - `tests/e2e/test_tier1_r1_protocol_cli.py` (26 tests)
  - `tests/e2e/test_tier1_r2_attention_memory.py` (26 tests)
  - `tests/e2e/test_tier1_r3_security_sandboxing.py` (22 tests)
  - `tests/e2e/test_tier1_r4_verification_acceptance.py` (26 tests)

### Tier 2: Boundary & Corner Cases (Limits & Extremes)
- **Scope**: Boundary values, limits, off-by-one, extreme payloads, malformed files, and adversarial encodings.
- **Test Modules**:
  - `tests/e2e/test_tier2_r1_boundaries.py` (25 tests)
  - `tests/e2e/test_tier2_r2_boundaries.py` (25 tests)
  - `tests/e2e/test_tier2_r3_boundaries.py` (21 tests)
  - `tests/e2e/test_tier2_r4_boundaries.py` (25 tests)

### Tier 3: Cross-Feature Combinations (State & Workflow Interactions)
- **Scope**: Multi-command pipelines, multi-store operations, export/import roundtrips, sync/pilot telemetry, holdout causal experiments, and doctor repair cycles.
- **Test Module**:
  - `tests/e2e/test_tier3_cross_feature.py` (12 tests)

### Tier 4: Real-World Application Scenarios (Operational Simulation)
- **Scope**: High-fidelity operational workflows simulating real-world agent pipelines and production workloads.
- **Test Module**:
  - `tests/e2e/test_tier4_real_world.py` (3 tests)
    1. Full 11-Phase SKILL.md Double-Review Lifecycle:
       - Phase 0 Alpha memory retrieval
       - Phase 3 Agent A implementation
       - Phase 5 Reviewer B gap discovery
       - Phase 7 Iteration loop to conformity
       - Phase 10 Omega synthesis and lesson proposal
       - Phase 11 Lambda automated validation and approval
       - Episode persistence and subsequent retrieval verification
    2. Long-Running Fleet Telemetry & Distillation:
       - Multi-worker trace clustering across repeated failure patterns (TLS timeouts, DB serialization, Redis socket disconnects)
       - Threshold-bounded distillation into review candidates
       - Taxonomy and reliability scoring
    3. Large Corpus Stress & Dosage Management:
       - 100+ lessons across 7 domains
       - Character and token budget enforcement (`dosage.select`)
       - Unconditional `core: true` rule prioritization
       - Redundancy deduplication gating
       - Benchmark health execution without OOM

---

## 6. Pass/Fail Criteria

All tests must execute cleanly with zero failures and zero unhandled exceptions:
- Pass Rate: **100%**
- Subprocess exit codes match specifications (0 for success, 1 for operational/schema errors, 2 for CLI syntax errors, 130 for SIGINT)
- No filesystem leakage outside test fixture boundaries
