# Operations, troubleshooting and migrations

## Deploy and observe

Use the tested deployment assets in [deploy/README.md](../deploy/README.md) and
[hub/DEPLOYMENT.md](../hub/DEPLOYMENT.md); those cover local/staging/production
setup, migrations, TLS, replicas, connection budgets and restoration drills.
CI validates assets and never applies infrastructure. Keep development on
loopback. The local HTTP gateway defaults to at most 128 active connections;
`make_http_server(..., max_connections=...)` configures the positive limit. Idle
connections retain a slot until timeout; excess connections close without starting
a waiting worker. Deploy a reverse proxy with appropriate client retry/admission
policy when public traffic exceeds that budget. Store tokens/keys in deployment secrets, not CLI history or the repo.

Hub `/healthz` checks the process and `/readyz` checks database readiness.
Existing telemetry, request metrics, webhook delivery state and optional
ServiceMonitor/SLO alerts provide operational signals. Performance measurements
are workload-specific; do not translate a local warm p95 into a global SLA.

## Diagnose common failures

| Symptom | Check |
| --- | --- |
| Empty retrieval | Root/space, approved status, scopes, validity dates, source eligibility and configured floor; inspect raw evidence before changing the scorer. |
| Stale/corrupt optional index | Rebuild through the supported index command; retain authoritative source files/SQLite. Do not trust a generated index after source drift. |
| Lock timeout | Find the active writer and check filesystem locking support. Acquisition defaults to 30 seconds; protected-block execution is not timed out. |
| Invalid settings | Inspect the named config field; nonfinite/out-of-range retrieval settings fall back safely when loaded and are rejected when explicitly configured. |
| Unauthorized or forbidden | Distinguish missing/revoked API key, org region, API-key scope and person role/capability. Do not retry permanent authentication failures. |
| Rate limited | Respect Retry-After and shared pacing. Reduce concurrency or wait; retries have budgets and jitter. |
| Encryption failure after rotation | Confirm CURRENT key and configured PREVIOUS keys. Malformed envelopes fail with EncryptionError; do not bypass authentication or log ciphertext/keys. |
| Webhook retry | Check bounded delivery state, endpoint validation and connectivity; stored errors omit credential-bearing transport messages. |
| PostgreSQL pool pressure | Check replica × connection budget and pending work before increasing pool size. |

Use generated [environment reference](environment.md) for source locations;
never dump the process environment when diagnosing credentials. Tests for local
SQL enforce read-only statements, result bounds and progress-handler deadlines.

## Upgrade and recover

New explicit CLI/MCP lesson approvals bind governed frontmatter and body to a
signed local admission record. Editing those bytes requires another review;
operational usage counters and Hub synchronization fields do not invalidate it.
`commontrace lesson revoke SLUG` withdraws approval durably, including when an
old active lesson file is restored. Historical recall applies current revocation.
Set `require_integrity: true` in `memory/approval-policy.yaml` to withhold legacy
unsigned lessons, then move each lesson to review and approve it through the
supported workflow. Keep the existing mode and separation-of-duties settings.

The local `.approval-key` and `lesson_admissions.db` under `memory/` are private
owner-only state. Back them up consistently with reviewed lesson files; moving
the root changes its bound identity and requires renewed approval. Deployment
approvers can supply `COMMONTRACE_APPROVAL_KEY_FILE` and
`COMMONTRACE_APPROVAL_KEY_ID`; retain the previous verification key using
`COMMONTRACE_APPROVAL_KEY_PREVIOUS_FILE` and
`COMMONTRACE_APPROVAL_KEY_PREVIOUS_ID` while rotating. Producers must not have
access to signing keys or write access to the trusted ledger. Receipt verification
cannot protect against an attacker with the same OS identity/key access or a
rollback of the entire trusted ledger. A receipt certifies review of content,
not factual truth or the absence of every possible prompt injection.

Back up raw memory and SQLite databases consistently before upgrading. Writable
conversation opens apply idempotent migrations; legacy turns stream and fact
hashes backfill in bounded batches. Belief repair is transactional and temporary
staging is removed on completion/failure. Keep raw provenance and historical
facts; do not delete old facts to resolve an update.

Hub upgrades use the existing Alembic migration chain; see deployment guidance.
Restore into a new database, validate readiness and tenant/source evidence, then
repoint configuration. Do not run test fixtures against a restored production DB.

Batch contributions return ordered independent outcomes: a later refusal does
not roll back prior successful items. Persist distinct idempotency keys before
retrying contributions. Batch deletions permanently remove owned chains and
must not automatically replay ambiguous transport failures.

## Console retrieval and review

`POST /v1/explore` is an authenticated, non-model retrieval inspection. Budgets
are bounded to 50–8,000 context tokens and 0–2,000 evidence tokens; conversations
require an explicit space. The gateway derives scope from its existing request
context and namespaces conversation spaces before opening storage. Container
routing is not a substitute for independently authenticated tenant identities.
Valid-time queries continue to enforce current revocation and deletion.

The console's **Fact ranking** control defaults to compatible overlap. Select
**BM25 · multilingual** to use English stemming, Unicode words and CJK bigrams.
The response records `fact_scorer`, and fact provenance records matched terms.
`POST /v1/explore` accepts `fact_scorer: "overlap-v1"` or `"bm25-v1"`; unknown
or malformed explicit values are rejected. MCP `query_facts` and
`archival_memory_search` accept `scorer`; `memory_recall` accepts `fact_scorer`.
CLI equivalents are `fact search --scorer` and `recall --fact-scorer`.

Fact indexes are disposable bounded in-memory state over the authoritative
`memory/facts/facts.jsonl`. Each request verifies the file generation, including
inode and change time; it does not wait for a TTL to notice mutation. Current
source quotations and lesson admission are verified per request. If the source
changes during assembly, retrieval conservatively withholds that snapshot.
Retained snapshots are capped at 64 MiB across eight store paths and scoped
statistics at 8 MiB. Canonical locked fact writes can advance an already warm
snapshot by reusing unchanged normalized records and token postings. Publication
is bound to the exact serialized bytes: a coherent read-back checksum verifies
the committed file before its generation can be cached. External replacement,
restart, expired/oversized cache entries or an unverified write retain the cold
reconciliation path. Current evidence checks are never reused from that snapshot.
Full JSONL writes, checksum verification, map copies and ordering remain O(N);
this is changed-record reuse, not constant-time writes or a persistent index.
Optional cache publication failures do not turn a committed write into a failed
API response. Active request views have their own lifetime.
`commontrace.fact_index.clear_cache()`
releases retained cache state; clearing Python objects is not physical secure erasure.
BM25 corpus statistics are filtered by routing, lifecycle and validity metadata;
they do not certify source support. Evidence-ineligible facts can affect ranking
statistics within the same authorized view, but cannot be returned. Ranking and
lexical coverage are separate from factual entailment and answer accuracy.

`python -m benchmarks.fact_retrieval` prints original labelled retrieval metrics
and scope, correction, erasure and abstention contracts. It does not measure
downstream answers or compare competitors. `python -m benchmarks.fact_search`
prints reproducible real-store timing and compatibility checks. Both use
temporary stores and clean up without creating result folders.
`python -m benchmarks.fact_mutation` compares canonical write-plus-search paths
and full cold-oracle results across mutation, correction, erasure and restart.
It emits timings and compatibility contracts to stdout; elapsed times are
descriptive and are not portable CI thresholds.

Lesson list/detail responses expose a governed-content `revision`. Send it as
`expected_revision` when editing, approving or rejecting. Comparison, fresh
scope/status checks and mutation occur under the same lesson lock. Stale
content returns `409 stale_review`; read the latest version before retrying.
The browser always sends a revision, while omitted revisions retain legacy
client compatibility. Oversized lessons cannot be approved from a truncated
console view. Generic browser commands cannot admit lessons or apply force
overrides; without `--allow-approval` they default to audited inspection handlers.
Container-scoped command sessions expose help only; use routed endpoints for
scoped reads and decisions.

Skip to content moves focus without changing the current route or discarding an
unsent form. Background data refreshes update navigation state in place, so a
focused section link remains active until the user presses Enter.

`python -m benchmarks.vector_search` prints seeded real-engine measurements to
stdout, including returned-score/order checksums and duplicate-heavy workloads.
The native dot-product screening optimization applies on supported CPython
versions; competitive rows retain the original exact scorer. Performance gains
depend on dimensionality and candidate distribution; no corpus-independent
speed multiplier or answer-quality improvement is implied.

## Budgeted conversation evidence

Conversation recall now assesses lexical coverage from the exact bodies and
profile statements delivered to the reader. Hidden portions of an excerpt,
speaker names and session headers cannot establish a missing subject or
identifier. Coverage remains a heuristic, not an answer probability.

The default `legacy` context strategy preserves existing ranking and packing.
Opt into `coverage-v1` to prioritize additional query facets per quoted token
after the existing strongest primary candidates (three by default). It considers at most 200 candidates,
keeps graph supporting passages together, and uses the existing scope, time,
injection and budget checks. Candidate-plan diagnostics describe available
excerpts; `explain.coverage` describes final delivered evidence. This strategy
can trade latency and ranking quality for diversity and should be evaluated on
your workload before enabling it.

```bash
commontrace conversation recall user "timeout retention recovery" \
  --context-strategy coverage-v1 --budget 1500 --lexical --json
python -m benchmarks.conversation_retrieval --dataset locomo \
  --data /tmp/locomo10.json --budgets 1500,7000
```

Python callers use `Options(context_strategy="coverage-v1")`; HTTP
`POST /v1/conversation/recall` and MCP `conversation_recall` accept the same
optional field. Unknown strategies fail explicitly. Cache entries are isolated
by strategy and retain the existing revision and expiry invalidation rules.

The maintained public-dataset evaluator uses fresh temporary production stores
with redaction enabled and prints JSON to stdout. It compares the two CommonTrace
strategies with identical histories and estimated budgets, disables final-response
caching, and alternates execution order. It records dataset, selection and source
checksums, category breakdowns, p50/p95 latency and conversation-cluster intervals.
Malformed gold references remain misses. Selecting a source and retaining its
entire canonical text are separate metrics; neither measures answer correctness.
Budgets currently estimate tokens as `ceil(characters/4)`, which is not provider
tokenization. LoCoMo categories 1–4 are evaluated; category 5 is excluded.

Acceptance gates cover unchanged default context behavior, bounded opt-in
selection, atomic graph support, scope/time/injection safety, actual delivered
coverage, fair matched budgets and absence of gold-label ingestion. Public-data
gains and regressions must both be reviewed; no fixed improvement is promised.
The [LongMemEval paper](https://arxiv.org/abs/2410.10813) motivates separating
indexing, retrieval and reading. Comparing downstream scores with
[Mem0's published evaluation](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)
requires matched models, judges, actual token accounting and service access.

## Multilingual conversation search

Conversation lexical recall supports NFC-normalized Unicode words and overlapping
Chinese, Japanese and Korean bigrams. Single-character CJK queries use separate
unigram postings. A non-ASCII word cannot become an unrelated ASCII substring;
mixed-language queries fuse complete ASCII words with Unicode candidates.
ASCII-only queries keep their existing porter/BM25 ranking and install no
additional tables.

The first Unicode query on a writable bank installs and builds a derived SQLite
postings index. Later ingestion queues changed non-ASCII units; SQL triggers also
invalidate updates, moved IDs and deletes made by other SQLite writers. Refresh
uses 256-unit batches before the recall snapshot, preserving canonical unit
hashes and the vector change journal. Frozen legacy banks and dirty read snapshots
fall back to source streaming without modifying the database. This fallback scans
eligible units and retains matching candidates, so its memory and latency costs
depend on corpus size. Unicode BM25 statistics use the authorized source scope;
the ASCII arm of a mixed query retains the legacy FTS corpus statistics.
The Unicode arm considers the first 16,384 query characters and at most 256
distinct terms; source passage sizes retain the existing ingestion limits.

Unicode excerpts select a bounded contiguous source span, prefer complete
sentences when they fit, and mark omitted edges. They can retrieve a relevant
tail from long text without spaces. Quotation, attribution and omission marks
count toward the same estimated token budget. These lexical mechanisms do not
translate text, establish entailment, or cover every Unicode script variant;
supplemental Han ranges and language-specific morphology remain limitations.

```bash
commontrace conversation recall user "北辰缓存容量" --lexical --budget 1500 --json
python -m pytest tests/test_conversation_unicode_index.py \
  tests/test_conversation_multilingual_excerpts.py -q
```
