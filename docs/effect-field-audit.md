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

## Follow-up audit: 2026-11-06

Starting commit: `e35fcaf`. This pass reviewed the recent numerical accelerators
and cleanup tooling, then traced gateway parsing/streaming, SQLite transactions
and handle ownership, download/build boundaries, and session accounting. It
combines parallel source review with reproduced failures, targeted regressions,
Graphify, static checks and the combined test suite. It does not claim that
every line or every optional external integration has been verified.

Graphify traced `conversation.store.write_txn` to conversation writes, vector
storage, snapshots and jobs. A two-hop `estimate_cost` caller query reached the
ledger CLI, MCP tool and LLM runtime, confirming that the accounting fix serves
the product entry points rather than an unused helper.

### Additional fixed findings

| ID | Severity | Defect and resulting behavior |
| --- | --- | --- |
| FA01 | High | A deferred SQLite constraint can fail at COMMIT while leaving rejected conversation writes in an active transaction. Commit now runs inside the rollback boundary; automatic rollback preserves the original exception, and rollback failure discards the unsafe handle. |
| FA02 | Medium | Failed connection PRAGMAs, setup, migrations and schema inspection leaked conversation/fact SQLite handles. Setup is protected from the first PRAGMA; cleanup closes handles and preserves the original setup error. |
| FA03 | Medium | An embedder's close exception skipped other providers, the database and async executor shutdown. Cleanup now attempts every owned resource before propagating failure. |
| FA04 | Medium | Gateway parsing rejected literal Infinity but admitted overflowing JSON exponents such as `1e400`. Parsed floats must now be finite, including nested values. |
| FA05 | Medium | Duplicate Host/Authorization/Origin headers were collapsed ambiguously; malformed origins could produce uncaught parsing errors or a 500. Duplicate headers are rejected before dispatch and malformed origins receive a controlled refusal. |
| FA06 | Medium | Aborted or rejected streams did not explicitly release their source iterators. Explicit iterator ownership and transport cleanup close the source in its captured authorization context, including closure before consumption and failure while sending headers. |
| FA07 | Medium | Dataset/SDK downloads shared temporary names, causing concurrent collisions and following precreated symlinks that overwrote unrelated files. Each invocation now owns a private sibling directory and atomically installs only verified content; failures remove only its own temporary state. Cached SDK generator hashing also streams under the existing 64 MiB limit. |
| FA08 | Medium | Docker context copying followed symlinked entrypoint ancestors, and lexical containment checks missed aliased checkouts. Ancestors are checked and containment uses canonical paths before copying. |
| FA09 | Medium | Tiny valid randomization rates overflowed inverse-propensity weights or their squares. Extreme arms use scale-invariant weights; leaving out an extreme context recomputes the retained scale. Validation checks original rates and permits derived probabilities rounded to one. Ordinary arithmetic remains unchanged. |
| FA10 | Medium | Empty, nonpositive, Boolean or fractional power grids reached division errors or invalid kernel operations. Grid/count validation now rejects them before numerical work. |
| FA11 | Medium | Broad model substrings priced `gpt-4o-mini` at `gpt-4o` rates. Specific families and bounded provider/version aliases now determine the estimate; ambiguous partial identifiers use the documented fallback. |
| FA12 | Medium | Invalid financial costs could enter session journals and propagate into summaries. New costs and consumed historical cost metadata must be finite and nonnegative. Corrupt stored token/cost fields and overflowing aggregates raise explicit errors without modifying history. |

The conversation regressions reproduce eleven failures against the original
implementations; all pass after the fixes. Fact connection regressions reproduce
five setup/rollback failures. Both original download implementations overwrite a
protected fixture through their shared temporary-name symlink. Tests exercise
concurrent downloads, interrupted transfers, deferred constraints, cleanup
failures, context-bound stream cleanup, malformed HTTP input, extreme numerical
inputs and ledger attribution.

### Unwired and dead-code disposition

No supported public integration was enabled or deleted solely because its name
was absent from a static call graph. The new cleanup command remains wired
through Makefile and the development CLI. Framework fixtures, ABC hooks, SDK
exports and optional callbacks remain intentional. Unsafe shared download paths,
whole-file generator hashing and cleanup paths that skipped resources were
replaced by the validated implementations above. Ruff checks unused imports
and undefined names across the repository.

### Follow-up verification

| Check | Result |
| --- | --- |
| Combined core and end-to-end suite | **9,190 passed, 145 skipped** in 12m38s, with warnings treated as errors. |
| Focused conversation/async/journal suite | 654 passed, 20 skipped, warnings treated as errors. |
| Focused gateway/HTTP/TLS/stream/export suite | 155 passed, warnings treated as errors. |
| Numerical boundary regressions | 153 passed, warnings treated as errors. |
| Fact-store regression suite | 69 passed, warnings treated as errors. |
| Download/build tooling | 43 passed; type checks passed for the three changed scripts. |
| Accounting and connected LLM/competitor paths | 45 passed; ledger type check passed. |
| Ruff | Repository lint and configured formatting passed: 1,334 Python files formatted. |
| Mypy | Full 636-file check passed; strict checks passed for effect-field, vector scoring, fact index and async conversation store. |
| Graphify | 28,223 nodes, 94,437 edges; 402 modules, 291 possible unreachable candidates; zero missing endpoints; integrity/budget gates passed. |
| Security scans | Bandit: zero medium/high findings. Hub direct pins and full transitive lock: no known vulnerabilities reported by pip-audit. |
| OpenAPI and packaging | Gateway/Hub drift checks passed; wheel and sdist built. |

Focused counts overlap and must not be added to the combined suite total.
The high-confidence production credential-format scan found no matches for the
selected token/private-key patterns; it is not a secret-history scan. Hub
database integration, real provider/network/hardware integrations, non-Python
SDK toolchains and remote CI were not rerun for this follow-up. No dependencies
or Hub SQL/schema code changed. The earlier audit's Hub/TypeScript results above
are historical results, not new verification claims.

The combined run used:

```bash
.venv/bin/python -B -m pytest tests e2e_tests -q -n 2 --dist worksteal -p no:cacheprovider -W error
```

Generated build/cache outputs were removed with the scoped `make clean` command
after verification. Audit logs and extracted graphs stayed outside the source tree.

## Ledger scale and malformed-input audit: 2026-11-06

Starting commit: `c3455c6`. A further pass traced session accounting through the
CLI, MCP and LLM runtime, and reviewed the JSONL reader and resource lifetime.
Eight new regression cases failed before the fixes:

- **[BUG] Summary truncation:** `session_summary` stopped at 10,000 calls and
  `overall_ledger_summary` at 100,000 before applying date filters. Both could
  silently underreport spend. Summaries now process the complete journal;
  tests verify 10,001/100,001 calls and date filtering beyond the former caps.
- **[EDGE CASE] Listing limits:** a zero or negative limit returned one entry.
  Such requests now return an empty list before opening the journal.
- **[BUG] Malformed persisted fields:** lists/dictionaries in model/session
  identifiers, numeric timestamps and non-object metadata escaped validation,
  causing unstructured errors or silently ignored metadata. Readers now reject
  these with an explicit corruption error while preserving the original file.
- **[PERF] Row retention:** summaries and small listings materialized every
  decoded row. The shared JSONL module now exposes a closeable streaming reader;
  its existing list-returning reader retains its prior parsing policy. Summaries
  retain aggregates rather than rows; listings retain at most the requested
  number of matching raw rows, validate only that selected tail, and preserve
  newest-first order. Readers close explicitly on validation/aggregation errors.

The new helpers are used by the public execution paths. Redundant metadata
fallbacks were removed after validation made the object contract explicit.
No optional callbacks, public integrations or migrations were removed.

### Measurements

Baseline `c3455c6` and candidate ran serially in separate Python 3.12 processes
without concurrent tests or scanners. Each workload had one warm-up and five
timed calls; setup was excluded. Peak Python allocation was measured separately
with tracemalloc, rather than included in the timings. Synthetic journal rows
used one session/model and 256 bytes of metadata padding. Both output digests
matched. Results describe these workloads, not every deployment or memory RSS.

| Workload | Baseline median ms | Candidate median ms | Baseline traced peak bytes | Candidate traced peak bytes |
| --- | ---: | ---: | ---: | ---: |
| Newest 100 entries from 50,000 rows | 323.131 | 207.543 | 94,184,818 | 197,094 |
| Complete summary of 10,000 rows | 105.017 | 96.896 | 20,493,346 | 26,577 |

Reading remains O(N). Summary aggregate memory still depends on the number of
distinct sessions/models/occasions, and each individual row must fit in memory.
The 100,001-row correctness case intentionally changes the previously truncated
answer and is separate from the parity measurements above.

### Verification

| Check | Result |
| --- | --- |
| Complete core and end-to-end suite for this pass | **9,201 passed, 145 skipped** in 13m36s, warnings treated as errors. |
| Focused ledger, JSONL, LLM accounting and competitor contracts | 162 passed, warnings treated as errors. |
| Types | Full 636-file check and strict checks for both changed production modules passed. |
| Ruff | Repository lint and configured formatting passed. |
| Graphify | 28,235 nodes, 94,476 edges; 402 modules, 291 possible unreachable candidates, zero missing endpoints; integrity/budget gates passed. |
| Bandit | Zero medium/high findings. |
| OpenAPI and packaging | Both API drift checks passed; wheel and sdist built. |

The performance comparison covered an unchanged-answer workload; it does not
establish universal speedups. Dependencies, Hub SQL and SDK implementations were
unchanged. Their service/toolchain integrations and remote CI were not rerun in
this pass; earlier results in this report remain historical.

The complete suite used the same two-worker, work-stealing command documented
above. Generated artifacts and the disposable baseline worktree were removed
after verification; no benchmark datasets or new audit files were added to the
repository.

## Provider accounting and secret-boundary follow-up (2026-11-06)

This pass follows baseline `9c433f9`. It preserves supported providers, public
completion results, cache-hit accounting, secret precedence and file rotation.
New failure-path regressions first reproduced 15 failures against the baseline
(27 other selected tests passed).

### Confirmed findings and changes

- **[SECURITY, EDGE CASE] `unimem/secrets_provider.py::env_secret`:** mounted
  `NAME_FILE` secrets were read without a bound, and UTF-8 decoding failures
  escaped the sanitized provider error contract. Configured `file:` references
  bounded characters after trimming, allowing a whitespace-padded oversized
  source to be silently truncated and accepted. Both entry points now use one
  closeable binary reader, read at most 65,537 bytes, reject raw files over
  65,536 bytes, and translate read/decoding failures without leaking secret
  bytes or falling back to stale plaintext. Existing missing-file diagnostics,
  empty-file overrides, universal-newline conversion, and valid rotation remain
  supported, including multibyte input and the exact byte-size boundary.
- **[BUG] `unimem/llm_runtime.py::LLMRuntime.complete`:** cancellation and other
  `BaseException` interruptions could escape without marking dispatch accounting
  unknown. A ledger write failure after a billable response also left the
  runtime available for further dispatch. One failure boundary now records the
  failure once and fences subsequent spending, while propagating the original
  exception, restoring the active context, and retaining already-known usage.
  Successful cache hits still record zero new provider spend.
- **[BUG, EDGE CASE] `unimem/llm.py::cost_usd`:** owner-supplied NaN, infinite,
  negative and boolean prices could poison or understate spending; enormous
  integers could raise an uncaught overflow. Invalid/unrepresentable costs now
  remain unknown, so a monetary budget stops the next call. Ordinary arithmetic,
  six-decimal rounding and numeric price strings are preserved. A scaled fallback
  recovers finite results when only an intermediate multiplication overflows.
- **[PERF, EDGE CASE] `unimem/retry.py::retry_after_seconds`:** a valid zero
  `Retry-After` was ignored in favor of exponential backoff. Zero is now honored,
  consistent with the nonnegative delay-seconds grammar in
  [RFC 9110 section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3).
  Exponentiation also saturates before large retry counts can overflow. Retry
  count limits, the 60-second wait cap and ambiguous-write replay protections
  remain in place.

The shared secret helper is connected to both existing resolution paths. No
public API, optional integration, migration or callback was removed solely from
static reachability results; no new production module or benchmark data file was
added.

### Measurements

Baseline and candidate ran serially in separate Python 3.12 processes with no
concurrent tests or scanners. Five timed loopback HTTP trials each received one
429 response with `Retry-After: 0`, then a successful `ok` response; both revisions
made exactly ten requests. Timings include the real retry wait and HTTP request
work, not server setup. These measure recovery from this specific valid server
hint, not general provider throughput.

| Workload | Baseline | Candidate |
| --- | ---: | ---: |
| Retry recovery median | 1,070.705 ms | 1.020 ms |
| Retry recovery range | 1,008.045–1,185.804 ms | 0.986–11.095 ms |
| 8 MiB mounted-secret peak traced Python allocation | 16,782,578 bytes | 70,606 bytes |
| 8 MiB mounted-secret outcome | Accepted all bytes | Rejected oversized file |
| Valid 64 KiB secret read median | 13.325 µs | 10.736 µs |

Secret allocation was measured separately with tracemalloc after file creation;
it is not RSS. Oversize rejection intentionally changes an invalid-input outcome
and is a bounded-resource check, not an equivalent-output speed comparison.
Valid-file timing used one warm-up and 100 measured reads; the small difference
does not establish a general throughput gain. Temporary fixtures and the baseline
worktree stay outside the repository and are removed after verification.

### Verification

| Check | Result |
| --- | --- |
| Complete core and end-to-end suite for this follow-up | **9,292 passed, 138 skipped** in 791.93 seconds; two workers, warnings treated as errors. |
| Focused provider/retry/pricing/accounting and Hub secret/configuration/KMS boundaries | 163 passed, 1 skipped; warnings treated as errors, no database required. The skipped test needs the optional Google GenAI SDK. |
| Types | Full 637-file check and strict retry-module check with `--follow-imports=silent` passed. |
| Ruff | Repository lint and configured formatting passed. |
| Graphify | 28,307 nodes, 94,659 edges; 402 modules, 291 possible unreachable candidates, zero missing endpoints; integrity/budget gates passed. |
| Bandit | Zero medium/high findings; this is static analysis, not a penetration test. |
| OpenAPI and packaging | Gateway and Hub API checks passed; wheel and source distribution built; isolated installed-wheel provider/secret smoke passed. |

No dependency, SQL, SDK or deployment implementation changed in this pass. Live
cloud-provider behavior, hosted CI, deployment and production-scale load have not
been verified here. Earlier service integration results remain historical.
The full typing command is the repository's existing check; it does not enforce
strict typing across every legacy module or check every untyped function body.

The complete suite used:

```bash
PATH=/tmp/unimem-production-tools:$PATH .venv/bin/python -B -m pytest tests e2e_tests \
  -q -n 2 --dist worksteal -p no:cacheprovider -W error
```

The temporary tools directory supplied Helm and promtool for native deployment
checks. Generated source-tree artifacts, the installed-wheel smoke directory,
and the disposable baseline worktree were removed after verification. No
benchmark fixtures or additional audit files were committed.

## Anticipation and worker-context follow-up (2026-11-06)

Starting commit: `6a57613`. This pass traced anticipation from the conversation
CLI and idle hook, plus the shared parallel helper used by cross-space MCP
retrieval and embedding ingestion. New regressions reproduced **13 failures**
against the baseline, with 19 passes; one test for the new iterator API was
deselected because that API does not exist in the baseline.

### Confirmed findings and changes

- **[SECURITY, BUG] `unimem/parallel.py::bounded_map` and
  `unimem/conversation/anticipate.py::expand`:** worker threads lost the caller's
  ContextVar bindings. Restricted writers became the default local operator,
  task-local offline policy disappeared, and anticipation bypassed the active
  LLM runtime budget. Each submitted operation now receives its own copied
  context. A regression checks the actual runtime call limit and verifies that
  worker changes to context bindings do not leak to the caller or later jobs.
- **[PERF, EDGE CASE] `anticipate.expand`:** every prompt was rendered before
  dispatch, and `ThreadPoolExecutor.map` eagerly queued the entire pass. An early
  storage failure still drained all queued model calls. The new closeable
  `parallel.bounded_imap` yields ordered results with a default backlog of twice
  the worker count, cancels unstarted jobs on early exit, and drains accepted
  operations. Prompt rendering and SQLite access remain in the caller; provider
  calls remain in workers, including with one worker. `bounded_map` retains its
  list result, ordering and historical sequential fast path.
- **[BUG, SECURITY] `anticipate._write`:** an ignored duplicate `anticipated`
  insertion still appended another generated unit and FTS row. Deletion during
  inference raised a foreign-key error and aborted valid surviving work. Worse,
  SQLite could recycle the deleted turn ID, attaching erased source text's
  generated keys to an unrelated replacement. Rendering now captures the
  existing `Turn.evidence_hash`; the write lock protects a fresh batch read,
  fingerprint comparison and insert-or-ignore claim before indexing. Only live,
  unchanged, newly claimed turns receive generated units. Current turns are
  fetched once per batch, avoiding a query per fingerprint check. The Store lock
  also protects shared-connection operations; SQLite serializes separate handles.
- **[EDGE CASE] `anticipate.pending` and `_parse`:** negative limits selected
  everything, while booleans, strings and fractions were coerced into limits.
  Invalid limits now fail before dispatch; zero remains a valid empty pass.
  Deeply nested model JSON now leaves the batch pending instead of propagating a
  decoder recursion failure.

The shared iterator is used by both existing parallel-map callers and
anticipation. The local executor import, eager prompt lists and duplicate
submission implementation were removed. No module, public integration or
framework callback was deleted based solely on static reachability.

These changes follow the documented Python 3.12 behavior:
[Executor.map eagerly consumes inputs](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Executor.map),
and [copy_context captures bindings in O(1) time](https://docs.python.org/3.12/library/contextvars.html#contextvars.copy_context).
Context copying is shallow; arbitrary mutable objects stored in a ContextVar
retain their existing ownership contracts.

### Measurements and tradeoffs

Baseline and candidate ran serially in separate Python 3.12 processes without
concurrent tests or scanners. A fixture contained 6,000 distinct turns of about
700 characters, indexed in batches of 12 with two workers. Each trial used a fresh
copy of the same source database. Setup/copy time and output hashing were
excluded. One warm-up preceded five timed passes; peak allocation came from a
separate tracemalloc pass. The deterministic provider stub returned immediately.
Both versions made exactly 500 model calls per trial and produced identical
reports and generated-unit digests
(`52bef51b608097399af06b822a3a5b8ce59d9ac607b3c60b1ab96c514c544e3e`).

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| First provider dispatch, median | 92.556 ms | 6.198 ms |
| Complete pass, median | 398.927 ms | 621.934 ms |
| Complete pass, range | 364.522–434.620 ms | 601.276–695.370 ms |
| Peak traced Python allocation | 13,228,284 bytes | 356,527 bytes |

This fixture shows about 37 times less peak allocation and 15 times faster first
dispatch, with approximately 56% more total time for an instant-response stub.
Source revalidation and bounded scheduling add work; the checks prevent indexing
stale or erased evidence. This is not a universal throughput improvement or a
measurement of a live provider. Pending turn IDs still occupy O(N) memory; prompt
and submission backlog are bounded by batch size and worker count. Store caches,
individual source sizes and the caller's chosen concurrency also affect memory.

### Verification

| Check | Result |
| --- | --- |
| Complete core and end-to-end suite for this follow-up | **9,307 passed, 138 skipped** in 791.84 seconds; two workers, warnings treated as errors. |
| Focused anticipation, parallel backpressure and LLM accounting tests | 45 passed, warnings treated as errors. |
| Repeated overlap probe | 50 synchronized two-pass races, alternating shared and separate Store connections; each 12-turn batch had exactly one indexing winner. |
| Types | Full 637-file check and strict checks for both changed modules with `--follow-imports=silent` passed. |
| Ruff | Repository lint and configured formatting passed. |
| Graphify | 28,336 nodes, 94,742 edges; 402 modules, 291 possible unreachable candidates, zero missing endpoints; integrity/budget gates passed. |
| Bandit | Zero medium/high findings in production Python. |
| API and packaging | Gateway/Hub OpenAPI checks passed; wheel and source distribution built; isolated installed-wheel context and recycled-identity checks passed. |

No dependency, SQL schema, SDK or deployment implementation changed. Hosted CI,
live provider behavior and production-scale load remain unverified in this pass;
earlier service integration results remain historical.

The complete suite used the same two-worker, work-stealing command and temporary
Helm/promtool path shown in the preceding follow-up. Generated source-tree build
and cache artifacts, the installed-wheel smoke directory, and the disposable
baseline worktree were removed after verification. Benchmark data and graphs
were kept outside the source tree; no additional production module or audit file
was added.

## Async policy, context-kernel and dependency audit (2026-11-07)

Starting commit: `7d49137`, branch `feat/effect-field`. This pass used the
repository-pinned Graphify 0.9.92, repository-wide lint/type/security/dependency
checks and manual tracing of asynchronous conversation storage, model loading,
configuration secrets, effect-field neighbor selection and SDK dependencies.
The review is broad static coverage with focused manual verification; it does
not establish that every line or optional integration is defect-free.

### Reproduced findings and fixes

| Finding | Severity | Change |
| --- | --- | --- |
| AsyncStore lost request context | High | `run_in_executor` did not propagate ContextVars. Store construction and each admitted operation now run in separate copied contexts, preserving task-local offline refusal, configuration, runtime and identity bindings. Capturing only the queue worker's context would incorrectly apply the opener's settings to every caller. Batched writes still share a transaction while their operations retain individual contexts. |
| Legacy conversation models bypassed offline policy | High | `arctic-m` and `minilm` retried a local cache miss with an unrestricted model constructor. The fallback now checks the existing offline policy and raises `OfflineError` before attempting a download. Cached local models and online initialization retain their behavior. |
| Configuration had a weaker duplicate secret reader | High | `configuration.getenv` accepted truncated file secrets, exposed UTF-8 decoder errors and returned a default when configured file/environment sources were unavailable. Configured references now use `secrets_provider.env_secret`, including the 65,536-byte raw bound, sanitized failures, relative paths and fresh rotation reads. Explicit environment precedence remains unchanged. |
| Context-kernel neighbor counts were inconsistent | Medium | `CellKernel.neighbors` accepted negative/boolean counts and performed content-pool work for zero. It now enforces the content kernel's nonnegative integer contract and returns immediately for zero. A streaming heap replaces full candidate-list sorting, preserving rounded scores and deterministic ties within the existing neighbor roster. |
| Development and Hub dependencies could not resolve together | Medium | OpenAPI validator 0.7 required `referencing<0.37`, conflicting with Hub's JSON Schema pin. The development extra now uses validator 0.9, which supports the pinned schema stack and Python 3.10+. |
| SDK lock contained vulnerable transitive packages | High | Updated compatible locked versions of `@hono/node-server`, `body-parser`, `fast-uri`, `ip-address`, `qs` and `zod`. The initial production dependency scan reported six affected packages: two high and four moderate. These advisories include server-side dependencies bundled by the MCP SDK; exploitation through the UniMem client was not demonstrated. |
| Existing optional-value type errors | Low | Narrowed the optional embedder explicitly. Removed misleading missing-YAML import fallbacks in the reference scripts: those readers dereference YAML unconditionally, and PyYAML is the project's required core dependency. Optional NumPy/model fallbacks remain intact. |

The first regression run reproduced **10 failures** in async context, offline
model loading and configured secrets, alongside 58 passes. A separate kernel
probe reproduced **five failures** and one pass. New coverage checks independent
concurrent callers, worker binding isolation, actual task-local network refusal,
both legacy models under environment/task policy, oversized and invalid UTF-8
secrets, unavailable sources, exact byte boundaries, rotation, invalid neighbor
counts, the zero-work path and deterministic ranking over 1,000 cells.

### Wiring and dead-code review

Graphify traced `AsyncStore` through its public conversation export and
`frameworks.MemoryTools`, and traced `configuration.getenv` into model, retrieval,
offline, OAuth and handoff consumers. The new dispatcher is used by both batched
writes and queued reads/vector operations. The duplicated secret parsing and
unnecessary dynamic attribute lookup were removed; no public integration was
removed based on static reachability alone.

The unreachable inventory remains dominated by UI event callbacks, reference
script entry points, optional framework/robot adapters, OpenTelemetry callbacks
and urllib redirect handlers. These require their runtime registration or public
API contracts, rather than deletion merely because static callers are absent.
Ruff checks unused imports and undefined names across the repository.

### Allocation measurement

A synthetic pool of 40,000 cells belonging to one memory used seven repeating
contexts, one query and `k=12`. Baseline and candidate ran serially in separate
Python 3.12 processes, after a warm-up populated their ordinary pool/context
caches. A separate tracemalloc pass measured the warmed selection call, excluding
source construction. Its output digest matched in both versions:
`cb72e8d5238fbb99ecea79930a349cd6a7fcd5f429e765d4de069086c7ddacef`.

| Metric | Baseline | Candidate |
| --- | ---: | ---: |
| Peak traced Python allocation during warmed selection | 7,322,624 bytes | 2,098,312 bytes |

Timing samples were collected while other audit checks were active, so they are
not reported as throughput evidence. This measures neither RSS nor a cold/full
field fit. Pool membership and cached context/pool data still require memory
proportional to the source; heap selection removes the full scored list/sort,
and retains at most the requested result count. Context copies likewise isolate
bindings, while mutable objects stored in ContextVars retain their existing
ownership contracts.

### Verification for this pass

| Check | Result |
| --- | --- |
| Complete core and end-to-end suite for this pass | **9,285 passed, 155 skipped** in 1,743.40 seconds; two workers, warnings treated as errors. |
| Hub suite with disposable PostgreSQL 16 | **2,812 passed, 24 skipped** in 1,273.12 seconds; two workers, warnings treated as errors. |
| Focused effect-field, async conversation and vector materialization contracts | 70 passed, 20 optional pgvector cases skipped; warnings treated as errors. |
| Gateway and Hub OpenAPI validator contracts | 20 passed with validator 0.9; generated specifications are current. |
| Types | All 637 files passed the repository's full mypy check with both mypy 1.20.2 and 2.4.1 (the current unpinned CI resolution). Strict checks passed for async conversation storage and effect-field kernels; explicit checks also passed for the reference readers and synchronous conversation store. |
| Ruff | Repository lint and configured formatting passed across 1,338 Python files. |
| Graphify | 28,365 nodes, 94,824 edges, 402 core modules, 291 possible unreachable candidates; no missing endpoints or integrity/budget violations. Final extraction used `--code-only --no-cluster`, with the LLM backend disabled. |
| Python dependency scans | No known vulnerabilities in the Hub transitive lock or 150 pinned packages resolved from all declared extras for Python 3.12. The development extra also resolves for Python 3.10. |
| TypeScript SDK | Clean lockfile installation, build and all 42 tests passed. Full npm audit: zero vulnerabilities, down from six affected production packages. |
| Bandit | No medium/high production Python findings. |
| Packaging | Wheel and source distribution built; isolated installed-wheel checks on Python 3.10 passed for async context/SQLite capture, secret-file bounds and count validation. |
| Credential-format inventory | Selected GitHub token, AWS access-key, live Stripe key and private-key-header patterns matched only test fixtures. This is not a comprehensive or history-wide secret scan. |

Focused counts overlap and are not additive. Full mypy does not enable strict
checking of every legacy untyped function body. Real provider credentials,
external model downloads, optional cloud/robot hardware integrations, browser
automation and non-TypeScript SDK toolchains were not verified in this pass.
Hosted CI results are separate from these local checks.

To reproduce the full suites, set `AUDIT_DATABASE_URL` to a disposable PostgreSQL
URL. This audit used only a disposable loopback instance:

```bash
.venv/bin/python -B -m pytest tests e2e_tests -q -n 2 --dist worksteal -p no:cacheprovider -W error
HUB_TEST_DATABASE_URL="$AUDIT_DATABASE_URL" \
  .venv/bin/python -B -m pytest hub/tests -q -n 2 --dist worksteal -p no:cacheprovider -W error
```

The Hub suite used PostgreSQL 16. Fsync was disabled partway through the run to
reduce container filesystem fixture-reset costs; these tests verify SQL/API
behavior, not PostgreSQL power-loss durability. No production database or
deployment setting changed. Generated graphs, dependency inventories, benchmark
fixtures and build artifacts remained outside the tracked product source.

## CodeQL security-gate remediation (2026-11-07)

Starting commit: `3fc60d8`. The Actions workflows completed successfully, but the
separate GitHub Advanced Security `CodeQL` check on PR #186 failed with four new
high-severity clear-text logging alerts. The previous local verification did not
cover that separate gate. Its annotations identified fact-consolidation output,
conversation indexing in JSON/text, and store-initialization metadata.

The generic configuration getter can resolve the `secrets` mapping, including
arbitrary opaque file contents. Model/backend metadata must not share that value
path. Pattern-based output redaction cannot identify every opaque credential.

Added `configuration.setting` for public configuration. It refuses credential
names and secret-backed references before opening files or calling providers.
Embedding selection, consolidation embedding metadata and fact-backend selection
now use it. The existing bounded secret getter remains available for credential
consumers. Ordinary profile/settings/environment values keep their behavior;
public values configured through `secrets` now fail with a sanitized error.
Use `profiles.local.embedding` or `settings.runtime.conversation_embedder` /
`settings.runtime.consolidate_embedder` for embedding metadata, and `--fact-store`
or `UNIMEM_FACT_STORE` for the backend. Indexing translates invalid configuration
into the CLI's ordinary exit code 2.

Four CLI regressions reproduced the original behavior, including opaque values
in JSON and text output. They now verify refusal before any secret-file read.
Seven additional API cases cover credential-name refusal, unresolved references,
public configuration defaults and environment precedence. The expanded focused
suite passed **257 tests** with warnings treated as errors; lint, full 637-file
mypy checks with versions 1.20.2 and 2.4.1, and Graphify integrity/surface gates
also passed (402 modules, 291 reachability candidates, zero missing endpoints).

The CodeQL reproduction uses CLI **2.27.4** and Python query pack **1.9.0**,
matching the hosted run. Original and fixed source databases use the repository's
existing source exclusions. The original logging query is compared against the
fixed code's complete 52-query Python security-extended suite. Generated
databases and SARIF results remain outside the repository.

The baseline query reproduced exactly the four hosted logging findings at the
same locations. The fixed security-extended suite reports **zero clear-text
logging findings**. Its sole remaining diagnostic is the already documented
`hub/auth.py:64` HMAC-SHA256 index for 256-bit random API keys; the existing review
is recorded in `.github/codeql/config.yml`. Query selection and source exclusions
were retained. Hosted verification must inspect the PR's complete check rollup,
including the separate `CodeQL` policy check, rather than only workflow success.

Hosted verification of `349157b` cleared the CodeQL security gate, then exposed
an ONNX default-selection fixture that still patched the removed private getter
import. The fixture now binds an isolated store through the public `using(root)`
configuration context. With the native fastembed/ONNX test dependencies installed,
the exact CI selection (`test_onnx_text`, `test_embeddings`, and
`test_default_local_retrieval`, with `HF_HUB_OFFLINE=1`) passed **55 tests with no
skips**. The follow-up changes only that fixture and this verification record.

The next hosted run passed the ONNX selection and the separate CodeQL gate, but
the full Python 3.12 suite exposed nondeterministic personalization-test input.
For the failing trial ID, deterministic assignment withheld 166 of 400 cases
at a declared rate of 0.5 (exact binomial p=0.000808), so the production
assignment-integrity guard correctly refused it. Both wall-clock fact timestamps
and freshly generated store signing keys changed source revisions and trial IDs
between test runs. The shared frozen clock now also controls fact timestamps;
personalization fixtures create a fixed test-only private key in temporary
stores, retaining real signing and signature verification. A regression requires
identical source revisions and trial IDs across independent stores with the
same fixture inputs. Production randomization, key generation and integrity
thresholds are unchanged.
The complete affected fixture family plus assignment-integrity suites passed
**152 tests**, with **one optional-service test skipped**, under two workers and
warnings treated as errors. Lint and formatting also passed.

## Removal and cache-preservation review (2026-11-07)

Compared all changes in this audit against `7d49137`, including Python API
symbols, CLI argument registrations, console entry points, dependency extras,
dedicated cache/pool/snapshot modules and cache methods inside modified modules.
No tracked files, public symbols, CLI registrations, entry points or extras
were removed. Nine dedicated cache/pool/snapshot modules are byte-identical;
16 embedding, vector snapshot, effect-field and configuration cache components
are AST-identical. Cache algorithms, expiration policies, persistent vectors
and stored user memories were retained. The focused cache-preservation suite
passed **114 tests**, with **10 optional local cases skipped**; hosted native
integration jobs are checked separately.

Intentional replacements remove duplicate secret-reading and async-dispatch
code, replace full neighbor sorting with bounded top-k selection, and remove
the missing-required-PyYAML import fallback in two reference tools. Public
model/backend settings now reject secret references before resolving them;
offline model cache misses raise instead of downloading; invalid neighbor
counts are rejected. These are documented behavior changes, not removed
features. The private embed configuration import was replaced; its fixture now
uses the public configuration context.

The earlier `make clean` removed generated development artifacts, including
`.mypy_cache`, `.ruff_cache`, Python bytecode, build outputs and TypeScript SDK
`dist`; it did not clear application runtime caches or memory databases. The
TypeScript SDK build output was regenerated after this review. Generated
removal inventories remain outside the tracked product source.
