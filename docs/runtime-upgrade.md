# Runtime, security and provider improvements

This change follows a targeted review of all eight requested reference projects
and the attached `HANDOVER_PROMPT.md`. It improves concrete gaps in CommonTrace's
existing implementation. It does not establish superiority on answer quality,
certify security, or make every operation 50 times faster.

CommonTrace's comparison baseline is `bf16603eddc6b220efcab95ce4a6912a88e79318`.
Reference repositories were cloned with their histories, inspected as references,
and left unchanged. This review is focused rather than a line-by-line audit of
all nine repositories. Implementations here are original stdlib-based code;
competitor source was not vendored into the package.

## Verified reference patterns

| Reference | Inspected source | Decision in CommonTrace |
| --- | --- | --- |
| Graphiti | `graphiti_core/llm_client/cache.py`, `graphiti_core/helpers.py` | Keep JSON-only durable completions; explicitly close per-operation SQLite connections; bound submission as well as execution. |
| Cognee | `cognee/infrastructure/databases/utils/closing_lru_cache.py:317` | Resource lifetime must outlive users of a cache entry. Avoid retaining open SQLite connections; weak corpus identities avoid keeping evicted indexes alive. A leased adapter cache would be unnecessary for these immutable values. |
| Mem0 | `mem0/memory/main.py:144`, `mem0/utils/scoring.py` | Protect configured identity/filter scope, remove API keys from configuration repr, preserve existing lexical eligibility before dynamic adjustments. |
| EverOS | `src/everos/core/persistence/markdown/path_safety.py` | Preserve existing store/path contracts; use private temporary credentials, reject token symlinks/hardlinks, and replace credentials atomically. No new lossy naming convention is imposed on existing memory. |
| Hindsight | `hindsight-api-slim/hindsight_api/engine/bank_info_cache.py` | Bound TTL/LRU retention, coalesce misses, and include the full scope in a cache key. Retrieval uses exact corpus identity; authentication rechecks credential file identity on each request. |
| Zep | `integrations/langgraph/python/src/zep_langgraph/tools.py`, `provisioning.py` | Pin configured identity parameters. Serialize initial token creation across processes rather than relying on check-then-write. |
| Letta Code | `src/websocket/listener/device-status-cache.ts` | Tie cache identity to the owning resource. Weak index keys do not extend corpus lifetime. |
| Supermemory | `packages/tools/src/shared/cache.ts` | Cache repeated memory calls with complete namespace separation. Use structured key encoding rather than delimiter concatenation. |

## What changed

### Retrieval and concurrency

`retrieval.rank_lessons` caches immutable numeric top-k templates for plain
lexical requests. The key includes a weak identity of the exact corpus index,
normalized query terms, scorer, top-k, floor and adaptive-tail configuration.
It retains at most 256 entries, 8 MiB by conservative accounting and 30 seconds.
Each call returns new records and new matched-term lists, using current metadata
for the returned document positions. Filters and source generations continue to
select their own corpus indexes; the cache does not include lesson bodies,
causal holdout assignments, prompt rendering or authorization decisions.

Cache admission has a measurable cost for distinct queries. Predominantly unique
workloads can set `COMMONTRACE_QUERY_CACHE=0`, or pass `cache_results=False` to
`rank_lessons`, while still benefiting from the existing corpus index cache.

Any nonempty reliability, recency or graph lookup bypasses result retention, so
mutable scoring signals are evaluated each time. Fresh indexes built from
unstamped caller data cannot hit a prior index's result cache. Existing index,
lesson, temporal and source consistency checks remain in place.

The reusable `RuntimeCache` bounds count/bytes/TTL, loads outside its lock,
coalesces same-key misses, propagates loader failures to waiting callers, and
prevents pending work from republishing after invalidation. Forked children
reset locks, entries and pending flights. Retention budgets do not bound the
memory of active callers or distinct in-flight computations.

`bounded_map` now consumes lazy input incrementally and retains at most
`max_pending` submitted futures (default twice `max_workers`), preserving input
order. It stops consuming after an observed input/worker failure, cancels queued
work, and waits for running workers. The returned result list still grows with
the number of results; a streaming output API is not added by this change.

### Completion cache and provider recovery

Completion keys use structured SHA-256 encoding and include provider, model,
endpoint, region, project, explicit owner namespace and a credential digest.
`Config.api_key` is excluded from repr. Set
`COMMONTRACE_LLM_CACHE_NAMESPACE` to separate tenants that use the same API key.
Bedrock/Vertex calls bypass completion caching unless an explicit namespace is
configured, because IAM/ADC identity cannot be inferred safely from model/region.
Applications constructing `Config` directly use its `cache_namespace` field.

The cache defaults to `$XDG_CACHE_HOME/commontrace/llm-cache.db`, falling back to
`~/.cache/commontrace/llm-cache.db`. The previous shared temporary database is
not migrated. An explicitly configured database uses the new scoped keys; old
unscoped rows are misses and are pruned on writes. Legacy metadata upgrades
transactionally. Database files are private, regular files owned by the current
user; symlinks and hardlinks are rejected. Connections stay in their calling
thread and close immediately. SQLite errors degrade to provider calls.

Durable entries expire after 24 hours, retain at most 10,000 rows and 64 MiB of
live serialized payload, and reject values above 1 MiB. SQLite pages, free pages,
indexes and WAL add overhead to this live-payload budget. No encryption at rest
is introduced. The in-process layer retains at most 256 entries / 8 MiB / 300
seconds and respects original disk expiry. Concurrent identical calls share a
provider call within one process; cross-process billing coalescing is not
implemented. Hot JSON deserialization gives every caller independent mutable
objects. Public writes invalidate the local hot entry; writes by other processes
become visible when its hot entry expires.

Text completions now use account/provider-scoped circuit breakers. Five
consecutive transient failures open a circuit for 30 seconds. The retries already
performed by an HTTP provider count as one failed logical call. One recovery
probe is admitted after cooldown. Generation fencing prevents older requests
from closing a newly opened circuit. Permanent failures do not trip the breaker;
existing successful cache hits remain usable. Breaker retention is bounded to
128 provider contexts for one hour. Set `COMMONTRACE_LLM_CIRCUIT_BREAKER=0` to
disable it. This breaker is process-local and does not wrap vision completions.

### Gateway credential lifecycle

New `memory/gateway.token` files contain a single versioned JSON record. Legacy
plaintext bearer files remain readable. Credential creation uses the existing
cross-process file lock, private temporary files, fsync and atomic replacement.
File-backed gateways check strong file identity on every authenticated request,
reload replacements, and enforce expiry on every check. Malformed, inaccessible,
missing and revoked credentials fail closed.

```bash
# Start with a one-hour lifetime for a newly created token.
commontrace gateway --dest /path/to/store --token-ttl 3600

# Replace the credential of already running file-backed gateways; then exit.
commontrace gateway --dest /path/to/store --rotate-token --token-ttl 3600

# Revoke without issuing a new credential; then exit.
commontrace gateway --dest /path/to/store --revoke-token
```

Rotation immediately invalidates the previous file-backed token on subsequent
requests. It does not interrupt an already authorized in-flight operation.
Revocation remains in place across restart until an explicit rotation. Starting
with an expired token creates a new one; clients must receive the new credential.
`--token-ttl` does not change the lifetime of an existing live token: rotate it
when changing that policy. Direct `Gateway(..., token=...)` and `--token` remain
static credentials; supply a `token_provider` callable to an embedded gateway
when live credential control is needed.

Tokens are omitted from default startup logs. `--show-token` explicitly prints a
console URL containing the bearer credential; paired with `--rotate-token` it
prints the new token to stdout. To retrieve a new-format token through a local,
controlled channel, parse the JSON file's `token` field. Do not hand the whole
JSON record to the HTTP Authorization header.

### Secrets and adapter boundaries

`commontrace.secrets_provider.SecretProvider` supports explicit provider
injection. `env_secret` preserves file-over-environment precedence and adds
optional AWS Secrets Manager references. Configured-source failures raise
sanitized errors; they do not fall back to a stale environment value.

```bash
# Mounted file: reread whenever load_config resolves the key.
export COMMONTRACE_LLM_API_KEY_FILE=/run/secrets/llm-api-key

# Or opt into AWS Secrets Manager (requires the optional boto3 SDK).
export COMMONTRACE_LLM_API_KEY_AWS_SECRET_ID=production/commontrace/llm
export COMMONTRACE_LLM_API_KEY_AWS_SECRET_FIELD=api_key
export COMMONTRACE_LLM_API_KEY_AWS_REGION=us-east-1
```

`NAME_FILE` takes precedence over `NAME_AWS_SECRET_ID`, then `NAME` is the
fallback. AWS retrieves `AWSCURRENT`, coalesces reads, and refreshes after a
60-second TTL; injected providers expose immediate invalidation. The Hub's
`env_secret` import remains compatible. Install `commontrace[llm]` or `boto3`
when using AWS; core and normal Hub imports do not require that SDK. Vault or
another manager can implement the protocol and be injected explicitly.

A resolved secret cannot rotate a previously constructed immutable `Config` or
Hub process configuration. Reload that configuration at the appropriate service
boundary. HMAC pepper rotation still needs a deliberate old/new verification
and key-rehash migration; retrieving a new pepper is not that migration.

`MemoryAdapter` is a structural provider contract. `Mem0Adapter` copies configured
arguments and pins configured identity fields and filters. Runtime calls can
change options such as top-k, but cannot replace a configured tenant/user filter.
Provider-side mutation cannot alter the adapter's pinned configuration. Provider
authorization remains responsible for ownership checks on delete operations;
the adapter is not a substitute for an authenticated multi-tenant service.

## Disposition of handover proposals

| Proposal | Status |
| --- | --- |
| Query-result TTL caching | Implemented for exact plain lexical requests; dynamic signal requests bypass it. |
| Closing cache / safe resource lifecycle | Explicit SQLite closure and weak corpus ownership implemented; no open-resource LRU added. |
| Gateway token expiry, rotation, revocation | Implemented for file-backed gateways, with live reload and CLI controls. |
| Secret-manager integration | AWS and injectable providers implemented; HMAC pepper migration and automatic reload of held service config remain open. |
| Circuit breaker | Implemented for text completions; multi-process and vision breakers remain open. |
| Identity key protection | Configured Mem0 search scope pinned; existing Hub authentication/RLS remains authoritative. |
| Interface adapters / dependency injection | Structural memory/secret contracts and gateway token-provider injection implemented; a wholesale storage rewrite or service-container framework is not added. |
| Phased batches, hybrid scoring, cached indexes | Already present in substantial form. This change improves fan-out admission and preserves scoring quality instead of substituting arbitrary weights. |
| XSS / request limits | The handover's claim of no UI is outdated: CommonTrace already serves a console and has hardening/limit tests. Those protections are preserved. |
| HTTP pooling / universal async conversion | Not implemented here. These need transport-specific ownership and end-to-end measurement, not an untested wholesale rewrite. |
| Plugin framework / event bus / global strict typing | Remain architectural work. Explicit protocols avoid an unused global registry and do not require loading arbitrary plugin code. |
| Coverage metrics | Focused coverage report added to validation; no arbitrary repository-wide 80% gate is asserted. |

## Measurement and verification

[Performance evidence](performance.md) records baseline/candidate timings,
workloads and reproduction commands. Repeated
ranking at 10,000 lessons is the workload targeted at 50x; distinct queries,
file scans, cold indexing, LLM inference and end-to-end latency are separate.

Regressions cover all lexical scorers, cached/fresh parity, scoped stores,
filtered views, mutable signal lookups, result copies, weak index lifetime,
concurrent cache flights, cancellation, fork safety, token startup races,
live expiry/revocation, private files, SDK secret refresh, payload/row limits,
legacy migration and immediately closed connections. The five baseline core
failures were four MCP SDK-version mismatches plus a stub-encoder fixture that
did not enable the semantic arm on a core-only install; the fixture now enables
its injected local encoder, without downloading a model.

Full core/end-to-end, disposable-PostgreSQL Hub, lint and local-latency
checks run in CI. Generated benchmark and validation output is not kept in the
source tree.
Live cloud SDK authorization, Windows file locking, external model quality and
multi-machine throughput were not exercised.
