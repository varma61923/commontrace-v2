# Original User Request

## Initial Request — 2026-09-19T21:48:55Z

Comprehensive audit and hardening of CommonTrace v2 to eliminate bugs, handle edge cases, optimize performance, and address security vulnerabilities across the protocol and reference implementation.

Working directory: /root/commontrace-v2
Integrity mode: development

## Requirements

### R1. Protocol & CLI Robustness
Ensure all CLI commands (`init`, `install`, `capture`, `trace`, `lesson`, `query`, `index`, `bench`, `sync`, `doctor`) handle edge cases gracefully with clear error reporting, input validation, and strict JSON schema compliance.

### R2. Attention & Memory Performance
Optimize embedding index generation, incremental updates, and semantic querying to scale efficiently with large numbers of lessons and traces without memory bottlenecks.

### R3. Security & Sandboxing
Audit and harden all file operations, path resolution, and external process execution against path traversal, unvalidated input injection, and unintended filesystem modifications.

### R4. Test & Verification Coverage
Expand automated test coverage and benchmark metrics to independently verify correctness across error conditions, edge cases, and performance regressions.

## Verification Resources
- Test suite: `python3 -m pytest tests/ -v`
- Memory benchmark: `commontrace bench`
- System diagnostic: `commontrace doctor`
- JSON schemas: `protocol/schemas/trace.schema.json`, `protocol/schemas/lesson.schema.json`

## Acceptance Criteria

### Test & Diagnostic Health
- [ ] All existing and new automated tests pass with 100% success rate (`pytest tests/ -v`).
- [ ] `commontrace doctor` and `commontrace bench` execute cleanly without unhandled exceptions or errors.

### Schema & Protocol Integrity
- [ ] Schema validation rejects malformed traces and lessons with informative diagnostics while accepting all valid instances.
- [ ] CLI commands exit with descriptive error messages and non-zero exit codes on invalid inputs, missing paths, or malformed data.

### Security & Path Safety
- [ ] All file read/write operations enforce boundary checks preventing path traversal outside designated workspace/project directories.
- [ ] Subprocess executions and external commands properly sanitize arguments and avoid shell injection risks.

### Performance & Scalability
- [ ] Attention indexing and query operations execute within bounded memory and handle large lesson sets without degradation.
