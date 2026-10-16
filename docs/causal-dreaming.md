# Causal dreaming and learned retention

`commontrace dream --causal` reports an itemized budget weighted by historical
retrieval share times positive causal-gate effect. Negative estimates are listed
for quarantine review; hazards are excluded. The forecast and utility model are
heuristics, not confidence guarantees.

`retention.fit` trains a small stdlib logistic utility model on randomized AIPW
pseudo-outcomes, with pooled shrinkage for low-traffic items. `retention.evict` is
an explicit operator action using predicted value rather than age alone. Legal
holds and hazards always override eviction. Ordinary retrieval never calls it.

Propose a rewrite with `commontrace frontier rewrite-propose REQUEST.json`
(`{"slug", "body", "author"}`), then obtain an independent operator review with
`frontier rewrite-review` (`{"identity", "reviewer"}`); `frontier rewrite-status {}`
reports the budget and trials. Once reviewed, MCP `retrieve` randomizes the rewrite
against its immutable original on every occasion where the lesson is injected
(`retention.choose`, through the existing holdout logger), labels the item with
`rewrite.arm`, and records the rewrite revision in the retrieval receipt so an
incident replay reproduces the delivered text. Without an occasion id or a
running experiment the original is served: an untested rewrite is never
delivered unrandomized. Rewrite texts live in deletable content-addressed files;
the signed journal keeps only their digests, so erasure can remove them. Report outcomes with
`holdout_io.record_outcome` using the same occasion. `retention.selected` exposes
an overlay only when experiment and integrity evidence support HELPS; a stale
original or loss of support returns no overlay. A HURTS result rolls subsequent
trial choices back to the original. Keep an explicit randomized monitoring slice;
no new outcome information can appear without continued measurement.
