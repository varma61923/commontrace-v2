# Project: CommonTrace v2 Hardening & Security Audit

## Architecture
CommonTrace v2 is structured into three primary architectural tiers:
1. **Protocol Core (`protocol/`)**:
   - Specification (`protocol/PROTOCOL.md`) defining the 7-stage lifecycle: Capture → Structure → Extract → Validate → Store → Inject → Measure.
   - Canonical JSON Schemas: `protocol/schemas/trace.schema.json` and `protocol/schemas/lesson.schema.json`.
2. **CLI & Command Dispatcher (`commontrace/`)**:
   - Entry point: `commontrace/cli.py` registering 10 core subcommands (`init`, `install`, `capture`, `trace`, `lesson`, `query`, `index`, `bench`, `sync`, `doctor`) plus extended subcommands.
   - Validation engine: `commontrace/validate.py` (JSON Schema enforcement) and `commontrace/commands/_validators.py` (CLI argument validation).
   - Command implementations: `commontrace/commands/*_cmd.py`.
   - Subprocess helper: `commontrace/commands/_shellout.py`.
   - Path resolution: `commontrace/paths.py`.
3. **Attention & Memory Subsystem (`commontrace/reference/` & `memory/`)**:
   - Semantic index generator: `commontrace/reference/build_index.py`.
   - Neural & lexical retrieval: `commontrace/reference/query.py` and `commontrace/lesson_cache.py`.
   - Memory health benchmark: `commontrace/reference/measure_performance.py` invoked via `commontrace bench`.
4. **Verification & Test Framework (`tests/`)**:
   - Pytest suite covering CLI commands, frontmatter parsing, benchmark metrics, hub client, and security mitigations.

---

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | R1-F1: CLI Subcommand Input Validation | Validate all arguments, types, ranges, and mutual exclusion across all 10 core subcommands | M1 | ORIGINAL_REQUEST §R1 |
| 2 | R1-F2: Standardized Exit Codes | Enforce standardized exit codes: 0 (success), 1 (operational/schema error), 2 (CLI syntax/arg error), 130 (SIGINT) | M1 | ORIGINAL_REQUEST §R1 |
| 3 | R1-F3: Pre-Write Schema Enforcement | Reject malformed traces and lessons before writing to disk with informative diagnostics | M1 | ORIGINAL_REQUEST §R1 |
| 4 | R1-F4: Positional Argument Flag Delimiting | Ensure `query_cmd.py` delimits task strings with `--` so leading dashes are not misparsed as flags | M1 | Spec Miner Survey |
| 5 | R1-F5: Repair `install.sh` Target Paths | Fix obsolete `memory/attention/build_index.py` path to `commontrace/reference/build_index.py` | M1 | Security Explorer Survey |
| 6 | R2-F1: Bounded-Memory Semantic Deduplication | Chunked similarity processing in `measure_performance.py` using `float32` to prevent $O(N^2)$ OOM crashes at scale | M2 | ORIGINAL_REQUEST §R2 |
| 7 | R2-F2: Incremental Embedding Indexing | SHA-256 content hashing in `build_index.py` to reuse cached vectors for unchanged lessons | M2 | ORIGINAL_REQUEST §R2 |
| 8 | R2-F3: Fast Query Metadata Co-location | Store `importances` and `statuses` inside `index.npz` to eliminate $O(N)$ frontmatter parsing on every query | M2 | Memory Explorer Survey |
| 9 | R2-F4: Inverted-Index Lexical Deduplication | Prune candidate pairs in `compute_lexical_duplicates` to eliminate $O(N^2)$ CPU bottlenecks | M2 | Memory Explorer Survey |
| 10 | R3-F1: Centralized Workspace Boundary Enforcement | Implement `enforce_boundary(base_dir, path)` in `commontrace/paths.py` to block path traversal outside workspace | M3 | ORIGINAL_REQUEST §R3 |
| 11 | R3-F2: Validation Path Sandboxing | Restrict `lesson validate` and `trace validate` from inspecting arbitrary files outside workspace | M3 | Security Explorer Survey |
| 12 | R3-F3: Safe Output Writing & Symlink Breaking | Prevent symlink write-through and directory traversal in `export`, `commons`, `overlap`, and `install` | M3 | Security Explorer Survey |
| 13 | R3-F4: Subprocess Execution Hardening | Enforce `PYTHONSAFEPATH="1"` in `_shellout.py` to prevent module hijacking via CWD standard library shadowing | M3 | Security Explorer Survey |
| 14 | R4-F1: Dedicated Schema Validation Test Suite | Author comprehensive unit tests in `tests/test_validate.py` covering types, bounds, lengths, enums | M4 | ORIGINAL_REQUEST §R4 |
| 15 | R4-F2: Security & Sandboxing Test Suite | Author negative tests in `tests/test_workspace_boundary.py` covering traversal, symlinks, and subprocess hardening | M4 | ORIGINAL_REQUEST §R4 |
| 16 | R4-F3: Performance & Scalability Test Suite | Author regression tests in `tests/test_memory_performance.py` for chunked similarity and incremental indexing | M4 | ORIGINAL_REQUEST §R4 |
| 17 | R4-F4: Diagnostic & Benchmark Health Verification | Verify `commontrace doctor` and `commontrace bench` execute cleanly with 100% test pass rate | M4 | ORIGINAL_REQUEST §Acceptance Criteria |
| 18 | R4-F5: Final E2E Test Suite Validation | Pass 100% of requirement-derived opaque-box E2E test cases across Tiers 1-4 | M5 | Project Pattern Track |
| 19 | R4-F6: Adversarial Coverage Hardening | White-box adversarial testing (Tier 5) with Challengers to harden edge cases | M5 | Project Pattern Track |

---

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Protocol & CLI Robustness | R1-F1, R1-F2, R1-F3, R1-F4, R1-F5 | none | DONE |
| M2 | Attention & Memory Performance | R2-F1, R2-F2, R2-F3, R2-F4 | none | DONE |
| M3 | Security & Sandboxing | R3-F1, R3-F2, R3-F3, R3-F4 | none | DONE |
| M4 | Test & Verification Coverage | R4-F1, R4-F2, R4-F3, R4-F4 | M1, M2, M3 | DONE |
| M5 | Final Acceptance & Adversarial Hardening | R4-F5, R4-F6 (Pass 100% E2E tests + Tier 5 Hardening) | M4, TEST_READY | DONE |

---

## Interface Contracts

### `commontrace.paths` ↔ CLI Commands (`commontrace.commands.*`)
```python
def enforce_boundary(base_dir: str, candidate_path: str, allow_within: bool = True) -> str:
    """
    Resolves candidate_path relative to base_dir, normalizes both paths (resolving symlinks),
    and strictly verifies that candidate_path resides within base_dir.
    Raises ValueError or PathTraversalError if candidate_path escapes base_dir.
    Returns the sanitized, canonical absolute path.
    """
```

### `commontrace.reference.measure_performance` (Chunked Semantic Duplicate Detection)
```python
def compute_semantic_duplicates(
    embeddings: np.ndarray,
    slugs: list[str],
    threshold: float = 0.85,
    chunk_size: int = 1000,
) -> tuple[int, list[tuple[str, str, float]]]:
    """
    Computes pairwise cosine similarities in float32 blocks without materializing
    the full N x N matrix or full coordinate arrays in memory.
    Guarantees memory usage is bounded to O(chunk_size * N) rather than O(N^2).
    """
```

### `commontrace.reference.build_index` (Incremental Caching)
```python
def build_or_update_index(
    lessons_dir: str,
    output_path: str,
    model_name: str = _TRUSTED_MODEL_NAME,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """
    Computes SHA-256 hash of lesson content. Reuses precomputed embeddings for unchanged
    hashes from output_path. Encodes only new/modified lessons.
    Saves embeddings, slugs, hashes, importances, and statuses into output_path (.npz).
    """
```

### `commontrace.commands._shellout` (Subprocess Execution)
```python
def run_script(
    root: str,
    script_relpath: str,
    args: list[str],
    missing_hint: str,
    capture: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """
    Executes Python script with sys.executable, shell=False, PYTHONUTF8=1, and PYTHONSAFEPATH=1.
    Prevents module hijacking from current working directory.
    """
```

---

## Code Layout
- Exclusive Write Ownership:
  - Milestone M1: `commontrace/cli.py`, `commontrace/commands/query_cmd.py`, `install.sh`
  - Milestone M2: `commontrace/reference/build_index.py`, `commontrace/reference/query.py`, `commontrace/reference/measure_performance.py`
  - Milestone M3: `commontrace/paths.py`, `commontrace/commands/_shellout.py`, `commontrace/commands/export_cmd.py`, `commontrace/commands/commons_cmd.py`, `commontrace/commands/overlap_cmd.py`, `commontrace/commands/install_cmd.py`, `commontrace/commands/lesson_cmd.py`, `commontrace/commands/trace_cmd.py`
  - Milestone M4: `tests/test_validate.py`, `tests/test_workspace_boundary.py`, `tests/test_memory_performance.py`, `tests/test_subprocess_security.py`
  - E2E Testing Track: `tests/e2e/`, `TEST_INFRA.md`, `TEST_READY.md`
