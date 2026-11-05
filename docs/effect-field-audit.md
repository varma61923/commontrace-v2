# Effect-field branch audit

Date: 2026-11-05. Starting commit: `46a0289`, branch `feat/effect-field`.

## Scope and method

The repository-wide pass used the pinned Graphify 0.9.92 revision in
`architecture-exploration.md`, Ruff, Bandit, dependency auditing, Python type
checks, OpenAPI freshness checks, packaging, and core/Hub test execution.
Manual tracing concentrated on effect-field kernels, content signatures,
binary/graded outcome writers, memory API/SDK learner updates, and robot replay.
This records the evidence and limits of the review; automated inventory is not
equivalent to line-by-line manual verification of every module.

Graphify's starting extraction scanned 1,420 code files and produced 28,057 nodes
and 94,001 edges. Its integrity gate passed with 402 core Python modules, no
dangling endpoints or integrity violations, and 291 possible unreachable nodes
against the existing budget of 318. `explain unimem_holdout_io_record_outcome`,
`explain unimem_rewards_record`, and
`affected unimem_memory_signatures_observe --relation calls --depth 1` located
shared writers, API/SDK callers, the measurement lifecycle, and erasure tests.
Generated graphs remain outside the repository.

## Fixed findings

| ID | Severity | Defect and resulting behavior |
| --- | --- | --- |
| EF01 | High | NumPy discarded negative embedding cosines before adding lexical similarity, overestimating mixed-space neighbors; it also skipped all dimensions except the first query's width. Both execution paths now preserve signed contributions and compare every matching dimension group. Duplicate queries no longer multiply scores. |
| EF02 | Medium | Squaring finite extreme embedding values overflowed or underflowed their norm, silently discarding vectors. Normalize by the largest coordinate first; reject nonfinite, boolean, and unrepresentable numeric inputs with validation errors. |
| EF03 | High | Binary memory API and local SDK outcomes updated policy/exploration learners before the canonical writer rejected a conflict. Validate the boolean and commit the canonical outcome first; refused outcomes cannot alter learners. Gateway binary outcomes honor its configured durability. |
| EF04 | High | Binary writers could override a graded commitment after a crash, or race the reward writer between its check and append. Writers now share the reentrant, interprocess outcome lock; binary writes also honor existing graded commitments. Secondary labels retain their immutable retry checks. |
| EF05 | Medium | Finite utility weights and opposite-signed bounds overflowed during scoring. Scale weights before summation and use bounded interval arithmetic. Huge integers produce validation errors; mixed-type term names and trailing newlines are refused. |
| EF06 | High | A nonfinite stored draw or a truthy nonboolean bit could bypass reward verification. Audit checks finite draw bounds and exact boolean types. Reward retries require the exact committed reward; returned rows cannot mutate the cached commitment. Draws remain below one even at the floating-point boundary. |
| EF07 | Medium | Concurrent signature observers checked revisions outside the file lock, appending duplicate/stale rows. Recheck under the write lock. Unversioned content now contributes to the key, contentless revisions clear stale representations, and unchanged content refreshes recency at most daily. Embedding digest bytes use explicit little endian encoding. |
| EF08 | Medium | Signature terms exposed mutable cached state; caches grew with every root and missed same-size edits. Loaded signatures use immutable term mappings; both indexes track file metadata, synchronize threads, clear inherited locks/indexes after fork, and retain at most 128 roots. Read APIs do not create filesystem lock files. |
| EF09 | High | A process killed during a reward/signature append left an unterminated fragment that swallowed the next append. Under the journal's write lock, discard only the uncommitted final fragment before retrying. Signed journals keep their existing fail-closed policy. |
| EF10 | Medium | Robot gateway HTTP errors retained their response resources on permanent refusals and transient failures. Every HTTP error is now closed before propagating refusal/retry behavior. |
| EF11 | Low | Agent/model identifier regexes accepted a terminal newline. Full-string matching now rejects it. |
| EF12 | Medium | Neighbor selection sorted the full pool and allocated a full query-by-member product. Use heap-based top-k, batches of at most 128 queries, and products of at most 128 by 512 entries (512 KiB of float64 product storage). Single-occasion reward lookups and signature observations also avoid copying the full root index. Exact dense search still requires quadratic work across a full fleet. |

Regressions first reproduced the faulty behavior before the fixes. Coverage
includes accelerated/stdlib parity, dimension mismatches, batching boundaries,
numeric extremes, malformed audit records, canonical refusal ordering, cache
isolation, concurrent signature observation, recency, erasure, partial appends,
and closure of seven permanent/transient HTTP statuses.

## Unwired and dead-code review

Ruff reports no unused imports or undefined names. Redundant secondary-metric
calls and the obsolete learner-before-commit flow were removed. The native-array
serialization dependency was replaced by explicit endian encoding.

Static unreachable candidates were checked against source, tests, and public
exports before deciding on removal. In particular:

- `agent_loop._noop_executor` is the fallback executor selected at runtime.
- `vector_scoring._native_dot` initializes the module's selected scoring function.
- `conversation.pool.ReadPool` backs the module-level pool and fork callback.
- Robot node handlers, framework defaults, browser event listeners, and OpenTelemetry
  exporter methods are callbacks or optional public integration surfaces.
- `ingest.pipeline.file_fingerprint` is a tested compatibility wrapper.
- `web_crawler` redirect methods are invoked by urllib's handler dispatch.

These remain supported code. Static reachability alone is insufficient evidence
to delete them or to wire optional behavior into unrelated execution paths.

## Verification

| Check | Result |
| --- | --- |
| Core and end-to-end Python suite | 9,064 passed, 145 skipped, warnings promoted to errors. |
| Hub, disposable PostgreSQL 16 | 2,789 passed, 24 skipped, warnings promoted to errors. |
| Graphify, updated code | 28,090 nodes, 94,061 edges; 402 core modules, 291 reachability candidates; integrity and budget gates passed. |
| Focused effect-field/reward/signature tests | 101 passed, warnings promoted to errors. |
| Authentication/security module | 28 passed; secondary-metric ordering/immutability module: 11 passed. |
| Cross-process outcome race probe | 32 synchronized binary/graded races produced exactly one consistent winner each; reward audit remained clean. |
| TypeScript SDK | Build and all 42 tests passed. |
| Ruff | Repository lint and format checks passed. |
| Mypy | Full 636-file check and strict checks for six changed/boundary modules passed. |
| Bandit | No medium/high severity findings in production Python. |
| pip-audit | No known vulnerabilities in the Hub lock or the 148 dependencies resolved from all declared extras for Python 3.12. |
| OpenAPI and packaging | Gateway/Hub specifications are current; wheel and source distribution built. |
| Credential-format scan | Four high-confidence token/private-key formats matched only test fixtures; this is not a comprehensive secret-history scan. |

The initial core run had 9,015 passes, 140 skips, and 11 setup-related failures:
Hub-dependent tests were collected while Hub requirements were still installing,
leaving their imported module references unset. The complete 28-test security
module passes after installation. The fresh full run began with the completed
environment; those initial failures are not presented as product defects or as a
passing suite. The final run uses two workers and work stealing to distribute
the expensive proof fixtures on this two-CPU environment.

The completed full-suite commands were:

```bash
.venv/bin/python -m pytest tests e2e_tests -q -n 2 --dist worksteal -W error
HUB_TEST_DATABASE_URL=postgresql+asyncpg://unimem_dev:devpassword@127.0.0.1:55432/unimem_hub_test \
  .venv/bin/python -m pytest hub/tests -q -n 3 -W error
```

The database credentials above belong only to the disposable loopback test
container created for this audit. Optional service/SDK skips remain skips;
they are not counted as verified integrations. Remote CI is separate from these
completed local checks.

## Limits

No claim is made that every vulnerability or race has been eliminated. Optional
external engines, real robot hardware, provider credentials, all operating systems,
production load, and non-TypeScript SDK toolchains require their own environments.
The finite-sample and statistical assumptions of the causal gate and effect-field
validation were preserved. Outcome data, secondary-label, and transparency journals
remain separate files; the common lock serializes cooperating writers, while
arbitrary process loss across all downstream projections is not a distributed
transaction. Cache invalidation supports append-only writes and atomic replacement;
external tools must not rewrite earlier bytes while also appending to a live file.
