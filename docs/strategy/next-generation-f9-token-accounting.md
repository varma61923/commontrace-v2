# F9: per-purpose token accounting

The next-generation prompt names per-prompt token accounting as an F9 gap. The local Graphiti implementation tracks input and output totals separately for each prompt name in `graphiti_core/llm_client/token_tracker.py` (`PromptTokenUsage` and `TokenUsageTracker.record`). Its Graphify graph connects that tracker to `LLMClient` and its prompt-level tests.

CommonTrace already routes calls by purpose in `commontrace/llm_runtime.py`, optionally caches completions by prompt hash in `commontrace/llm_cache.py`, and writes each runtime call to the session ledger with its purpose as `occasion`. Ledger entries already retain prompt and completion token counts. The missing part was aggregation: `session_summary()` grouped calls by occasion but exposed only combined `total_tokens`, so callers could not distinguish input from output for an individual purpose.

Session summaries expose `prompt_tokens` and `completion_tokens` for each purpose, model, and overall session. Runtime completions now mark opt-in cache hits in the returned usage metadata. The runtime keeps those tokens under the token budget, counts their historical model cost separately, and charges zero fresh spend for the replay. The cache retains the price estimate from the time it stored the result, and the ledger reports `cache_hit_count`, `cached_tokens`, and `historical_cached_cost_usd` alongside current spend.

An unpriced provider call no longer looks like a known zero-cost call in summaries: `total_cost_usd` becomes `null`, with `known_cost_usd` and `unpriced_calls` showing the known subtotal and gap. The CLI prints that state without treating it as a number. Cache entries with unknown historical prices keep current spend known at zero while reporting `historical_cached_cost_usd: null` and `cached_unpriced_calls`.

This records provider-reported token usage and estimates cost only from the owner-supplied price table. It does not reconcile against provider invoices, and existing ledger rows without cost metadata retain their previous known-cost interpretation. The caller-supplied `occasion` remains a purpose label; prompt text and template identifiers are not stored.

Validation: `tests/test_llm_runtime_accounting.py`, `tests/test_session_ledger.py`, `tests/test_competitor_features.py::TestLLMCache`, and `tests/test_competitor_features.py::TestSessionLedgerAttribution` (11 passed).
