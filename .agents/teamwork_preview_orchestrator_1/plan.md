# Implementation Plan — Codebase Audit & Remediation

## Objectives
1. Bug Detection and Remediation across `commontrace/`, `memory/`, `protocol/`, `benchmark/`, `hub/`, `commons/`.
2. Performance Optimization (memoization, I/O, embedding & cosine similarity, CLI overhead).
3. Security Hardening (unsafe YAML loading, shell injections, path traversal, file permissions, input validation).
4. Dead and Unwired Code Cleanup (dead functions, unused imports, wiring incomplete features or isolating them).
5. Comprehensive Regression Testing & Independent Verification.

## Strategy & Phasing
- **Phase 0: Comprehensive Survey (Step 0)**
  - Spawn 3 parallel Explorers:
    - Explorer 1: Core CLI, attention & memory layer (`commontrace/`, `memory/`, attention/indexing/query)
    - Explorer 2: Protocol, schemas, hub, and commons (`protocol/`, `hub/`, `commons/`)
    - Explorer 3: Benchmark, tests, and security/dead code scan across whole repo (`benchmark/`, `tests/`, dependencies)
  - Synthesize reports into `PROJECT.md` Feature & Defect Inventory.
- **Phase 1: Milestone Planning & Decomposition**
  - Group findings into cohesive milestones by module/area.
  - Define regression test requirements.
- **Phase 2: Execution via Iteration Loops**
  - For each milestone: Explorer (fix design) → Worker (implementation + tests) → Reviewers + Challenger + Auditor (verification gate).
- **Phase 3: Final Verification & Audit**
  - Run full test suite, benchmarks, doctor, and forensic audit.
