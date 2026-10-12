# Next-generation runtime: implementation and evidence

This branch combines `feat/commontrace-memory-evolution` and
`codex/next-generation-p0`. Memory evolution's code was already squashed into
main; the merge also preserves the feature branch's prompt/history. The user's
later instruction explicitly requested continuing beyond the original missed
P0 gate. The A–I requirement matrix, including incomplete acceptance criteria,
is in [implementation-status.md](implementation-status.md).

## Design decisions

SQLite schema v2 interns lexical terms and uses compact postings rather than
repeating long strings per fact. Governed fact mutation remains the canonical
pipeline. Transactions append events and update exact/near/contradiction/FTS
projections together; migration preserves normalized checksums and the original
JSONL. Readers validate current evidence, scope and time. Durable sequence
watermarks publish vectors before advancing cursors; content hashes reuse
embeddings after restart. Canonical revisions immediately withhold old dense scores
until their vector outbox is published. JSONL export and projection rebuild remain supported.
Back up through SQLite's backup API, or stop writers and checkpoint WAL; copying
only the active database file can lose committed state.

Runtime envelopes use a separate private WAL journal for records, captures,
extraction cursors, study grants and delivery decisions. Entity/facet facts
atomically refresh a stable observation from at most nine active facts in the
exact scope. It copies the latest source text by default; optional consolidation
may summarize but rejects invented numeric values. Derived reads verify source
versions, active status and validity recursively; missing proof, cycles and
retracted sources fail closed. Unknown forward references cannot later cross
scope. Redaction preserves logical identities and owner scopes, including email-shaped
identifiers. Human profiles bind exact captured evidence; erasure follows every
revision and removes same-scope sources and dependent profiles. Legacy record payloads and causal conformance vectors remain compatible.

The Hub record engine uses a transactional PostgreSQL event/projection ledger,
full-text candidates, canonical validation of optional owner-pinned pgvector/HNSW
hits and migration-installed, forced tenant RLS. The same minimal stdlib envelope
and visibility rules are used in the server image. Hub record CRUD does not imply
that every legacy Gateway operation has been ported to the Hub. Hosted `turn`
and `profile` now capture source-bound human preferences and return a scoped
pending buffer. Turn requires both read and write permission. Sparse-only
candidates do not produce an inferred memory injection or causal exposure.

The native hook broker keeps optional embedding imports and learning off the
host's process startup path. The Node bridge has a 25 ms socket deadline and
acknowledges a delivery only after writing context to stdout. Missing optional
models preserve captures and critical directives. A stopped broker launches a
detached capture-only writer. The core logs both exposure and withholding once
per scoped session; child sessions inherit decisions. Only successfully delivered
assignments enter the randomized experiment. Fixed rules and critical preferences
precede budgeted, escaped user-level context; system prefixes stay stable.

Captured runs split at explicit Stop/SessionEnd/SubagentStop boundaries and use
bounded segments, at most nine related cases and ten prior lessons. Tool-level
outcomes are not treated as whole-run success. A curator and independent writer
produce at most one typed proposal per cluster; offline proposals are quarantined.
An identified reviewer may authorize a finite, expiring study of a shadow revision.
Promotion and retirement require current text/salt, sound frozen assignments and
the existing sequential/AIPW evidence rules. Arena preferences are review labels,
not causal proof; DPO export does not automatically train a model.
Offline replay priors change eligible shadow-candidate evaluation order by at most
one percent. They cannot bypass relevance admission, causal evidence or review.
Preference updates use indexed latest-revision pointers rather than scanning history.

Erasure revokes eligibility before removing fact/event history, postings, FTS,
content-hash vectors, derived control revisions, graph references, private captures
and pending receipts. Local SQLite uses secure deletion and checkpoint/vacuum;
tests inspect database bytes for erased phrases. Owners must separately erase
external backups/mirrors. Hub vector cleanup failures remain explicitly pending in a tenant-pinned durable
retry ledger; an available vector worker must drain it before claiming complete erasure.
Tenant advisory locks prevent a delayed retry from deleting a reused identifier. Retention reaping requires a deadline and reason; telemetry defaults off.

## Graphify and cleanup

All nine requested repositories and Graphify were cloned. Graphify 0.9.84 was
installed in an isolated environment and used for code-only AST extraction,
clustering, integrity checks and implementation-path queries across every code
repository. Competitor commits/counts are pinned in
[measurements/graphify-audit.json](measurements/graphify-audit.json); no competitor
source was vendored and no competitor accuracy claim was reproduced. Final
CommonTrace graph counts/digest are in the signed aggregate report. Generated
graphs and private query transcripts stay outside the repository.

Static reachability cannot establish dead code in a callback-based package. The
Graphify CI audit reports candidates and fails on dangling endpoints; it does not
delete exports or dynamic roots. This pass removed an unused joint-judge prompt,
merged duplicate recipe ordering, reused the existing vector cache, removed the
comment-only CodeQL config and its workflow reference, and replaced duplicate P0
design/reproduction files with this guide and the root reproducer. Historical P0
failure measurements remain available. No query suppression or CodeQL dismissal
was performed: GitHub's alert API returned HTTP 403 for this integration.

The continuation consolidates 29 command wrapper modules into three domains,
preserving all 69 command names and command behavior. The package now has 296
Python modules and 25 primary deployment variables. Advanced settings share the
YAML file; the machine-readable configuration schema has 155 legacy alias entries,
24 of which overlap the primary list. This leaves 156 unique supported
environment-variable names. The primary documentation surface is below forty;
the compatibility inventory remains larger. Graphify CI
checks dangling edges, the 306-module ceiling, and the reviewed static reachability
baseline. Static candidates include dynamic SDKs, callbacks and reference scripts;
the audit does not establish that all such functions are dead code.

Imported actionable history claims now seed quarantined lessons only after their
quoted symbols match current repository files. Review rechecks the file hashes,
and later code changes withhold stale lessons. Split proposals contain distinct
replacement parts, each with independent causal evidence; promotion replaces the
target atomically after every part passes. A changed target version withholds the
replacement. The optional Lance backend builds HNSW and scalar indexes outside
search, includes unindexed writes, and uses a separate durable projection watermark.

## Measurements and limitations

The 2026-10-11 [P0 continuation audit](next-generation-p0-audit.md) measured
31.43 ms write p95 and 759 facts/s at one million facts on the current host,
missing both write gates. It stops phase advancement and retains the earlier
release results below. The runs are not a controlled hardware comparison.
The subsequent [write optimization](next-generation-p0-write-optimization.md)
measured 3.23 ms write p95 and 1,181 facts/s at one million facts. Single writes
pass; batched ingest still misses 2,000 facts/s and keeps phase advancement
stopped. Neither run exercises the hybrid/server or zero-reembedding restart gates.

The latest [bounded-FTS batch candidate](next-generation-p0-batch-projection.md)
measured 4.263 ms single-write p95 and 1,819 facts/s at 1M on the candidate
source. Single writes pass; batch ingest still misses the 2,000 facts/s gate.
The candidate and frozen `7e0a224` runs are separate source revisions, so no
controlled gain is claimed. The raw report and source hashes are retained in
`measurements/p0-batch-projection-candidate.json`.

A later detached-snapshot candidate measured 1.797 ms single-write p95 and
2,071.287 facts/s for one 1,000-fact batch at 1M, clearing both write
thresholds on the local synthetic workload. The batch result has a 3.6% margin;
there is no same-HEAD 1M baseline, so it is a measured candidate pass rather
than a causal comparison. See the [snapshot-reuse note](next-generation-f1-snapshot-reuse.md)
and [source-hashed 1M report](measurements/f1-snapshot-reuse-candidate-1m.json).

The measured storage source's application suite completed with 6 failures,
6,666 passes and 118 skips. Follow-up connection and isolation fixes pass 135
focused checks, with a separate full suite running. Its native transport fixture
delivered all 50 directives but measured 152.27 ms p95, missing <50 ms. The
million-fact timings above do not describe the follow-up source.

The [signed aggregate leaderboard](leaderboard/index.html) links public JSON
reports and pins its Ed25519 session public key. Signatures detect edits and
identify the session publisher; they do not certify scientific validity. The
storage report records the base commit and per-file source hashes. Raw trajectories,
private signing keys, large graphs and model weights are excluded.

The recorded release's 1M SQLite selective workload achieved **5.031 ms single-write p95**,
**3,052 facts/s** for one 1,000-fact batch, and **0.418 ms sparse overlap p95**.
All seven recorded storage source hashes match the release. The earlier combined
branch measured 1.253 ms and 3,417 facts/s; its raw report remains in Git history
at `0eaa1fd0e3674f827419f77a3bca8785d77fdad7`.
Sparse overlap is not hybrid search. The earlier P0 disk-full failure is retained
in [measurements/p0-storage.json](measurements/p0-storage.json); compact postings
resolved it in this measured run. Tests of PostgreSQL used a disposable PostgreSQL
16 instance with fsync/synchronous commit disabled for fixture speed; no server
performance or durability measurement is inferred.

Actual `BAAI/bge-small-en-v1.5` embeddings were measured on fourteen labelled
coding queries, five repetitions. The idle run had **1.0 precision and recall**,
**21.77 ms p95**, zero timeouts. A concurrent-validation run had **50.90 ms p95**,
four timeouts and 0.94 recall. Both reports are retained. The current 50-process
native transport fixture measured **29.75 ms p95**, with every critical directive
delivered, a fixed directive and no semantic model. Its separately reported broker
warmup took 46.05 ms. The [baseline transport report](measurements/injector-transport-baseline.json)
measured 52.50 ms p95 and missed the target. An intermediate rerun also failed to
deliver during initialization; the benchmark now distinguishes socket readiness
from warmup and reports withheld trials instead of discarding the run. Cold core
calls timed out in both semantic runs. Semantic timings exclude interpreter
startup/model download; transport timings include fresh Node process startup.
These fixtures do not establish production precision, cold-model latency or 1M hybrid speed.
Eleven adversarial poison/status cases delivered no poison; clean utility remained.

Validation: [source CI](https://github.com/varma61923/commontrace-v2/actions/runs/38110144760)
passed **all 32 jobs**. Python coverage: **6,483 passed, 193 skipped**, **82.29%**;
development-extra Python 3.12: **6,482 passed, 194 skipped**; PostgreSQL Hub:
**2,501 passed, 2 skipped**; core-only Python 3.10: **6,129 passed, 280 skipped**.
These full suites precede the final shared preference-counter fix, whose local,
hosted, delivery and framework regression checks passed **43 tests**, with six
optional skips. The local full Hub suite also passed 2,501 tests before that fix.
Native OpenAI/Microsoft middleware, Google ADK/Strands/AG2 tools, all five SDK
generators, Docker, browser contracts, strict typing, security and dependency
checks passed CI. TypeScript framework suite: **23 passed**. Graphify and supply-chain
workflows also passed. Optional packages/services remain skips. The evidence
publication receives its own final branch CI; the manifest identifies the tested
source revision rather than claiming a result for an as-yet uncommitted report.

No GPT-class reader/judge endpoint or coding-model credentials were configured.
LoCoMo >=0.85, LongMemEval-S >=0.90, BEAM 100K/1M/category completeness, real SWE
uplift >=5pp with positive sequential lower bound, measured harmful-retirement N
on real labeled runs, and lower extraction cost remain unverified. CodingLessonBench
now drives isolated per-task assignments through the production journal
reconciliation path; synthetic harmful and neutral fixtures verify that N is
reported only after a SOUND retirement transition. Multi-task causal estimates
now use a fixed target-weighted task-stratified estimator and a Bonferroni
simultaneous interval built from per-task anytime-valid bounds; fixture coverage
does not establish real SWE uplift. The real patch/test harness reports token usage, unpriced calls and
explicit relevance labels; it never fills missing measurements with synthetic
scores. One-million-record dense/hybrid latency,
first injected lesson from an empty store in two minutes, live-host coverage of
all adapters and complete local/hosted API parity are also outstanding. The primary
configuration surface now meets the count target; its legacy compatibility
inventory is explicitly retained rather than silently removed.

## Use and reproduce

```sh
python -m pip install -e '.[dev,attention,security,code]'
commontrace up --dest /path/to/agent-project --setup-only
commontrace up --dest /path/to/agent-project
# Existing stores can explicitly migrate/export/rebuild:
commontrace fact migrate --dest /path/to/store
commontrace fact export --dest /path/to/store --output /path/to/export.jsonl
commontrace fact rebuild --dest /path/to/store
OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 bash reproduce.sh --next-generation benchmark-results/next-generation
```

`commontrace.yaml` chooses local/team/server, embedding/reranking, model-purpose
routes, budget and explicit history exports. `evolve runtime history`, `learn`,
`shadow`, `evaluate`, `reconcile`, `arena`, `arena-vote` and `forget` expose the
review workflow; use `--help` for required actor/scope/session fields. Kimi needs
its generated config flag, Cline needs Hooks enabled, and Windsurf native hooks
capture only. Setup imports supported project exports and previews eligible
existing lessons; empty stores wait for independently evaluated evidence.

For model-backed quality, configure the documented `COMMONTRACE_LLM_*` reader and
judge settings and provide pinned datasets. Run `benchmarks.conversation_bench`
with `--answer --budget 1500 --strict-context-budget` and the actual tokenizer;
include official dataset/category sizes. For coding, provide isolated checkout
manifests and run `benchmarks.coding_lesson_bench --tasks TASKS.json --runner
'python -m benchmarks.coding_agent' --model ACTUAL_MODEL --occasions 2000 --output
RESULT.json`. Record independently labelled relevance and an actual retirement
intervention before claiming those gates. `bash reproduce.sh` without the new
flag retains the independent-process causal fixture check.
