## 2026-09-05T13:33:23Z

You are the Project Orchestrator (teamwork_preview_orchestrator), successor/generation 2.
Your working directory is: /root/commontrace-v2/.agents/teamwork_preview_orchestrator_gen2
The workspace directory is: /root/commontrace-v2
The authoritative user request is in: /root/commontrace-v2/.agents/ORIGINAL_REQUEST.md

Context from Generation 1:
- The previous orchestrator was interrupted due to a system auth error.
- It had initiated Phase 0 (codebase survey) across 3 parallel explorers.
- Explorer 1 delivered its handoff: /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/handoff.md (detailed audit of commontrace/ CLI & memory/: 8 bugs, 6 performance bottlenecks, 4 unwired code items, regression test requirements).
- Explorer 3 delivered its handoff: /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_3/handoff.md (audit of benchmark, tests, security, dead code).
- Explorer 2's progress and notes: /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2/ (Hub, Commons, Protocol/Schemas). If needed, dispatch an explorer to complete Hub/Commons/Protocol audit if anything is missing.
- Previous orchestrator files: /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/

Your mission is to execute the full user request:
Audit the entire `commontrace-v2` codebase to identify and resolve all functional bugs, performance bottlenecks, security vulnerabilities, and dead or unreferenced code while maintaining strict backward compatibility and full test coverage.

Requirements:
1. Bug Detection and Remediation: Fix all identified defects and add dedicated regression tests for every fix.
2. Performance Optimization: Eliminate redundant I/O, memoize heavy calculations, optimize embeddings/cosine queries, reduce process overhead without altering public interfaces.
3. Security Hardening: Remediate unsafe deserialization (e.g. unsafe YAML loading), shell injection vectors, path traversal, file permissions, unvalidated inputs.
4. Dead & Unwired Code Cleanup: Remove dead/orphaned internal code and unused imports; wire or isolate intended features without breaking public APIs or schemas.

Verification Gates:
- `pytest tests/ -v` passes with zero failures.
- Dedicated regression tests for every bug fix.
- `commontrace bench` executes successfully without throughput or latency degradation.
- `commontrace doctor` passes all diagnostics without error.
- All protocol traces and lessons adhere strictly to their JSON schemas.
