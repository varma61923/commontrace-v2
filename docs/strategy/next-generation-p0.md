# P0: incremental local fact storage

The attached next-generation brief is the acceptance contract. This phase keeps
existing JSONL stores unchanged until an explicit `commontrace fact migrate`.
Migration activates SQLite WAL, an append-only transactional event ledger, and
rebuildable fact, statement, contradiction, lexical and FTS5 projections. The
existing governed fact APIs remain the only mutation pipeline. A lazy mapping
loads only touched facts; indexed candidate lookup replaces `_StatementIndex`
reconstruction. Event payloads use an additive versioned memory envelope.

Migration must preserve normalized facts, report both source and normalized
checksums, reject malformed input rather than silently losing it, and leave the
original JSONL intact. JSONL export is explicit after activation; editing the old
file does not change SQLite. SQLite readers use a consistent transaction and
revalidate evidence and lineage. JSONL ranking contracts remain unchanged.

Tests precede implementation: transaction rollback, process concurrency, migration
checksum and failure, projection rebuild, exact/near duplicate and contradiction
parity, governed retrieval, historical filters and compatibility exports.

Measure actual `hierarchical.add_fact` and `add_facts` (including governance and
entity linking) at one million records. Report sparse retrieval separately from
hybrid retrieval. Do not claim server performance, dense quality, accuracy or
causal uplift without reproducing those gates. Stop after this phase if any P0
gate remains missed or unverified, as required by the brief.

## Operational use

```sh
commontrace fact migrate --dest /path/to/store
commontrace fact add 'the project uses a durable journal' --dest /path/to/store
commontrace fact search 'journal' --dest /path/to/store
commontrace fact export --dest /path/to/store --output /path/to/export.jsonl
commontrace fact rebuild --dest /path/to/store
```

No new dependencies or configuration variables are required. Before migration,
JSONL remains authoritative. After migration, the original JSONL is a preserved
backup; export explicitly to refresh a portable copy. Copy the SQLite database
with SQLite's backup API, or stop writers and copy after a WAL checkpoint. Do not
copy the main database file alone while a writer is active. Schema version 1 is
local-only; the existing hosted APIs are unchanged. Direct SQLite edits bypass
the ledger and are unsupported; `rebuild` restores committed state.

The FTS5 projection and deterministic lexical postings are maintained
incrementally. Existing overlap and BM25 score contracts are preserved. BM25's
scope-filtered corpus statistics still scan the corpus; dense fusion remains the
existing opt-in path. This is not hybrid-by-default retrieval. Broad queries,
full listing, bulk replacement, evidence binding through the legacy helper and
source retirement may still materialize the bank. The million-record timings
use selective synthetic queries and unbound journal facts; they do not cover
those operations or stores with large entity or evidence ledgers.

An empty terminal validity interval created by a same-instant contradiction
resolution is now preserved on reload for lineage. It remains ineligible at
all valid-time instants. Source/evidence currency, scope isolation and forgetting
are checked at retrieval, not cached as proof eligibility.

The server fact tier, automatic hook kit, learner expansion, default dense
models, profiles, simplification and public leaderboard are still outstanding.
They are not claimed implemented by this branch. CodeQL review context is in
`.github/codeql/codeql-config.yml`; dismissal was blocked by GitHub HTTP 403.
