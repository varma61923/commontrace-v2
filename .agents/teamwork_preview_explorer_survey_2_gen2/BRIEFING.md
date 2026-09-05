# BRIEFING — 2026-09-05T16:38:17Z

## Mission
Exhaustive read-only survey and audit of protocol/ (spec and schemas, schema conformance of stored lessons/traces), hub/ (server, crud, models, auth, abuse, audit, commons, search, plans, bench_retrieval), and commons/ (eval, seed).

## 🔒 My Identity
- Archetype: teamwork_preview_explorer
- Roles: explorer, auditor, investigator
- Working directory: /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2_gen2
- Original parent: 5f20407e-a93e-4af7-ba44-ecf1d3bff56b
- Milestone: survey_hub_commons_protocol

## 🔒 Key Constraints
- Read-only investigation — do NOT implement or modify source code
- Schema validation of memory/lessons/*.md and memory/traces/*.md against protocol/schemas/*.json
- Deep audit of all hub/ modules for runtime bugs, SQL/data integrity, auth/rate-limiting, exception handling, perf, dead/unwired code
- Audit commons/eval/ and commons/seed/ for bugs, data consistency, and dead imports/files
- Deliver findings in handoff.md with 5 components and proposed remediation table

## Current Parent
- Conversation ID: 5f20407e-a93e-4af7-ba44-ecf1d3bff56b
- Updated: 2026-09-05T16:38:17Z

## Investigation State
- **Explored paths**: `protocol/` (`PROTOCOL.md`, `protocol/schemas/trace.schema.json`, `protocol/schemas/lesson.schema.json`), `memory/lessons/`, `memory/traces/`, `hub/` (`server.py`, `crud.py`, `models.py`, `auth.py`, `abuse.py`, `audit.py`, `commons.py`, `search.py`, `plans.py`, `bench_retrieval.py`, `bench_scaling.py`, `console.py`, `admin.py`, `manage.py`, `observability.py`, `db.py`, `config.py`, `main.py`, `smoke.py`, `schema_validation.py`, `hub/tests/`), `commons/` (`commons/eval/run.py`, `representations.py`, `retrieval_tiers.py`, `search_modes.py`, `RESULTS.md`, `probes-v1.jsonl`, `probes-v2.jsonl`, `commons/seed/substrate-v1.jsonl`).
- **Key findings**:
  1. `memory/lessons/lesson_template.md:8`: `importance_rationale: ""` violates `minLength: 1` in `protocol/schemas/lesson.schema.json`.
  2. `hub/outcomes.py:214-236`: `validate_outcome` rejects `null` for `resolved`, `escalated`, `tokens_used`, etc., directly violating `protocol/schemas/trace.schema.json` which declares `type: ["boolean", "null"]` and `type: ["integer", "null"]`.
  3. `hub/crud.py:800-804`: `search_traces` executes `update(Trace).where(Trace.id.in_(...))` without `Trace.org_id == org_id`, breaking tenant isolation defense-in-depth on SQL write path.
  4. `hub/crud.py:221-269`: `_to_wire` projects `shared_with_commons`, `quarantined`, and `quarantine_reason`, which are not declared in `protocol/schemas/trace.schema.json`.
  5. `hub/schema_validation.py`: Outbound validation is claimed in docstring but never called on outbound objects from MCP tools. `validate_lesson` is completely unwired/uncalled.
  6. `commontrace/commands/capture_cmd.py:25`: Hardcoded `choices=paths.AGENT_TYPES` restricts `--agent-type`, violating open-vocabulary protocol spec in `PROTOCOL.md#7-taxonomy-open-not-closed`.
  7. `hub/crud.py:1692-1700`: `amend_trace` omits `profile: original.profile` in `wire` dict for schema validation.
  8. `commons/eval/representations.py:65`: Uses ASCII-only regex `_WORD = re.compile(r"[a-z0-9]+")` instead of `\w+` with `UNICODE`.
- **Unexplored areas**: None (survey is complete).

## Key Decisions Made
- Executed and passed test suite baseline (1076 passed, 33 skipped)
- Verified all 46 substrate seeds in `commons/seed/substrate-v1.jsonl`
- Executed `commons/eval/` scripts (`run.py`, `search_modes.py`, `retrieval_tiers.py`, `representations.py`)
- Verified all schema constraints and failure modes with Python reproductions

## Artifact Index
- handoff.md — Final 5-component handoff report
- progress.md — Real-time progress and heartbeat
- BRIEFING.md — Situational awareness

