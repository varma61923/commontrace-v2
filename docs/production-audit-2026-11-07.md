# Production audit — 2026-11-07

Branch: `feat/effect-field`. Starting revision: `9d47e6a`.

This pass reviews the current implementation independently of the earlier audit
records. It uses the requested [agent-teams skill](https://github.com/varma61923/agent-skills/blob/main/skills/agent-teams/SKILL.md)
for separate numerical/proof, Hub, and transport/SDK reviews, with a lead reviewing
storage and integration. Findings, ownership, and executable completion gates
were recorded with `teamctl`. No customer service or database was used.

## Fixed findings

| Area | Reproduction and resulting behavior |
| --- | --- |
| Witness disclosure | An activated policy denied proof export, but `export_witness` disclosed private outcome rows and commitment seeds. Witness export now enforces source export and generalization policy under the incident lock. |
| Legacy authentication | Deletion, expiry, scope, or hash changes during Argon2 verification could admit a deleted credential or stale authority. Verification now refreshes and locks the current key after hashing, and refuses missing, replaced, revoked, or expired keys. |
| Authentication cache | An authentication lookup started before revocation could repopulate the invalidated cache. A generation fence prevents stale cache publication while preserving normal cache hits, TTL, expiry, and capacity. |
| Legal holds | Account deletion bypassed holds; other erasure paths could race hold placement. Hold/policy changes and destructive consumers now serialize on the organization row. Account deletion checks holds before external subscription cancellation, and deletion token changes use fresh locked state. |
| Billing | Restricted live Stripe keys bypassed the live-account guard. Boolean, nonfinite, overflowing, and sub-cent amounts could be billed incorrectly or raise unintended errors. Both live key prefixes require explicit live authorization; malformed money and evidence receive the normal refusal. |
| Off-policy estimates | Independently clipping large importance weights could fabricate effective sample size and an `IMPROVES` verdict. Uniformly tiny weights could all underflow. Common log-weight scaling preserves relative weights, self-normalized value, effective sample size, and influence estimates. |
| Context witnesses | A fixed 12-memory content shortlist could omit a valid context match and fail requests above 12 neighbors. Selection now expands until the remaining content upper bound cannot beat the result, including deterministic ties. Expanded rows remain local to the request. |
| Similarity cache resources | Exact expansion could exercise unbounded context/content pair memoization. Both pair caches now retain at most the existing `MAX_SIMILARITY_CACHE` budget; saturated misses still compute exact values and retained hits still use the cache. |
| Hub deletion scale | Retention and account deletion could exceed asyncpg's 32,767 bind-parameter limit. Retention now uses 5,000-ID batches within the original transaction, with tenant predicates and actual quota changes. Account deletion keeps trace selection and relation cleanup in SQL, preserving the audit count. Separate regressions erase 32,768 real PostgreSQL rows through each path. |
| Conversation retention | Session deletion and purge materialized all matching search-unit IDs and issued one Python-to-SQL deletion per unit. A bound bulk SQL deletion keeps index and source changes in the same transaction. Rollback and surviving evidence remain covered. |
| SCIM | Listing populated groups issued two additional queries per group. Membership hydration now takes one joined query per page and checks group/user/membership ownership. Unique tie breakers stabilize pagination and membership ordering. |
| MCP deadlines/lifecycle | Progress or heartbeats could keep RPCs open indefinitely despite HTTP inactivity timeouts. Initialization and each RPC now have completion deadlines. Invalid options are refused before allocating an HTTP client, and closed sessions clear their handle. |
| Concurrent HTTP status | Calls shared mutable status observations, allowing a 401 response to misclassify an unrelated timeout or 503. Per-call ContextVar observations isolate statuses and restore bindings after use. A regression exercises the real MCP/httpx2 transport with deliberately interleaved responses. |
| Disconnected Hub helpers | `contribute_trace` sent fields absent from the registered tool schema; `purge_subject_traces` called an unregistered name. Contribution now supports native fields and the legacy convenience mapping with one stable idempotency key per logical call; purge calls the registered tool. A test validates helper arguments against the actual server's tool schemas. |
| Ambiguous writes | Comment and subject-erasure helpers could replay after a lost response, duplicating comments or erasing newly arrived traces. They now refuse ambiguous transport retries. Nonobject tool responses fail at the transport boundary. |
| HTTP ownership/deadlines | Refused redirects leaked the original response on policy/DNS failure. Both connector handlers now close refused responses. Crawler reads use the shared total-body deadline in addition to the size cap; deadline overshoot remains bounded by one socket read timeout. |
| TypeScript clients | Runtime turn options could override the pinned session/query. Those fields now take precedence. Hub calls and retries reject use after close; concurrent closers await the same cleanup, and failed connection setup cleans up without replacing the original error. |

Public APIs and stored memory formats are retained. The contribution helper adds
native keyword arguments while preserving the existing text/rationale mapping.
The stricter refusal of unauthorized exports, held erasure, unsafe write replay,
malformed billing values, and closed-client use is intentional.

## Graphify and dead-code review

Extraction uses the repository's pinned Graphify 0.9.92 revision
`86c57be8204673c2a1b9849a3292a7dd3ab373d8`, with the LLM backend disabled:

```sh
GRAPHIFY_BACKEND=none graphify extract . --code-only --no-cluster --max-workers 2 --out ../unimem-architecture-final
python scripts/graphify_audit.py \
  --graph ../unimem-architecture-final/graphify-out/graph.json \
  --output ../unimem-architecture-final/audit.json \
  --source-root . --max-modules 402 --max-unreachable 318
```

The final graph contains **28,467 nodes, 98,384 edges, 402 core Python modules,
and 290 possible unreachable callables**. It has no missing endpoints,
integrity violations, or budget violations. Scoped queries traced authentication,
hold consumers, policy evaluation, witness disclosure, and Hub helper callers.

The disconnected helpers above are now wired to actual tool contracts. Duplicate
ID materialization, per-unit deletion dispatch, and per-group hydration loops
were removed. Static candidates include registered cleanup/fork callbacks and
public/optional adapters; no API or callback was removed solely because Graphify
could not establish a dynamic call. Ruff checks unused imports and undefined
names across the repository.

## Performance evidence

A synthetic deletion of one turn with 20,001 search units was run against the
starting and refactored store implementations in separate temporary databases.
Both preserved the remaining turn and its search evidence. Peak Python allocation
during deletion, measured with `tracemalloc`, fell from **1,936,018 bytes to 2,314
bytes**. Fixture construction was excluded. SQLite native allocations and RSS
are outside that measurement. Concurrent audit checks were active, so timing is
not used as throughput evidence.

Exact context matching can inspect more content memories than the old fixed
shortlist when contexts disagree or content scores tie. Geometric expansion,
top-k retention, per-query visited sets, and bounded pair memoization limit the
additional allocation; expanded rows do not accumulate in the pool cache.
Legal-hold serialization can add contention among writes for the same tenant.

## Validation

| Check | Result |
| --- | --- |
| Core and end-to-end coverage run | 9,370 passed, 151 skipped in 1,301.97 seconds; two legacy Argon2 mock fixtures failed and were corrected as described below. Coverage **85.90%**, exceeding the 80% gate. |
| Corrected authentication fixtures | All 28 tests in the affected file passed with warnings as errors. The full run's two failing cases also pass in the final failed-case rerun. There are no unresolved failures. |
| Complete Hub suite | Passed, exit 0, in 866.6 seconds with two workers and warnings as errors. The team completion gate discarded successful pytest output, so no exact full pass/skip count is claimed. |
| Final account-deletion follow-up | Passed on a separate disposable PostgreSQL database, including 32,768 traces and unrelated-tenant preservation. This directly covers the final SQL subquery change made after the broad Hub run started. |
| Focused numerical/proof regressions | Seven added regressions independently fail against the starting source. The 154-test assigned gate and final 50-test effect-field cache gate passed; counts overlap. A deterministic 200-pool comparison matched exact pairwise neighbor selection. |
| Focused I/O, Hub, and storage checks | I/O: 180 passed; authentication/SCIM/billing: 150 passed; retention/erasure: 110 passed; conversation retention/archive/resources: 33 passed. Counts overlap the broad suites. |
| Browser | All 22 real Chromium contracts passed. Chromium also runs in the final core selection. |
| SDKs and extension | TypeScript build and 47 tests; Go SDK tests with checksum-verified Go 1.27.3; four Rust SDK tests; two Rust effect-core tests; four VS Code tests passed. Real Rust/Python effect arithmetic comparisons are included in the core run. |
| Static checks | Full 637-file mypy, repository Ruff lint/format, and whitespace checks passed. Both OpenAPI exports are current. No medium/high Bandit findings remain. |
| Dependency checks | No known vulnerabilities in the Hub transitive lock or installed Python environment; TypeScript npm audit reports zero vulnerabilities. The audit environment's old pip was upgraded before the final environment scan. |
| Packaging | Wheel and source distribution built. An isolated core-only installed wheel passed imports, SQLite retention/search, asynchronous store lifecycle, context-neighbor selection, and invalid-deadline refusal checks. |
| Credential-format inventory | Selected GitHub token, AWS access-key, live Stripe-key, and private-key-header patterns matched only test fixtures. No credential values were copied into the inventory. |

The two old Argon2 tests mocked the former scalar `revoked_at` read. They now
supply the current key and explicitly assert the fresh `populate_existing` /
`with_for_update` lookup. This updates their fixture contract and strengthens
their locking assertion; it does not change production code. The broad run had
already collected their old definitions, so the corrected fixtures were rerun
separately. Together the broad run and corrected-case rerun verify **9,372
passing core/end-to-end cases**, with the 151 optional skips retained.

The Hub uses disposable PostgreSQL 16. Fsync was disabled partway through its
broad run to reduce fixture reset costs; no product configuration changed. These
checks exercise SQL/API behavior and concurrency, not power-loss durability.
Generated graphs, native toolchains, build artifacts, and full logs remain
outside tracked product source.

Representative commands (native binaries and the disposable database must be
provisioned separately):

```sh
python -B -m pytest tests e2e_tests -v -n 2 --dist worksteal -W error --cov=unimem --cov-fail-under=80
python -B -m pytest tests e2e_tests --lf -q -n 2 -W error
HUB_TEST_DATABASE_URL="$AUDIT_DATABASE_URL" python -m pytest hub/tests -q -n 2 -W error
python -m mypy unimem/ hub/ --ignore-missing-imports
make lint
python scripts/export_openapi.py --check
python scripts/export_hub_openapi.py --check
```

The initial exploratory core run was deliberately stopped after source changes
to restart collection against the frozen implementation. Its 3,690 passing tests
are not counted as a completed final suite.

Remaining verification boundaries: live hosted identity/payment/model providers,
cloud/robot hardware integrations, JVM/JetBrains toolchains, and native RISC Zero
receipt proving were not exercised. Optional-service skips are reported with the
final suites. Full mypy uses the repository's configuration, which does not
strictly check every untyped legacy function body. Dependency and credential
inventories are point-in-time checks, not proofs of absence or history-wide scans.
