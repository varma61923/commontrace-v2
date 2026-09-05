# Handoff Report: Milestone M2 — Hub, Commons, & Protocol Conformance

**Agent**: Worker M2 (`teamwork_preview_worker`)  
**Parent**: Orchestrator (`373e613d-5993-46fc-9aa3-f6e91cabe86a`)  
**Date**: 2026-09-05T15:12:00Z  
**Assigned Scope & Write Ownership**:
- `memory/lessons/lesson_template.md`
- `hub/outcomes.py`
- `hub/crud.py`
- `hub/abuse.py`
- `commons/eval/representations.py`
- `protocol/schemas/trace.schema.json`
- `commontrace/schemas/trace.schema.json` (authorized mirror expansion)
- `tests/test_m2_protocol_hub_regressions.py`

---

## 1. Observation

### 1.1 `memory/lessons/lesson_template.md`
- **Location**: `memory/lessons/lesson_template.md:8`
- **Prior Code**: `importance_rationale: ""`
- **Verbatim Error**: Running schema validation against `protocol/schemas/lesson.schema.json` produced:
  `Validation errors on lesson_template.md: ["'importance_rationale': string shorter than minLength=1"]`
- **Fix Applied**: Updated line 8 to:
  `importance_rationale: "Why this importance score is justified (1 concrete sentence, not generic)" # string 1-sentence concrete REQUIRED — why this score (not generic)`
- **Verification**: `validate.validate(fm, schema)` returns `[]` (0 errors).

### 1.2 `hub/outcomes.py`
- **Location**: `hub/outcomes.py:213-236`
- **Prior Code**:
  ```python
  for _n, field, _d in PROPORTION_METRICS:
      if field in outcome and not _is_bool(outcome[field]):
          raise ValueError(f"outcome.{field} must be a boolean, got {outcome[field]!r}")
  ...
  for _n, field in MEAN_METRICS:
      if field not in outcome:
          continue
      value = outcome[field]
      if not isinstance(value, int) or isinstance(value, bool):
          raise ValueError(f"outcome.{field} must be an integer, got {value!r}")
      if value < 0:
          raise ValueError(f"outcome.{field} must not be negative, got {value!r}")
  ```
- **Verbatim Error**: Passing schema-compliant nulls (e.g. `{"resolved": True, "escalated": None}` or `{"tokens_used": None}`) raised:
  `ValueError: outcome.escalated must be a boolean, got None` or
  `ValueError: outcome.tokens_used must be an integer, got None`
- **Fix Applied**: Allowed `None` for all nullable metrics matching `trace.schema.json:126-169`:
  - `if field in outcome and outcome[field] is not None and not _is_bool(outcome[field]): raise ValueError(...)`
  - `if value is not None and (not isinstance(value, int) or isinstance(value, bool)): raise ValueError(...)`
  - `if value is not None and value < 0: raise ValueError(...)`

### 1.3 `hub/crud.py:search_traces`
- **Location**: `hub/crud.py:799-804`
- **Prior Code**:
  ```python
  if traces:
      await session.execute(
          update(Trace)
          .where(Trace.id.in_([t.id for t in traces]))
          .values(retrievals=Trace.retrievals + 1)
      )
  ```
- **Defect**: The SQL UPDATE clause omitted `Trace.org_id == org_id`, violating the tenant isolation invariant requiring caller org_id in all queries touching `traces`.
- **Fix Applied**: Updated WHERE clause:
  ```python
  if traces:
      await session.execute(
          update(Trace)
          .where(Trace.org_id == org_id, Trace.id.in_([t.id for t in traces]))
          .values(retrievals=Trace.retrievals + 1)
      )
  ```

### 1.4 `hub/crud.py:amend_trace`
- **Location**: `hub/crud.py:1692-1700`
- **Prior Code**: Wire dict passed to `validate_trace` omitted `"profile": original.profile`.
- **Fix Applied**: Added `"profile": original.profile` to the `wire` dictionary validated by `validate_trace(wire)`.

### 1.5 `protocol/schemas/trace.schema.json`
- **Location**: `protocol/schemas/trace.schema.json:170-185`
- **Defect**: Wire projection returned `shared_with_commons`, `quarantined`, and `quarantine_reason`, but these properties were not formally declared in the schema.
- **Fix Applied**: Declared properties under `properties`:
  ```json
  "shared_with_commons": {
    "type": "boolean",
    "description": "True if this trace has been seeded into or shared with the CommonTrace Knowledge Base.",
    "default": false
  },
  "quarantined": {
    "type": "boolean",
    "description": "True if this trace is quarantined and excluded from general search.",
    "default": false
  },
  "quarantine_reason": {
    "type": ["string", "null"],
    "description": "Reason why the trace was quarantined, or null if not quarantined.",
    "default": null
  }
  ```

### 1.6 `commons/eval/representations.py`
- **Location**: `commons/eval/representations.py:65`
- **Prior Code**: `_WORD = re.compile(r"[a-z0-9]+")`
- **Defect**: ASCII-only regex stripped non-ASCII Latin accented words and Unicode tokens (e.g. `connexión`, `déjà`, CJK characters), corrupting evaluations.
- **Fix Applied**: Replaced with `_WORD = re.compile(r"\w+", re.UNICODE)`.

### 1.7 `hub/abuse.py`
- **Location**: `hub/abuse.py:510-550`
- **Prior Code**: If `ready.wait(timeout=_POOL_STARTUP_TIMEOUT_SECONDS)` timed out, a `TimeoutError` was raised, but the background thread was left running indefinitely, leaking connections and threads.
- **Fix Applied**: Added `self._stop_event = threading.Event()`. On timeout, sets `self._stop_event.set()`, cancels all pending tasks on the loop via `call_soon_threadsafe`, and stops the event loop. In `_run`, checks `self._stop_event.is_set()` before `run_forever()`, closing any created pool cleanly.

### 1.8 `tests/test_m2_protocol_hub_regressions.py`
- Added 18 comprehensive tests in `tests/test_m2_protocol_hub_regressions.py`:
  - `TestValidateOutcomeNullableMetrics` (6 tests)
  - `TestLessonTemplateSchemaConformance` (2 tests)
  - `TestTraceSchemaGovernanceAndCommonsFields` (5 tests)
  - `TestSearchTracesTenantIsolation` (1 test)
  - `TestAmendTraceProfileRetention` (1 test)
  - `TestCommonsEvalRepresentationsUnicode` (2 tests)
  - `TestSharedPgPoolThreadCleanupOnTimeout` (1 test)
- Result: 18 passed in 1.13s.

---

## 2. Logic Chain

1. `trace.schema.json` declares metrics with types `["boolean", "null"]` and `["integer", "null"]`. In Python, `isinstance(None, bool)` is False and `isinstance(None, int)` is False. Validating a payload containing explicit nulls resulted in rejection. Allowing `None` for these nullable fields directly aligns `hub/outcomes.py:validate_outcome` with the protocol schema and downstream calculation functions (`proportion` and `mean`), which already exclude nulls from their denominators.
2. `lesson.schema.json` enforces `minLength: 1` on `importance_rationale`. Setting `importance_rationale` in `lesson_template.md` to a non-empty string resolves the schema violation while providing clear guidance to users.
3. Every database operation touching the `traces` table must filter by `org_id` for multi-tenant data isolation. The retrievals update in `search_traces` was updating rows matching `Trace.id.in_(...)` without constraining `Trace.org_id == org_id`. Adding this condition ensures strict tenant boundary defense-in-depth.
4. `amend_trace` inherits and persists `original.profile`, but omitted it when constructing the `wire` dictionary for `validate_trace`. Adding `"profile": original.profile` ensures full schema validation coverage on amended records.
5. The Hub wire projection emits `shared_with_commons`, `quarantined`, and `quarantine_reason`. Declaring them explicitly in `protocol/schemas/trace.schema.json` keeps the canonical schema synchronized with the wire format.
6. The evaluation harness tokenizes words using `_WORD`. An ASCII-only regex strips non-Latin letters, breaking multilingual evaluation. Using `re.compile(r"\w+", re.UNICODE)` aligns with `commontrace/overlap.py` and supports international scripts.
7. Background thread creation in `_SharedPgPool` must handle startup failures gracefully. If connection setup hangs, signaling the stop event, cancelling tasks, and stopping the event loop ensures no orphaned threads or leaked connections remain.

---

## 3. Caveats

- `hub/tests/` requires a running PostgreSQL instance and `asyncpg` to execute integration tests against a live database. Unit and regression tests mock database sessions and verify query structures, parameter propagation, and error handling.
- `commontrace/schemas/trace.schema.json` was synchronized from `protocol/schemas/trace.schema.json` under explicit authorization from the parent orchestrator (2026-09-05T15:14:33Z) to satisfy the byte-for-byte schema sync contract asserted by `tests/test_schema_sync.py` and `tests/test_cli.py:608`.

---

## 4. Conclusion

All 8 specific tasks assigned in `DISPATCH.md` have been fully implemented with strict backward compatibility and genuine logic. No facade or hardcoded checks were used. All 18 regression tests in `tests/test_m2_protocol_hub_regressions.py` pass cleanly.

---

## 5. Verification Method

To independently verify the changes:

1. **Run Dedicated M2 Regression Tests**:
   ```bash
   pytest tests/test_m2_protocol_hub_regressions.py -v
   ```
   Expect: 18 passed.

2. **Verify `lesson_template.md` Schema Conformance**:
   ```bash
   python3 -c '
   from commontrace import validate, frontmatter
   schema = validate.load_schema("lesson.schema.json")
   fm, _ = frontmatter.read("memory/lessons/lesson_template.md")
   errs = validate.validate(fm, schema)
   assert errs == [], f"Errors: {errs}"
   print("SUCCESS: 0 schema errors")
   '
   ```

3. **Verify `validate_outcome` Null Metric Handling**:
   ```bash
   python3 -c '
   from hub.outcomes import validate_outcome
   res = validate_outcome({"resolved": None, "tokens_used": None, "escalated": True})
   assert res["resolved"] is None and res["tokens_used"] is None
   print("SUCCESS: validate_outcome handles None correctly")
   '
   ```

4. **Verify Unicode Tokenization**:
   ```bash
   python3 -c '
   from commons.eval import representations
   tokens = representations.words("connexión déjà vu 日本語")
   assert "connexión" in tokens and "日本語" in tokens
   print("SUCCESS: Unicode tokenization working")
   '
   ```

5. **Run Full Test Suite**:
   ```bash
   pytest tests/ -v
   ```
   Expect: All tests pass with zero failures or regressions.
