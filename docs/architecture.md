# Memory architecture

CommonTrace has separate local memory and fleet-sharing services. They share
protocol evidence contracts, not a single global database or configuration.

```mermaid
flowchart TD
    Agent[Agent / CLI / MCP client] --> Gateway[Gateway application]
    Gateway --> Transport[HTTP or JSON-line transport]
    Gateway --> Lessons[Approved lesson repository]
    Agent --> Conversations[Conversation Store: SQLite per space]
    Conversations --> Raw[Sessions and raw turns]
    Raw --> Units[Source-backed retrieval units]
    Raw --> Facts[Facts and historical belief chains]
    Raw --> Entities[Entities and session summaries]
    Lessons --> Hybrid[Lexical / optional dense retrieval]
    Units --> Search[FTS5 / fallback BM25 / optional vectors]
    Facts --> Search
    Entities --> Search
    Hybrid --> Evidence[Bounded evidence selection]
    Search --> Evidence
    Evidence --> Agent
    Agent --> Hub[Authenticated Hub MCP]
    Hub --> Postgres[Org-scoped PostgreSQL / RLS]
    Postgres --> Audit[Audit, quotas and causal measurements]
```

`gateway_transport.py` handles framing/TLS/socket behavior; `gateway.py` owns
routes and authentication. Public gateway factories remain compatible facades.
Conversation `Store` owns SQLite persistence/migrations and source references;
`frontmatter` centralizes Markdown/YAML persistence and locked atomic writes.
`HubConfig`, `GatewayConfig` and `RetrievalConfig` are deliberate deployment/store
boundaries. Retrieval settings are shared by all retrievers for a store.

Raw evidence stays authoritative. Facts link to turns; historical facts remain
accessible after supersession. Scheduled changes must not become current early.
Graph/vector caches are accelerators with source-identity checks and exact
fallbacks. Similarity alone does not establish evidence sufficiency or truth.

Caches retain bounded generations. Corpus cache misses share builds; source
fingerprints still compare exactly. File edits, replacement, process forks and
failed builds must not leave stale results or inherited locked state. Cache
budgets bound retained objects, not all active requests or database native RAM.

Hub tools authorize scope and person capability, then execute org-scoped
transactions. Batch tools reuse single-item handlers sequentially, with ordered
outcomes and independent commits. They do not share an AsyncSession across
concurrent tasks. An ambiguous write response requires idempotency before retry.

For protocol details see [PROTOCOL.md](../protocol/PROTOCOL.md); for measured
constraints and source revisions see [performance.md](performance.md).
