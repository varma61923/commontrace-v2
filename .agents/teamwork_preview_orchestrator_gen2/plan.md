# Implementation Plan — Codebase Audit & Remediation (Gen 2)

## Objectives
1. Bug Detection & Remediation across `commontrace/`, `memory/`, `protocol/`, `benchmark/`, `hub/`, `commons/`.
2. Performance Optimization (memoization, I/O elimination, embedding/cosine queries, process overhead).
3. Security Hardening (safe YAML loading, shell injections, path traversal, permissions, input validation).
4. Dead and Unwired Code Cleanup (dead functions, unused imports, wiring incomplete features or isolating them).
5. Comprehensive Regression Testing & Independent Verification (pytest zero failures, dedicated tests for each fix, bench, doctor, schema conformance).

## Phasing & Steps

### Step 0: Survey Completion
- Dispatch Explorer 2 (`teamwork_preview_explorer_survey_2_gen2`) to complete audit of `hub/`, `commons/`, and `protocol/` (including schema conformance of stored lessons/traces).
- Await handoff.md from Explorer 2.

### Step 1: Synthesis & Decomposition
- Synthesize all findings from Explorers 1, 2, and 3 into `PROJECT.md`.
- Formulate comprehensive Feature & Defect Inventory.
- Decompose into cohesive milestones:
  - Milestone 1 (M1): Core CLI, Evidence/Trace I/O, and Memory/Attention Remediation & Optimization
  - Milestone 2 (M2): Benchmark Integrity, Performance Metrics, and Test Suite Expansion
  - Milestone 3 (M3): Hub, Commons, Protocol Schema Conformance, and Dead Code Cleanup
  - Milestone 4 (M4): Final Verification & Whole-System Forensic Audit

### Step 2: Milestone Iteration Loops
For each milestone:
1. Dispatch Explorer(s) to design concrete fix strategy and test requirements.
2. Dispatch Worker (with mandatory integrity warning and file ownership) to implement code fixes and regression tests.
3. Dispatch 2 Reviewers independently to verify correctness, test coverage, and regressions.
4. Dispatch 2 Challengers to empirically test edge cases and verify performance/correctness.
5. Dispatch Forensic Auditor (`teamwork_preview_auditor`) to verify implementation authenticity.
6. Evaluate Gate in `GATE_STATUS.md`. All criteria must pass.

### Step 3: Final System-Wide Verification
- Run full pytest suite across entire repository.
- Verify dedicated regression tests for every bug.
- Execute `commontrace bench` and verify performance metrics.
- Run `commontrace doctor`.
- Validate all traces and lessons against JSON schemas.
- Final forensic audit and sign-off report to user.
