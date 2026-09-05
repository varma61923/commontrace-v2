# Dispatch Record — Orchestrator Gen 3

## 2026-09-05T14:37:04Z

You are the Project Orchestrator (teamwork_preview_orchestrator), successor/generation 3.
Your working directory is: /root/commontrace-v2/.agents/teamwork_preview_orchestrator_gen3
The workspace directory is: /root/commontrace-v2
The authoritative user request is in: /root/commontrace-v2/.agents/ORIGINAL_REQUEST.md

CRITICAL CONTEXT — PHASE 0 SURVEY IS 100% COMPLETE:
All 3 survey explorers have already finished their investigations and delivered exhaustive handoff reports with exact file locations, code snippets, defect explanations, and proposed fixes:
1. /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/handoff.md — Covers `commontrace/` CLI subcommands, core IO, retrieval, and `memory/attention/`: 8 functional bugs, 6 performance bottlenecks, 4 unwired code items, clean security audit.
2. /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2_gen2/handoff.md — Covers `protocol/` schemas, `hub/`, and `commons/`: schema violation in `lesson_template.md` (empty `importance_rationale`), `hub/outcomes.py:validate_outcome` rejecting `None` on nullable fields (`resolved`, `escalated`, `tokens_used`) violating `trace.schema.json`, `hub/crud.py` emitting undeclared non-schema fields (`shared_with_commons`, `quarantined`) on wire, missing tenant boundary check on bulk trace update, and `amend_trace` wire validation omissions.
3. /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_3/handoff.md — Covers `benchmark/`, `tests/`, security hardening, dead/unwired symbols, and static analysis results.

Your immediate mission:
1. Do NOT re-run Phase 0 surveys. Read the 3 handoff files and synthesize all findings into `PROJECT.md` in your working directory.
2. Decompose all defects into execution milestones (e.g., Milestone 1: Core CLI & Memory/Attention; Milestone 2: Hub, Commons, & Protocol Conformance; Milestone 3: Benchmark, Security & Dead Code Cleanup; Milestone 4: Verification & Forensic Audit).
3. Dispatch implementation workers (teamwork_preview_implementer or specialist workers) to remediate the defects, with dedicated regression tests for every fix.
4. Run adversarial reviewers and forensic auditors to confirm zero regressions (`pytest tests/ -v`, `commontrace bench`, `commontrace doctor`, schemas valid).
5. Report completion when all gates pass with verified evidence.

Maintain `plan.md`, `progress.md`, and `BRIEFING.md` in your working directory.

## 2026-09-05T15:05:53Z

Liveness probe from Sentinel: Please confirm status and continue monitoring workers M1 and M2.
