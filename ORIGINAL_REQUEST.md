# Original User Request

## Initial Request — 2026-09-05T12:34:34Z

Audit the entire `commontrace-v2` codebase to identify and resolve all functional bugs, performance bottlenecks, security vulnerabilities, and dead or unreferenced code while maintaining strict backward compatibility and full test coverage.

Working directory: /root/commontrace-v2
Integrity mode: development

## Requirements

### R1. Bug Detection and Remediation
Audit the entire codebase (`commontrace/`, `memory/`, `protocol/`, `benchmark/`, `hub/`, `commons/`) for runtime errors, edge-case failures, unhandled exceptions, incorrect type conversions, and protocol non-conformance. Fix all identified defects and add dedicated regression tests for every fix.

### R2. Performance Optimization
Identify and resolve performance bottlenecks, including redundant file I/O, un-memoized heavy calculations, suboptimal embedding and cosine similarity queries, and unnecessary process overhead in the CLI and attention layers, without altering public interfaces or external behaviors.

### R3. Security Hardening
Audit for and remediate security vulnerabilities such as unsafe deserialization (e.g. unsafe YAML loading), shell injection vectors, improper path traversal protections, insecure file permissions, and unvalidated external inputs.

### R4. Dead and Unwired Code Cleanup
Detect and remove genuinely dead, unreachable, or orphaned internal code, functions, and unused internal imports. For any partially implemented or unwired features intended by design, ensure they are either properly wired into the CLI / protocol or cleanly isolated. Do not remove documented public APIs or breaking protocol schema elements.

## Verification Resources

- Test suite: `pytest tests/ -v`
- Memory and system benchmark: `commontrace bench`
- System diagnostics: `commontrace doctor`
- JSON Schemas: `protocol/schemas/trace.schema.json`, `protocol/schemas/lesson.schema.json`

## Acceptance Criteria

### Functional & Regression Prevention
- [ ] Existing test suite (`pytest tests/ -v`) passes with zero failures or regressions.
- [ ] Every bug fix is accompanied by a dedicated test case that fails before the fix and passes after.

### Performance Verification
- [ ] `commontrace bench` executes successfully and demonstrates no throughput or latency degradation.

### Security Hardening
- [ ] Zero instances of unsafe deserialization, shell injections, or unconstrained file operations remain.
- [ ] `commontrace doctor` passes all diagnostics without error.

### Code Quality & Schemas
- [ ] All unreferenced internal dead code is removed or wired.
- [ ] All protocol traces and lessons adhere strictly to their JSON schemas.
