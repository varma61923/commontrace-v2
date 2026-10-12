# Hosted hybrid record route

The live Hub route currently opens a lexical-only journal. The next path uses
the same versioned record API and adds server-owned query embedding and a
durable vector projection; clients cannot select a provider, tenant, namespace
or model revision in request bodies.

Every canonical record write and cascading erase must append an ordered
tenant change in the same transaction. The worker reuses the existing fenced
snapshot backend, caches document vectors by content and full projection
identity, and acknowledges only published changes. Queries pin the acknowledged
generation and hydrate only canonical rows whose change sequence is no later
than that generation, then repeat scope, lifecycle and grounding checks.
An interrupted publication is replayable; an optional dense failure preserves
the existing lexical arm. Generic fact envelopes remain generic; the explicit
AtomicFact bridge preserves the complete local payload.

The default server factory uses the existing local embedding provider, with
its actual immutable model commit and the adapter revision in the vector
identity. Missing models, missing pgvector or unavailable providers fall back
to lexical retrieval. Custom providers must supply their own revision-bound
identity through the same projection interface. Pools are bounded and closed
with the server. The first correct implementation drains a bounded batch on
read and holds the tenant write lock during publication; this is a documented
latency risk to measure, not a claim that the server gate passes.

Tests are written before route integration. They exercise the actual Hub HTTP
route with disposable PostgreSQL canonical storage, the existing real SQLite
snapshot engine and deterministic embeddings. PostgreSQL vector/HNSW and real
model quality require separate validation. Competitor source and transactional
outbox attribution are recorded in the stream and projection design notes.

The hosted route now reuses `EvidenceResolver` for marked AtomicFacts. It loads
only the bounded same-tenant, visible fact dependency closure, checks current
claim revisions, recursively validates cited fact evidence and rejects live
refutation. Hosted lesson receipts remain unavailable until a typed hosted
lesson mapping exists; a local filesystem lesson cannot authorize server data.
Evidence citations also participate in cascading forget, so source withdrawal
redacts dependent event snapshots and erases retained vectors, including through
old reader pins.

The route, records, REST, embedder and startup slice passed **83 tests in
83.10 s** against an owned, disposable PostgreSQL 16.15 database. After the
last evidence-scope check was added, the current route suite separately passed
**13 tests in 15.75 s with no skips** against actual PostgreSQL/pgvector 0.8.2,
using SQLite and PostgreSQL snapshot implementations with deterministic
embeddings. The isolated server-image source/import checks passed **11 tests
in 12.40 s**. Tests cover dense-only
recall, tenant/user isolation, post-snapshot edits, lexical fallback, stale and
refuted evidence, evidence-source erasure, and removal through an old pinned
snapshot. The actual local model package and weights were not present in the Hub
requirements environment, so these tests do not measure model output or latency.
The factory attempts the configured local BGE-small provider and falls back to
lexical search when its optional package, pinned model revision, pgvector or
runtime is unavailable. A 1M server benchmark and model-judged retrieval remain
open. A packaging audit compared Cognee's CPU-only Torch installation with
Hindsight's build-time BGE-small preload. The official `torch==2.10.0+cpu`
AMD64 wheel request returned HTTP 403 (error 1010) in this environment; the
pinned BGE-small weights were reachable at 133,466,304 bytes. The Hub dependency
lock and image were left unchanged until the complete CPU-only install can be
validated, so the model remains optional in the shipped server environment.
