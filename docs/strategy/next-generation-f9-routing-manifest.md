# F9 per-purpose model routing visibility

CommonTrace already supported per-purpose model overrides through project
`model_routes` and the `purpose()` context. Its runtime manifest, used by
background jobs and dream runs, listed only explicit `LLMRuntime(routes=...)`
configs. A project-configured model override could therefore affect a run
without appearing in its saved usage evidence.

The runtime now records both the configured purpose-to-model overrides and
the provider/model selected for each purpose that was attempted. The selected
route is captured after applying the existing precedence rules: an explicit
purpose route wins; otherwise the default route (or caller/provider config) is
used and a configured purpose model replaces its model name. Manifests still
exclude credentials, prompts, endpoint URLs, and template identifiers.

Graphiti's `TokenUsageTracker` is a useful adjacent pattern: it aggregates
input/output usage by prompt name. CommonTrace already carries the equivalent
purpose attribution into its session ledger, so this change closes the
reproducibility gap at the run-manifest boundary instead of adding a second
tracker. The Graphiti cache was not copied: its cache key omits provider and
generation scope, and its cache has no bounded retention policy. CommonTrace's
existing exact-prompt cache behavior is unchanged; this work does not expand
cache eligibility or retain prompt text. Keep that cache disabled for workloads
where locally retaining generated responses is not acceptable, and do not
interpret cached stochastic output as a fresh model generation.

Source comparison at Graphiti revision
[`964bb11d1e7020e3cc67fab244268b0736b6c219`](https://github.com/getzep/graphiti/tree/964bb11d1e7020e3cc67fab244268b0736b6c219):
[`TokenUsageTracker`](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/token_tracker.py#L55-L103),
[`LLMClient` cache-key and lookup flow](https://github.com/getzep/graphiti/blob/964bb11d1e7020e3cc67fab244268b0736b6c219/graphiti_core/llm_client/client.py#L164-L168).

Validation: `pytest -q tests/test_llm_runtime_routing.py`
