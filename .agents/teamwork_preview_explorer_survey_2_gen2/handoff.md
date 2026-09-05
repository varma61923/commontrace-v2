# Technical Survey & Audit Report: Hub, Commons, Protocol & Schemas

**Agent**: Survey Explorer 2 — Generation 2 (`teamwork_preview_explorer_survey_2_gen2`)  
**Parent**: Orchestrator (`5f20407e-a93e-4af7-ba44-ecf1d3bff56b`)  
**Date**: 2026-09-05T14:35:00Z  
**Scope**: 
1. `protocol/` (`protocol/PROTOCOL.md`, `protocol/schemas/trace.schema.json`, `protocol/schemas/lesson.schema.json`, validation of `memory/lessons/*.md` and `memory/traces/*.md`).
2. `hub/` (`hub/server.py`, `hub/crud.py`, `hub/models.py`, `hub/auth.py`, `hub/abuse.py`, `hub/audit.py`, `hub/commons.py`, `hub/search.py`, `hub/plans.py`, `hub/bench_retrieval.py`, `hub/bench_scaling.py`, `hub/console.py`, `hub/admin.py`, `hub/manage.py`, `hub/observability.py`, `hub/db.py`, `hub/config.py`, `hub/main.py`, `hub/smoke.py`, `hub/schema_validation.py`).
3. `commons/` (`commons/eval/` evaluation scripts, `commons/seed/substrate-v1.jsonl`).

---

## 1. Observation

### 1.1 Protocol Specification & JSON Schema Conformance

1. **`memory/lessons/lesson_template.md:8` — Schema Violation on Empty String `importance_rationale`**
   - **Location**: `/root/commontrace-v2/memory/lessons/lesson_template.md:8`
   - **Code**:
     ```yaml
     importance_rationale: "" # string 1-sentence concrete REQUIRED — why this score (not generic)
     ```
   - **Schema Requirement**: `protocol/schemas/lesson.schema.json:44-48`:
     ```json
     "importance_rationale": {
       "type": "string",
       "description": "One concrete sentence justifying the score. Must not be generic.",
       "minLength": 1
     }
     ```
   - **Observation**: Validating `memory/lessons/lesson_template.md` against `lesson.schema.json` fails with `'importance_rationale': string shorter than minLength=1`. In contrast, `commontrace/templates.py:33` correctly generates `"importance_rationale": importance_rationale or "needs calibration"`.
   - **Contrast with Active Lessons**: `lesson_example_regression_test_before_refactor.md` and `lesson_example_serialize_subagents_same_file.md` have 0 schema errors and conform strictly.

2. **`commontrace/commands/capture_cmd.py:25` — CLI Enforces Hardcoded Enum Choices on Open Protocol Vocabulary**
   - **Location**: `/root/commontrace-v2/commontrace/commands/capture_cmd.py:24-27`
   - **Code**:
     ```python
     p.add_argument(
         "--agent-type", choices=paths.AGENT_TYPES, default=None,
         help="Defaults to the agent_type this store was initialized with.",
     )
     ```
   - **Observation**: `protocol/PROTOCOL.md §7` explicitly mandates:
     > "`domain` and `tags` are open strings at the protocol level — no fixed enum is validated. `agent_type`: open vocabulary; 'code' is the reference implementation shipped in this repo. examples: ['code', 'support', 'sales', 'hr', 'marketing', 'ops', 'custom']".
     `protocol/schemas/trace.schema.json:35-39` uses `"examples"`, not `"enum"`. By enforcing `choices=paths.AGENT_TYPES`, `commontrace capture` rejects valid custom agent types (such as `--agent-type finance` or `--agent-type security`) at the CLI level with `argparse` errors, contradicting the protocol specification.

3. **`hub/crud.py:221-269` (`_to_wire`) — Undeclared Fields in Wire Projection vs. `trace.schema.json`**
   - **Location**: `/root/commontrace-v2/hub/crud.py:252, 265-266`
   - **Code**:
     ```python
     "shared_with_commons": trace.shared_with_commons,
     "quarantined": trace.quarantined,
     "quarantine_reason": trace.quarantine_reason,
     ```
   - **Observation**: `hub/crud.py:11-12` states:
     > "Every one of these functions returns wire-shaped Trace dicts (matching protocol/schemas/trace.schema.json) or None/[]".
     However, `shared_with_commons`, `quarantined`, and `quarantine_reason` are absent from `protocol/schemas/trace.schema.json`. Similarly, `_to_commons_wire` (`hub/crud.py:352-353`) projects `vote_count` and `standing` which are not defined in `trace.schema.json`. While `trace.schema.json` does not set `additionalProperties: false` at root, this is an undocumented drift between the canonical schema and the Hub wire object.

4. **`hub/schema_validation.py:1-13` — Unimplemented Outbound Schema Validation**
   - **Location**: `/root/commontrace-v2/hub/schema_validation.py:3-5`
   - **Code**:
     ```python
     """Per the brief this server implements: "Validate every inbound and outbound
     object against protocol/schemas/trace.schema.json and lesson.schema.json
     loaded from disk at runtime."
     ```
   - **Observation**: Grep search reveals `validate_trace()` is only called in 3 write paths (`crud.py:1025` in `contribute_trace`, `crud.py:1700` in `amend_trace`, and `crud.py:2631` in `submit_kb_entry`). Outbound responses from `search_traces`, `get_trace`, and `vote_trace` are never validated against `trace.schema.json`, leaving the documented outbound guarantee unimplemented.

---

### 1.2 Functional Bugs & Edge Cases (R1)

5. **`hub/outcomes.py:213-236` — `validate_outcome` Rejects Schema-Valid `null` Values with `ValueError`**
   - **Location**: `/root/commontrace-v2/hub/outcomes.py:213-236`
   - **Code**:
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
     ```
   - **Schema Definition**: `protocol/schemas/trace.schema.json:126-169`:
     ```json
     "resolved": { "type": ["boolean", "null"], "default": null },
     "escalated": { "type": ["boolean", "null"], "default": null },
     "repeated_error": { "type": ["boolean", "null"], "default": null },
     "frustration_signal": { "type": ["boolean", "null"], "default": null },
     "tokens_used": { "type": ["integer", "null"], "minimum": 0, "default": null },
     "llm_calls": { "type": ["integer", "null"], "minimum": 0, "default": null }
     ```
   - **Observation**: Because `_is_bool(None)` evaluates to `False` and `isinstance(None, int)` is `False`, passing an explicit `None`/`null` for any outcome field (e.g. `{"resolved": True, "escalated": None}` or `{"tokens_used": None}`) causes `validate_outcome` to raise `ValueError: outcome.escalated must be a boolean, got None` or `ValueError: outcome.tokens_used must be an integer, got None`. The server wraps this in HTTP 400 (`invalid_request`). An MCP client sending standard schema-compliant JSON payloads with nulls for unmeasured metrics is rejected.

6. **`hub/crud.py:1688-1690` — Inability to Nullify or Clear Outcome Fields in `amend_trace`**
   - **Location**: `/root/commontrace-v2/hub/crud.py:1688-1690`
   - **Code**:
     ```python
     resolved_outcome = dict(original.outcome or {})
     resolved_outcome.update(outcome)
     ```
   - **Observation**: Because `validate_outcome` rejects `None` as noted in finding 5, a caller attempting to clear an erroneously set outcome field via amendment (e.g. `amend_trace(id=..., outcome={"tokens_used": None})`) cannot pass validation. If validation is bypassed, `update(outcome)` preserves `None` in the database, but subsequent reads/evaluations fail.

7. **`hub/crud.py:1692-1700` — `amend_trace` Omits `profile` from Inbound Schema Validation Wire Object**
   - **Location**: `/root/commontrace-v2/hub/crud.py:1692-1700`
   - **Code**:
     ```python
     wire = {
         "id": amended_id,
         "title": resolved_title,
         "context_text": resolved_context,
         "solution_text": resolved_solution,
         "tags": resolved_tags,
         "agent_type": original.agent_type,
     }
     validate_trace(wire)
     ```
   - **Observation**: In `contribute_trace` (line 1023), `wire` includes `"profile": profile`. In `amend_trace`, `wire` omits `profile` even though `amended` persists `profile=original.profile` (line 1736).

8. **`commons/eval/representations.py:65` — ASCII-Only Word Tokenizer Strips Unicode Characters**
   - **Location**: `/root/commontrace-v2/commons/eval/representations.py:65`
   - **Code**:
     ```python
     _WORD = re.compile(r"[a-z0-9]+")
     ```
   - **Observation**: `representations.py` uses `_WORD = re.compile(r"[a-z0-9]+")` instead of `re.compile(r"\w+", re.UNICODE)` (which is the standard in `commontrace/overlap.py:80`). Any evaluation of non-English or accented strings through `representations.py` silently strips characters, corrupting character-ngram and word representations.

---

### 1.3 Security Vulnerabilities & Multi-Tenancy Invariants (R3)

9. **`hub/crud.py:800-804` — Missing Tenant ID in `search_traces` Retrieval Counter Update**
   - **Location**: `/root/commontrace-v2/hub/crud.py:799-804`
   - **Code**:
     ```python
     if traces:
         await session.execute(
             update(Trace)
             .where(Trace.id.in_([t.id for t in traces]))
             .values(retrievals=Trace.retrievals + 1)
         )
     ```
   - **Observation**: The `UPDATE` query updates `retrievals` without constraining `Trace.org_id == org_id` in the `WHERE` clause. This directly violates the core architectural invariant documented at lines 3-6 of `hub/crud.py`:
     > "every function that reads or writes a Trace takes the caller's org_id as an explicit, required argument and puts it in the SQL WHERE clause of the query that touches the `traces` table."
     For strict defense-in-depth, the query must be `.where(Trace.org_id == org_id, Trace.id.in_([t.id for t in traces]))`.

---

### 1.4 Performance Bottlenecks (R2)

10. **`hub/abuse.py:510-526` — Unmanaged Background Thread on Timeout in `_SharedPgPool`**
    - **Location**: `/root/commontrace-v2/hub/abuse.py:519-525`
    - **Code**:
      ```python
      if not ready.wait(timeout=_POOL_STARTUP_TIMEOUT_SECONDS):
          raise TimeoutError(
              f"timed out after {_POOL_STARTUP_TIMEOUT_SECONDS}s waiting for the "
              "HUB_RATE_LIMIT_BACKEND=postgres connection pool to start"
          )
      ```
    - **Observation**: If connection to Postgres hangs during `HUB_RATE_LIMIT_BACKEND=postgres` startup, `__init__` raises `TimeoutError` after 30 seconds, but does not stop or cancel the background thread (`_thread`). The thread continues running in the background attempting to establish connections or run its loop, leaking resources.

11. **`hub/crud.py:799-804` — Synchronous Row Locking on Search Retrieval Counter Increment**
    - **Location**: `/root/commontrace-v2/hub/crud.py:799-804`
    - **Observation**: Every page of `search_traces` issues an `UPDATE traces SET retrievals = retrievals + 1 WHERE id IN (...)` inside the search transaction. Because Postgres acquires row-level exclusive locks on `UPDATE`, concurrent queries returning the same top-ranked traces (e.g. popular queries) contend on row locks for `Trace`, serializing search read throughput.

---

### 1.5 Dead & Unwired Code (R4)

12. **`hub/schema_validation.py:84-88` — Unwired `validate_lesson` Function**
    - **Location**: `/root/commontrace-v2/hub/schema_validation.py:84-88`
    - **Code**:
      ```python
      def validate_lesson(obj: dict) -> None:
          """Raise SchemaValidationError if `obj` does not conform to
          protocol/schemas/lesson.schema.json. Not currently called by any Hub
          tool -- see hub/models.py module docstring."""
          _validate("lesson.schema.json", obj)
      ```
    - **Observation**: `validate_lesson` is never invoked anywhere in production Hub code. While harmless as scaffolding for future tools, it is completely unwired.

13. **`commons/eval/` — Evaluation Harness Separation**
    - **Location**: `/root/commontrace-v2/commons/eval/`
    - **Observation**: The scripts in `commons/eval/` (`run.py`, `representations.py`, `retrieval_tiers.py`, `search_modes.py`) are standalone research harnesses for validating the MinHash algorithm and tuning parameters. They are not dead code (they produce the results in `commons/eval/RESULTS.md`), but they are not part of the runtime package or CLI.

---

## 2. Logic Chain

```
[Observation 5: trace.schema.json defines outcome fields as ["boolean", "null"] & ["integer", "null"]]
     │
     ▼
[Observation 5: hub/outcomes.py:validate_outcome checks not _is_bool(v) & not isinstance(v, int)]
     │
     ▼
[Deduction: Python isinstance(None, int) == False, _is_bool(None) == False]
     │
     ▼
[Failure Mode: Sending valid {"resolved": true, "escalated": null} raises ValueError: outcome.escalated must be a boolean, got None]
     │
     ▼
[Client Consequence: Standard MCP client passing JSON null for unrecorded metrics receives HTTP 400 error]
```

```
[Observation 1: protocol/schemas/lesson.schema.json defines minLength: 1 on importance_rationale]
     │
     ▼
[Observation 1: memory/lessons/lesson_template.md defines importance_rationale: ""]
     │
     ▼
[Failure Mode: Validating stored lessons fails on lesson_template.md with length 0 < 1]
     │
     ▼
[Remediation: Set importance_rationale to a placeholder sentence matching commontrace/templates.py]
```

```
[Observation 9: hub/crud.py:search_traces issues UPDATE traces SET retrievals = retrievals + 1]
     │
     ▼
[Observation 9: The WHERE clause only checks Trace.id.in_(...), omitting Trace.org_id == org_id]
     │
     ▼
[Tenant Isolation Invariant: Every read and write to traces table must include org_id in SQL WHERE]
     │
     ▼
[Remediation: Add Trace.org_id == org_id to the update WHERE clause for strict defense-in-depth]
```

---

## 3. Caveats

1. **Postgres Driver Testing Environment**: The environment uses Python 3.14 without `asyncpg` or `psycopg2` installed globally; `hub/tests/` requires PostgreSQL test database fixtures. Findings for `hub/` modules were verified via static analysis, code trace, and unit tests of individual logic functions (`validate_outcome`, `resolve_client_key`, `search`, etc.).
2. **Schema Extensibility**: `trace.schema.json` does not declare `"additionalProperties": false` at the top level. Therefore, fields returned by `_to_wire` (`shared_with_commons`, `quarantined`, `quarantine_reason`) do not formally cause JSON Schema validation to fail under a permissive validator, but they represent undocumented property drift.
3. **Local Store Traces Directory**: `memory/traces/` is not committed in the repository (it is created by `commontrace capture` or `commontrace init`). The active memory directory contains `memory/lessons/` and `memory/episodes/`.

---

## 4. Conclusion & Proposed Remediation Table

| Item | Target File & Lines | Category | Severity | Description & Failure Mode | Proposed Remediation |
|---|---|---|---|---|---|
| **REC-01** | `hub/outcomes.py:214-236` | Bug (R1) / Schema | **High** | `validate_outcome` rejects `None` for outcome fields (`resolved`, `escalated`, `tokens_used`, etc.), raising `ValueError` on valid JSON nulls allowed by `trace.schema.json`. | Allow `None` if value is `None`: `if value is not None and not _is_bool(value):` and `if value is not None and (not isinstance(value, int) or isinstance(value, bool)):`. |
| **REC-02** | `memory/lessons/lesson_template.md:8` | Schema Conformance | **Medium** | `importance_rationale: ""` has length 0, violating `minLength: 1` in `lesson.schema.json`. | Update template line 8 to: `importance_rationale: "Why this importance score is justified (1 concrete sentence, not generic)"`. |
| **REC-03** | `hub/crud.py:801-804` | Security (R3) / Isolation | **Medium** | `search_traces` updates `retrievals` without `Trace.org_id == org_id` in the SQL `where` clause. | Change to: `.where(Trace.org_id == org_id, Trace.id.in_([t.id for t in traces]))`. |
| **REC-04** | `commontrace/commands/capture_cmd.py:25` | Protocol Conformance | **Medium** | `--agent-type choices=paths.AGENT_TYPES` restricts open-vocabulary agent types permitted by `PROTOCOL.md §7`. | Remove `choices=paths.AGENT_TYPES` from `--agent-type` parser definition, keeping default resolution. |
| **REC-05** | `hub/crud.py:1692-1700` | Code Quality / Bug | **Low** | `amend_trace` omits `"profile": original.profile` in the `wire` dict validated by `validate_trace`. | Include `"profile": original.profile` in `wire` before calling `validate_trace(wire)`. |
| **REC-06** | `commons/eval/representations.py:65` | Bug (R1) / Quality | **Low** | ASCII-only regex `_WORD = re.compile(r"[a-z0-9]+")` strips non-Latin Unicode characters during evaluation. | Replace with `_WORD = re.compile(r"\w+", re.UNICODE)`. |
| **REC-07** | `protocol/schemas/trace.schema.json:71-76` | Documentation / Schema | **Low** | `shared_with_commons`, `quarantined`, and `quarantine_reason` emitted by `hub/crud.py:_to_wire` are not declared in `trace.schema.json`. | Add optional properties `shared_with_commons` (boolean), `quarantined` (boolean), and `quarantine_reason` (string) to `trace.schema.json`. |
| **REC-08** | `hub/abuse.py:519-525` | Robustness / Resource Leak | **Low** | `_SharedPgPool.__init__` raises `TimeoutError` on slow startup without stopping the spawned background thread. | Call an event or flag to signal the thread loop to terminate upon timeout. |

---

## 5. Verification Method

### 5.1 Verify Schema Validation Failure on `lesson_template.md`
```bash
python -c '
from commontrace import validate, frontmatter
schema = validate.load_schema("lesson.schema.json")
fm, _ = frontmatter.read("memory/lessons/lesson_template.md")
errs = validate.validate(fm, schema)
print("Validation errors on lesson_template.md:", errs)
assert any("importance_rationale" in e for e in errs)
'
```

### 5.2 Verify `validate_outcome` Rejection of Null Fields
```bash
python -c '
from hub.outcomes import validate_outcome
try:
    validate_outcome({"resolved": True, "escalated": None})
    print("UNEXPECTED SUCCESS")
except ValueError as e:
    print("Reproduced expected failure on None:", e)
'
```

### 5.3 Verify `commons/eval/` Benchmark Execution
```bash
python commons/eval/run.py
python commons/eval/search_modes.py
python commons/eval/retrieval_tiers.py
python commons/eval/representations.py
```

### 5.4 Verify Core Test Suite Baseline
```bash
pytest tests/ -v
```
