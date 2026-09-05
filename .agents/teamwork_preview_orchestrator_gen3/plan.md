# Execution Plan: CommonTrace v2 Codebase Remediation

## Objective
Remediate all functional bugs, performance bottlenecks, security vulnerabilities, schema non-conformances, and dead/unwired code identified across the three Phase 0 Survey Handoffs. Ensure full regression test coverage, verify via adversarial review and forensic audit, and confirm all system gates pass.

## Milestones

### Milestone 1: Core CLI, Trace/Evidence I/O, & Memory Attention
- Remediate unhandled `OSError`/`FileNotFoundError` in `build_index.py` and `pilot_metrics.py`.
- Fix datetime naive/aware comparison in `measure_performance.py:compute_freshness`.
- Fix section regex lookahead leak in `trace_io.py`.
- Fix status filtering in `evidence_io.py:load_active_lessons`.
- Validate threshold parameter in `taxonomy_cmd.py`.
- Expand unhandled exception catching in `cli.py`.
- Optimize trace lookup by occasion ID in `capture_cmd.py` from O(N) to O(1) probe.
- Eliminate redundant trace directory reads in `pilot_cmd.py`.
- Optimize `memory/attention/query.py` staleness check.
- Wire `--include-importance-floor` in `query_cmd.py`.
- Fix help text in `serve_cmd.py` and add `memory/attention` initialization in `init_cmd.py`.
- Add dedicated regression tests for every fix in `tests/`.

### Milestone 2: Hub, Commons, & Protocol Conformance
- Fix schema violation in `memory/lessons/lesson_template.md` (`importance_rationale` minLength: 1).
- Fix `hub/outcomes.py:validate_outcome` to accept `None` on nullable fields (`resolved`, `escalated`, `tokens_used`, etc.) matching `trace.schema.json`.
- Fix `hub/crud.py` tenant boundary in `search_traces` retrieval update (`Trace.org_id == org_id`).
- Fix `amend_trace` to include `profile` in wire dict validated by `validate_trace`.
- Update `protocol/schemas/trace.schema.json` to declare optional `shared_with_commons`, `quarantined`, and `quarantine_reason` properties emitted by Hub wire.
- Fix CLI `--agent-type` in `capture_cmd.py` to allow open-vocabulary strings per Protocol spec §7.
- Fix Unicode regex in `commons/eval/representations.py`.
- Fix unmanaged thread cleanup in `hub/abuse.py:_SharedPgPool`.
- Add dedicated regression tests for Hub outcome nullability, schema validation, and tenant isolation.

### Milestone 3: Benchmark Integrity, Security Hardening, & Missing Tests
- Fix report collision sorting flaw in `measure_performance.py` (`-` sorting before `.`).
- Fix TOCTOU non-atomic report persistence in `measure_performance.py`.
- Fix JSON mode output contract on empty corpus in `measure_performance.py` and `pilot_metrics.py` (emit valid JSON error object).
- Fix HTML quote escaping and regex boundary matching in `measure_performance.py`.
- Fix root path resolution in `install_cmd.py` when `--dest` is specified.
- Ensure `PYTHONUTF8=1` in `_shellout.py` across all execution paths.
- Add comprehensive test coverage for `commontrace/commands/index_cmd.py` in `tests/test_index_cmd.py`.
- Add unit tests for untested `install_cmd.py` targets (`cursor`, `windsurf`, `devin`, `generic`).
- Add unit tests for `evidence_io.py` and `report_html.py`.

### Milestone 4: Verification, Adversarial Review & Forensic Audit
- Verify `pytest tests/ -v` passes 100%.
- Verify `commontrace bench` and `commontrace doctor` execute cleanly.
- Verify all lessons and schemas validate with zero errors.
- Dispatch independent Reviewers and Challengers to stress-test fixes.
- Dispatch Forensic Auditor to verify no hardcoded shortcuts, facades, or integrity violations.
- Aggregate all verdicts in `GATE_STATUS.md`.
- Report final verified completion.
