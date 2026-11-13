# Governed model KV capsules (G19)

A capsule stores opaque key/value bytes for one admitted memory and one exact model fingerprint. Version 2 keys bind the source ID, canonical revision, memory-content digest and model digest with HMAC-SHA256 using the store's tenant identity key. Identical text under another source cannot retrieve that capsule. Separate tenant stores produce unrelated keys. Building requires the source to pass ordinary memory admission and a caller-supplied promotion score to meet the configured threshold; this threshold is a product policy input, not a statistical certificate.

The reference tiers are process memory and owner-only local disk. The memory tier retains at most 128 entries and 256 MiB of payloads by default, evicting the least recently read entry; `MemoryPool(max_entries=..., max_bytes=...)` configures those limits. Memory residency is ephemeral: misses fall through to another configured tier. `PoolAdapter` and `Compressor` are injection points for deployment-owned LMCache, Dynamo KVBM, Mooncake or compression implementations; `unimem[kv]` installs no inference engine or network client. Adapter names are stable version identities and cannot be rebound to another instance in a running process. Pool deletion must be idempotent, including after a put whose response was lost. Compression adapters must enforce their allocation and timeout budgets; the core also checks compressed and decompressed result types and sizes. CacheBlend-style partial recompute is adapter-owned.

`unimem tier capsules build|status|evict` manages capsules. Registration persists `staging` ownership before adapter writes and activates only after they succeed and the source is revalidated. Identical active build retries return the committed record. Interrupted registration remains discoverable; evict it before rebuilding. Evicted capsules can be rebuilt, including after a memory-only cache miss. Historical active rows cannot override a newer eviction.

Retrieval revalidates source admission, revision and publication after adapter I/O and verifies the exact model and compressed digest. Pool reads run concurrently; a changed journal checkpoint triggers a publication check so an in-flight read cannot return a revoked or replaced capsule. Eviction first records `eviction_pending`, blocking reads before deleting any tier. Failed deletions remain pending and can be retried with `evict`; canonical erasure receipts include attempted, removed and failed tiers. Successful removal records `evicted`. External adapter residue still requires operator remediation when deletion fails. Canonical erasure and capsule writes share the same store fence.

Version 1 content-only keys are no longer served. Existing journal rows remain available for eviction and cleanup; evict and rebuild those capsules under version 2 before using them after upgrade.

Authenticated journal checkpoints key a bounded metadata cache (four snapshots,
32 MiB, 30-second retention), avoiding full history scans on warm reads. Lifecycle
writes select a new snapshot; returned build metadata is copied so callers cannot
alter cached routing state.

Capsules are an optimization for open-weight serving paths with an adapter that exposes KV. Closed model APIs do not expose their internal KV and are outside this feature's scope. The local disk reference backend relies on store filesystem permissions and does not claim encryption at rest.
