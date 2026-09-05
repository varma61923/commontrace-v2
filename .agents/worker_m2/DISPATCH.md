# Worker M2 Dispatch: Hub, Commons, & Protocol Conformance

## Assigned Scope & Files Owned
You exclusively own and modify:
- `memory/lessons/lesson_template.md`
- `hub/outcomes.py`
- `hub/crud.py`
- `hub/abuse.py`
- `commons/eval/representations.py`
- `protocol/schemas/trace.schema.json`
- `tests/test_m2_protocol_hub_regressions.py` (dedicated new regression test file)

DO NOT modify files outside this set.

## Specific Tasks
1. `memory/lessons/lesson_template.md:8`:
   Fix schema violation: Change `importance_rationale: ""` to a non-empty string conforming to `minLength: 1`, such as:
   `importance_rationale: "Why this importance score is justified (1 concrete sentence, not generic)"`.
2. `hub/outcomes.py:213-236`:
   In `validate_outcome`, allow `None` for all nullable metrics (`resolved`, `escalated`, `repeated_error`, `frustration_signal`, `tokens_used`, `llm_calls`), matching `protocol/schemas/trace.schema.json:126-169`.
   Update logic:
   - For proportion metrics: `if field in outcome and outcome[field] is not None and not _is_bool(outcome[field]): raise ValueError(...)`
   - For mean metrics: `if value is not None and (not isinstance(value, int) or isinstance(value, bool)): raise ValueError(...)`
3. `hub/crud.py:800-804`:
   In `search_traces`, add `Trace.org_id == org_id` to the `UPDATE traces SET retrievals = Trace.retrievals + 1` WHERE clause:
   `.where(Trace.org_id == org_id, Trace.id.in_([t.id for t in traces]))`.
4. `hub/crud.py:1692-1700`:
   In `amend_trace`, include `"profile": original.profile` in the `wire` dict validated by `validate_trace`.
5. `protocol/schemas/trace.schema.json`:
   Declare optional properties `shared_with_commons` (boolean), `quarantined` (boolean), and `quarantine_reason` (string or null) under `properties` in `trace.schema.json` so wire projection does not drift from schema.
6. `commons/eval/representations.py:65`:
   Replace ASCII-only `_WORD = re.compile(r"[a-z0-9]+")` with `_WORD = re.compile(r"\w+", re.UNICODE)`.
7. `hub/abuse.py:510-526`:
   Ensure the background thread in `_SharedPgPool` is cleanly stopped or signaled to terminate if `ready.wait()` times out.
8. Create dedicated regression tests in `tests/test_m2_protocol_hub_regressions.py` testing:
   - `validate_outcome` with explicit `None` for nullable fields (`resolved`, `escalated`, `tokens_used`, etc.).
   - `memory/lessons/lesson_template.md` validates cleanly against `protocol/schemas/lesson.schema.json`.
   - `search_traces` SQL query inspection / mock confirming `Trace.org_id == org_id` is present in the update statement.
   - `amend_trace` wire dictionary retains `profile`.
   - `trace.schema.json` validates traces with `shared_with_commons`, `quarantined`, and `quarantine_reason`.
9. Run `pytest tests/ -v` and verify all tests pass with 0 failures or regressions.

## 2026-09-05T15:10:20Z
**Context**: Milestone 2 Progress & Liveness Check
**Content**: Orchestrator liveness check: your last progress update was at 14:48:00Z (>20 minutes ago). Please report your current status, which task you are on, and update your `progress.md`.
**Action**: Report status and update progress.md.

## 2026-09-05T15:14:33Z
**Context**: Permission Granted for Schema Mirroring
**Content**: Yes, please copy `protocol/schemas/trace.schema.json` to `commontrace/schemas/trace.schema.json`. Your write ownership is officially expanded to include `commontrace/schemas/trace.schema.json` to satisfy the strict byte-for-byte schema sync contract asserted by `test_schema_sync.py`.
**Action**: Synchronize `commontrace/schemas/trace.schema.json` with `protocol/schemas/trace.schema.json`, re-run the test suite, and deliver your handoff report.

