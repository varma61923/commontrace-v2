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
scope. Legacy record payloads and causal conformance vectors remain compatible.

The Hub record engine uses a transactional PostgreSQL event/projection ledger,
full-text candidates, canonical validation of optional owner-pinned pgvector/HNSW
hits and migration-installed, forced tenant RLS. The same minimal stdlib envelope
and visibility rules are used in the server image. Hub record CRUD does not imply
that every legacy Gateway operation has been ported to the Hub.

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

The simplification target is still unmet: the last measured surface had 322
Python modules, 154 documented environment variables, 69 top-level commands and
63 Gateway operations. The recommended profile has nine settings, but that does
not make the legacy documented surface smaller than forty.

## Measurements and limitations

The [signed aggregate leaderboard](leaderboard/index.html) links public JSON
reports and pins its Ed25519 session public key. Signatures detect edits and
identify the session publisher; they do not certify scientific validity. The
storage report records the base commit and per-file source hashes. Raw trajectories,
private signing keys, large graphs and model weights are excluded.

The measured 1M SQLite selective workload achieved **1.093 ms single-write p95**,
**2,533 facts/s** for one 1,000-fact batch, and **0.252 ms sparse overlap p95**.
Sparse overlap is not hybrid search. The earlier P0 disk-full failure is retained
in [measurements/p0-storage.json](measurements/p0-storage.json); compact postings
resolved it in this measured run. Tests of PostgreSQL used a disposable PostgreSQL
16 instance with fsync/synchronous commit disabled for fixture speed; no server
performance or durability measurement is inferred.

Actual `BAAI/bge-small-en-v1.5` embeddings were measured on fourteen labelled
coding queries, five repetitions. The idle run had **1.0 precision and recall**,
**20.50 ms p95**, zero timeouts. A concurrent-validation run had **50.90 ms p95**,
four timeouts and 0.94 recall. Both reports are retained. Cold core calls timed
out in both runs. These small fixtures exclude interpreter startup/model download
and do not establish production precision, cold host latency or 1M hybrid speed.
Eleven adversarial poison/status cases delivered no poison; clean utility remained.

Validation: full Python suite **6,652 passed, 113 skipped**, **84.40% coverage**;
full PostgreSQL Hub suite **2,495 passed, 2 skipped**. Subsequent focused checks
cover source-version freshness, optional-provider failure and SDK callbacks.
Isolated native SDK checks: **19 passed, 5 skipped**; unavailable optional SDKs
remain skips. TypeScript suite: **23 passed**. Ruff, the 35-file strict typing
list, medium/high Bandit, declared dependency audits, generated references and
OpenAPI checks passed locally. Final branch CI is a separate result.

No GPT-class reader/judge endpoint or coding-model credentials were configured.
LoCoMo >=0.85, LongMemEval-S >=0.90, BEAM 100K/1M/category completeness, real SWE
uplift >=5pp with positive sequential lower bound, harmful-retirement N and lower
extraction cost remain unverified. The real patch/test harness reports token
usage, unpriced calls and explicit relevance labels; it never fills missing
measurements with synthetic scores. One-million-record dense/hybrid latency,
first injected lesson from an empty store in two minutes, live-host coverage of
all adapters and the module/config-count gate are also outstanding.

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
