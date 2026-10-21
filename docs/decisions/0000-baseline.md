# 0000. Frontier Causal Governance & Physical AI Baseline

- **Date:** 2026-10-19
- **Branch:** `feat/frontier-causal-governance`
- **Status:** Accepted / Verified

## Context & Objectives

UniMem / UniMem transition from an observational memory measurement tool into a governed, causal memory system with verifiable guarantees, policies, pricing, and physical AI safety controls. The implementation encompasses Wave 1 (F1–F22) and Wave 2 (G1–G30), providing complete coverage across software agents, multi-agent fleet swarms, and robotics.

## Verification Baseline Metrics

1. **Test Suite Execution:**
   - **Total Tests Collected:** 8,806 tests across unit, integration, and end-to-end suites.
   - **Passed:** 8,669
   - **Skipped:** 137 (environment-specific hardware/GPU/vendor-optional tests)
   - **Failed:** 0 (100% pass rate)

2. **Test Coverage Gate:**
   - **Measured Coverage:** 85.62% line coverage across the entire `unimem` codebase.
   - **Gate Requirement:** $\ge 80.00\%$ (`--cov-fail-under=80`). Status: **GREEN**.
   - **Branch Coverage Ratchet:** `unimem.fact_index` achieves 95.84% branch-inclusive coverage ($\ge 95\%$ ratchet).

3. **Static Analysis & Type Integrity:**
   - **Strict Type Checking (mypy):** 118 pure-logic modules listed in CI security scan pass under strict typing flags (`--strict --follow-imports=silent --ignore-missing-imports`) with 0 errors.
   - **Security Static Analysis (Bandit):** 0 Medium / 0 High severity issues across 132,528 lines of code (`bandit -r unimem/ hub/ -x "*/tests/*,*/test_*" -ll`).
   - **Code Linting:** Ruff line-length $\le 120$ targeting Python 3.10+ passes with zero violations (`make lint`).

4. **Architectural & Boundary Audits:**
   - **Graphify Module Budget:** Exactly 398 `.py` source modules under `unimem/`, conforming to the `--max-modules 398` boundary in `.github/workflows/graphify.yml`.
   - **Docker Hub Allowlist:** `hub/tests/test_image_contents.py` passes 11/11 tests, ensuring all lazily-loaded modules in Hub are explicitly allowlisted in `.dockerignore`.
   - **OpenAPI Schema Parity:** `scripts/export_openapi.py --check` and `scripts/export_hub_openapi.py --check` match the checked-in API contracts.

5. **Performance & Latency Gates:**
   - **Local Retrieval Latency:** Sublinear OLS slope ($\alpha = 0.96 \le 1.5$) with 6,400 lessons loaded/ranked in 30.2 ms ($\ll 500$ ms budget).
   - **50,000 Fact Bank Search:** 3,664x speedup over baseline with 0 warm snapshot re-loads and memory bounded below 64 MiB.
   - **Typed Graph Relations:** 257x speedup on 20,000 edges with exact semantic equivalence.
   - **Session Summary:** 35x speedup on 1,000-turn rolling history.
   - **Physical AI / Robot Memory:** Median retrieval latency 1.23 ms, safety shield with audit 19.6 ms (wall-clock budget respected).
