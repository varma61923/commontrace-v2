# Production hardening audit evidence

Repository base: `d9ec604`. Pull request: [#129](https://github.com/varma61923/commontrace-v2/pull/129).

## Coverage and limits

The inventory covered 1,504 tracked files at the base revision. Manual tracing
concentrated on Hub configuration, local SQLite and hosted PostgreSQL memory
records, dependency grounding, scoped retrieval, delegation, finite policies,
session history, source citations, and erasure transactions. Supporting privacy,
cryptographic authority, database session, SDK, CI, deployment, and API contract
boundaries were also examined. Repository instructions, README, manifests,
verification configuration, and relevant design documents informed these checks.
No `AGENTS.md`, `AGENTS.override.md`, `CONTRIBUTING*`, or `PLANS.md` was found.

This is a targeted hardening result with broad automated validation. It is not a
completed semantic, line-by-line audit of every first-party file. An inventory,
green tests, static analysis, and dependency checks do not establish that broader
claim, complete standards compliance, or production capacity.

## Verified and repaired findings

| ID | Severity | Previous behavior and repair | Regression evidence |
| --- | --- | --- | --- |
| H1 | P2 | Unknown Hub boolean values silently became false, including malformed required-RLS configuration. Reject unknown values and name the setting. | Security boolean tests in `hub/tests/test_config_and_limits.py`. |
| H2 | P1 | A small shared dependency graph caused exponentially repeated local grounding checks. Memoize per request and depth on a SQLite read snapshot; bound recursive grounding to 4,096 entries and 64 levels; deduplicate repeated edges. | Real SQLite point-read and search tests in `tests/test_memory_grounding_bounds.py`, including concurrent source retirement, later-request freshness, cycles, depth, and work limits. |
| H3 | P2 | Hosted grounding cached eligibility by ID alone. An overdeep candidate could cause a valid shallow candidate sharing its source to disappear. Include depth in the request memo key. | Both ordinary and capability-prefilter paths failed before repair in `hub/tests/test_memory_grounding_bounds.py`. |
| H4 | P1 | The hosted turn buffer returned episode text despite an active policy denying episode reads. Apply policy/capability admission before the recent-history window, then recheck visibility and readability. | Authenticated PostgreSQL tests in `hub/tests/test_policies.py`: denied reads previously exposed two episodes; permitted reads remain available. |
| H5 | P1 | A fact-only capability could cite an existing episode and erase it through the fact's privacy cascade. Require forget permission for each upstream episode before any erasure mutation. | Authenticated PostgreSQL tests in `hub/tests/test_delegation.py`: unauthorized cascade returns 403 and preserves both records; an authorized cascade succeeds. |
| H6 | P2 | HTTP erasure checked ownership in a separate transaction. A concurrently reused ID could be erased in its new scope; a missing root could trigger an unowned forward-reference cascade. Recheck an existing root's scope under the tenant writer fence. | Deterministic committed interleaving and missing-root tests in `hub/tests/test_rest.py`. |
| H7 | P2 | Hosted erasure rescanned all history once per dependency level. Index historical causal edges once and traverse the affected graph. | `hub/tests/test_memory_erasure_closure.py`: 32 seeded graph comparisons against the previous fixed-point semantics, shuffled input, and a 5,000-record chain requiring one history scan. |
| H8 | P2 | Expiration checked only a version number that resets after erasure. Reusing an ID at version 1 between selection and deletion could erase new unexpired content. Bind expiration to both the version and canonical digest of the selected stored envelope, checked inside the erasure transaction. | Both stores failed same-scope and changed-scope reuse tests before repair. Legacy envelopes omitting optional fields still expire correctly. |

Fixed findings: **P0: 0; P1: 3; P2: 5; P3: 0**. These counts describe verified
findings from the examined paths, rather than a count of all possible defects.

The policy documentation's promise that runtime reads obey active policy was
inconsistent with the turn buffer. H4 brings that path into agreement. Searches
for unfinished code found intentional abstract methods, explicitly unsupported
provider deletion, and draft lesson scaffolding; these were not treated as
unimplemented product features.

## Performance evidence

The local reproducer constructs two records per layer, each deriving from both
records in the preceding layer, and a final root. With 12 layers there are only
25 unique records. Before repair, a point lookup performed **8,191** admission
checks; after repair it performed **25**. A 20-layer repaired graph performed
41 checks for 41 records. The regression asserts work counts, without a fragile
wall-clock threshold. Memoization is request-local and depth-sensitive, so this
linear result describes this layered graph; arbitrary graphs also have the
explicit traversal budget. No memo survives a source revision or later request.
SQLite readers retain one revision while concurrent WAL writers commit; a later
request observes the new revision rather than a cached eligibility result.

For erasure, a single-parent chain previously required quadratic history
scanning. The new closure indexes history and edges once: **O(H + E)** time and
additional graph space, where H is historical rows and E is causal edges, plus
the bytes needed to encode scope keys. Measurements below are medians of three
runs on the audit host, with equivalent deleted/source sets asserted each time.
The baseline is the previous fixed-point algorithm retained as a test oracle.
Concurrent test execution affected timings; these are not production SLAs.

| Historical chain rows | Previous closure | Repaired closure |
| --- | ---: | ---: |
| 100 | 55.446 ms | 0.349 ms |
| 400 | 423.225 ms | 0.943 ms |
| 1,000 | 2,925.632 ms | 6.540 ms |

## Compatibility and operations

- No database migration, dependency update, or wire schema change is required.
- Existing explicit boolean spellings remain accepted. Malformed settings now
  stop startup instead of silently disabling a setting.
- Recursive grounding beyond the work budget is withheld; records remain stored.
  The existing 64-level limit remains. Large valid dependency graphs may therefore
  yield fewer results and should be exercised with representative tenant data.
- Hosted session buffers now respect read permissions independently of capture
  permissions. A denied read can legitimately produce an empty buffer.
- A scoped HTTP forget request for a missing root now returns the existing
  invalid-request error (HTTP 400). It cannot erase dependents of an unowned ID.
  Repeating an already completed erasure may therefore return 400.
- Python `PostgresJournal.forget` accepts an optional `scope` for atomic ownership
  checking. Trusted unscoped callers and version-checked expiration retain their
  existing calling convention.
- Reverting the PR's commits requires no schema rollback, but restores the
  repaired defects. Erasure already completed cannot be undone by reverting code.

## Residual risk and follow-up

| Severity | Component | Evidence, impact, and next action |
| --- | --- | --- |
| P2 | Hosted erasure capacity | `PostgresJournal.forget` still loads all tenant historical events under its writer fence. The quadratic walk is repaired, but memory remains proportional to tenant history. A large tenant can incur substantial memory use and block its other writes. Validate production history cardinality, query plans, and peak memory; design a durable paged erasure workflow or indexed database closure with explicit restart and atomicity guarantees. That requires a separate lifecycle/migration design and representative scale evidence. |

Production topology, real tenant history, cloud permissions, external provider
failure modes, backup deletion, and credential rotation were not validated by
local tests. No production deployment or data change was performed. Optional
service and privileged checks skipped by the test suites remain outside local
validation. The PR's Testing section records the exact completed commands,
results, CI revision, and environment interruptions.
