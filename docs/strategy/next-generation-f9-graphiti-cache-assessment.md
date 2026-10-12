# F9 Graphiti cache and token-tracking assessment

**Decision (2026-10-12): no additional F9 code change.** The suggested
exact-prompt response cache and cache-aware usage accounting already exist in
CommonTrace, with stronger scope, privacy, and retention controls than the
Graphiti pattern inspected here.

Graphiti checkout `/root/Test/graphiti` is clean on `main` at
`964bb11d1e7020e3cc67fab244268b0736b6c219`. Its `LLMClient` makes caching an
opt-in constructor flag and uses a SQLite/JSON cache under `./llm_cache`.
`_get_cache_key()` hashes model plus serialized messages with MD5; it does not
include provider, endpoint, generation parameters, or tenant/group identity.
The cache itself has no TTL or size limit. A cache hit returns before the
provider-specific token tracker records usage. `TokenUsageTracker` is
thread-safe and aggregates input/output tokens and calls by prompt name, but
does not distinguish cached usage from provider usage.

CommonTrace already has the compatible core behavior: opt-in SQLite response
caching keyed by SHA-256 over the exact prompt, model, and structured namespace;
provider, endpoint, region, project, owner namespace, and credential digest
partition the key. Bedrock and Vertex calls bypass the cache without an
explicit owner namespace. The cache keeps raw prompt text out of its key,
restricts its DB file to the current user, bounds TTL/entries/bytes, and
coalesces concurrent identical calls. `LLMRuntime` records cache-hit tokens and
historical cost separately from fresh spend and attributes ledger rows by
purpose. Existing tests cover namespace separation, cloud identity gating,
bounded retention, coalescing, cached usage, and cached historical cost.

Copying Graphiti's key or unbounded cache would weaken these safeguards. Adding
another cache layer would duplicate the current one without a demonstrated
behavioral gap. Graphiti's prompt-name aggregation also does not add a missing
CommonTrace capability: CommonTrace already tracks usage by runtime purpose in
the session ledger. No benchmark or score was produced.

Source attribution:

- [Graphiti LLM client and cache key at inspected revision](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/client.py#L75-L92)
- [Graphiti exact key construction](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/client.py#L164-L168)
- [Graphiti cache lookup/write and early return](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/client.py#L253-L281)
- [Graphiti SQLite/JSON cache](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/cache.py#L27-L61)
- [Graphiti token usage tracker](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/token_tracker.py#L55-L103)
- CommonTrace counterparts: `commontrace/llm_cache.py`, `commontrace/llm.py`, `commontrace/llm_runtime.py`, and `tests/test_llm_cache_safety.py` / `tests/test_llm_runtime_accounting.py`.

Focused verification of the existing CommonTrace behavior:

```bash
pytest -q tests/test_llm_cache_safety.py tests/test_llm_runtime_accounting.py tests/test_runtime_cache.py
```

Result: 24 passed. The provider/namespace partition test now checks stable
answer and token values while asserting the expected miss-versus-hit metadata,
which differs by design and is consumed by runtime cost accounting.
