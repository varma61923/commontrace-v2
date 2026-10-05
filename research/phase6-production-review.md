# Memory performance and security improvements

This release extends `c8ce5c792916c10e23b0618c59259960294b7bcc` with exact
scoped retrieval, linear conversation/document preprocessing, indexed temporal
graphs, reusable console reports and concrete security fixes. The pinned
competitor repositories and papers remain documented in
[competitor-analysis.md](competitor-analysis.md) and
[algorithm-audit.md](algorithm-audit.md). No competitor source was copied.

## Measurements against the published baseline

| Operation and workload | Before | After | Gain |
| --- | ---: | ---: | ---: |
| Cold mapped retrieval: 200,000 vectors, ten eligible passages, eight queries | 24.836 ms | 0.703 ms | 35.33× |
| Conversation chunking: 1,000,000 characters | 56.491 ms | 1.249 ms | 45.23× |
| Grounding 2,000 weekday mentions in one clause | 3,956.408 ms | 19.677 ms | 201.07× |
| First graph query plus 20 distinct-date queries, 20,000 edges, degree eight | 4,582.029 ms | 122.837 ms | 37.30× |
| Document chunker: 8 MiB direct input | 491.143 ms | 15.092 ms | 32.54× |
| Console report batch: 502 requests, 5,000 events | 955.743 ms | 172.266 ms | 5.55× |
| Scoped lesson recall: 100 requests over 1,000 lessons | 745.138 ms | 332.719 ms | 2.24× |

The exact retrieval IDs/scores, conversation units/dates, graph responses and
normalized console responses match their baselines on these workloads. Existing
context budgets and canonical dense scoring are unchanged. The slower native
scoring experiment was removed after end-to-end testing.

These are measured component workloads on shared hardware. They do not establish
a universal 20× speedup, overall answer quality, or superiority over managed
competitor deployments. Graph index setup rises from 92.154 to 122.495 ms; the
paired graph row includes that cost. The 8 MiB chunker input exceeds the 4 MiB
text-file ingestion ceiling and exercises the direct transform API. Adversarial
parser gains and allocation measurements are reported separately rather than
presented as ordinary document throughput.

Detailed samples, source identities, reproduction commands and limits:

- [Scoped mapped retrieval](retrieval-acceleration.md)
- [Conversation preprocessing](conversation-hotpath-performance.md)
- [Graph, facts and observations](graph-phase6.md)
- [Console serving and lexical caches](serving-phase6-performance.md)
- [Ingestion, catalog and SQL](ingestion-security-phase6.md)
- [Hub authentication, RLS and webhooks](security-phase6-hub.md)

## Production behavior and security

Selected mapped evidence is checked against authoritative source IDs, turns and
content hashes. Partial views never enter the global index; a failed proof
restarts every query facet through source-backed retrieval. Graph generations
are captured consistently, support read-only mounts, and return detached public
objects. Current graph state changes on effective start/closure/expiry dates.
Current fact search, consolidation and gateway recall respect temporal windows.
Scoped observations retain scopes and source fact IDs in separate ID namespaces.

Lesson metadata, lexical indexes, document registries and ingestion ledgers use
strong file identities, so same-size edits with restored modification times do
not conceal changes. Console caches have bounded retention and coalesce concurrent
report work. Binary lexical format three and metadata format five rebuild old
generated artifacts from raw sources; v1/v2 rejection, every identity field and
exact scorer/CJK roundtrips are tested.

Document retrieval returns registered snapshots and never falls back to arbitrary
filesystem reads. Descriptor-pinned regular-file ingestion rejects symlink/FIFO
substitutions and limits source growth, archive inflation, XML depth and extracted
text. Malformed PDF/HTML/role-tag cases no longer trigger demonstrated quadratic
scans. Guarded SQLite queries preserve quoted data and nested query limits while
enforcing read-only authorization, row and result budgets. Document/SQL MCP reads
run in worker threads. Platform and SQLite resource-limit qualifications remain
explicit in the ingestion review.

Linked-user JWT authentication now applies the same regional check as API keys.
REST/OTLP transactions bind the authenticated tenant to the configured RLS
backstop. Webhooks read status without buffering recipient bodies and use one
deadline across DNS, fallback addresses and response headers. Existing host
pinning, private-address policy and TLS verification remain in place.

## Validation

The frozen production implementation passed **4,457 core/end-to-end tests**
(34 skipped) and **2,464 real-PostgreSQL Hub tests** (one skipped). Full Ruff,
medium/high Bandit, JavaScript syntax, whitespace and the existing local-tier
latency gate passed. That gate measured 88.4 ms at 6,400 lessons with a fitted
total-latency exponent of 1.22, within its 500 ms/1.5 thresholds. Shared CPU
timings varied; this gate is a regression threshold rather than a speedup claim.

An independent agent reviewed the security, provenance, cache publication and
temporal changes, found concrete regressions that were corrected, and finished
with no blockers. The final full run follows the cache-schema fixture migration;
all original exact posting/weight/tie assertions are retained. The measured
profiles use no generation or embedding model calls. Dependencies, official judges and benchmark scoring
are unchanged. Machine-readable checks and frozen production hashes are in
[phase6-validation.json](phase6-validation.json). Publication and exact-head CI
results are linked from the pull request.
