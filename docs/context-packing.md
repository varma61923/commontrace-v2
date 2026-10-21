# Value per token and proven cache prefixes

`unimem retrieval --value-per-token` snapshots the causal gate's posterior
uplifts outside the retrieval hot path. Admission then uses bounded knapsack
selection with duplicate adjustment and a relevance-baseline fallback under the
same estimated-value objective. Refresh the snapshot after material evidence or
context changes. Posterior utility is an estimate, not realized task improvement.

The planner caps work at 500000 transitions/30000 states and reports capping.
Token weights use an explicit four-character estimate; providers may supply their
own exact tokenization upstream. Mandatory hazards bypass optional budgets.
Defaults continue using relevance order until this configuration is enabled.

MCP returns `context_packing` and a deterministic `cache_prefix` for freshly
approved, low-churn lessons with positive evidence. Prefix bytes are stable when
source bytes are unchanged, and versions are scoped to the store. Place the
prefix before the provider cache breakpoint and volatile memories after it.
`compression.cache_prefix` accepts deployment-supplied cached/uncached prices and
latency rates. Missing pricing produces no fabricated dollar savings. `KVReuse`
is the optional self-hosted interface behind `unimem[kv-cache]`; actual KV
engines are provider plugins.
