# Dispatch Assignment — Survey Explorer 2 (Hub, Commons, Protocol & Schemas)

## 2026-09-05T16:38:17Z

**Target Directory**: `/root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2_gen2`
**Original Request**: `/root/commontrace-v2/.agents/ORIGINAL_REQUEST.md`

## Mission
Perform a comprehensive technical survey and audit of:
1. `protocol/`:
   - Protocol specification (`protocol/PROTOCOL.md`) and JSON Schemas (`protocol/schemas/trace.schema.json`, `protocol/schemas/lesson.schema.json`).
   - Validate all stored lessons (`memory/lessons/*.md`) and traces (`memory/traces/*.md`) against their respective JSON schemas using `jsonschema` (or Python schema validators). Report any schema conformance violations or inconsistencies.
2. `hub/`:
   - Inspect all modules (`hub/server.py`, `hub/crud.py`, `hub/models.py`, `hub/auth.py`, `hub/abuse.py`, `hub/audit.py`, `hub/commons.py`, `hub/search.py`, `hub/plans.py`, `hub/bench_retrieval.py`, etc.).
   - Audit for runtime errors, SQL/data integrity issues, authentication/authorization flaws, rate limiting, exception handling, performance bottlenecks, and dead/unwired code.
3. `commons/`:
   - Inspect `commons/eval/` and `commons/seed/` modules.
   - Determine which code is dead vs. intended features vs. research harnesses.
   - Audit for bugs, data consistency, and import cleanliness.

## Deliverable
Write a comprehensive handoff report to:
`/root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2_gen2/handoff.md`
following the standard format:
1. Observation (Bugs & Edge Cases, Performance Bottlenecks, Security Vulnerabilities, Dead & Unwired Code, Schema Conformance).
2. Logic Chain (deductions from observations to concrete failure modes).
3. Caveats.
4. Conclusion & Proposed Remediation Table (with file, line numbers, severity, and remediation strategy).
5. Verification Method (exact commands to demonstrate each finding).

## 2026-09-05T13:50:01Z
From: 5f20407e-a93e-4af7-ba44-ecf1d3bff56b (parent)
**Context**: Hub, Commons, Protocol/Schemas survey
**Content**: Status check: Please update your progress.md and report your current status on auditing protocol schemas, hub, and commons.
**Action**: Update progress.md with your latest findings and send a status update.

## 2026-09-05T14:09:58Z
From: 5f20407e-a93e-4af7-ba44-ecf1d3bff56b (parent)
**Context**: Survey Step 0 (Hub, Commons, Protocol/Schemas)
**Content**: Liveness check: Please update your progress.md with current milestone status and report if you are preparing handoff.md.
**Action**: Update progress.md and reply with status.

## 2026-09-05T14:29:12Z
From: 5f20407e-a93e-4af7-ba44-ecf1d3bff56b (parent)
**Context**: Survey Step 0 (Hub, Commons, Protocol)
**Content**: Status inquiry: Are you ready to output handoff.md? Please update progress.md and deliver your handoff report.
**Action**: Write handoff.md to /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2_gen2/handoff.md and report completion.



