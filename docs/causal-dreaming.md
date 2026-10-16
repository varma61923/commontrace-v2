# Causal dreaming and learned retention

`commontrace dream --causal` reports an itemized budget weighted by historical
retrieval share times positive causal-gate effect. Negative estimates are listed
for quarantine review; hazards are excluded. The forecast and utility model are
heuristics, not confidence guarantees.

`retention.fit` trains a small stdlib logistic utility model on randomized AIPW
pseudo-outcomes, with pooled shrinkage for low-traffic items. `retention.evict` is
an explicit operator action using predicted value rather than age alone. Legal
holds and hazards always override eviction. Ordinary retrieval never calls it.

Propose a rewrite with `retention.propose`, then obtain an independent operator
review with `retention.review`. `retention.choose` assigns the rewrite versus its
immutable original through the existing holdout logger. Report outcomes with
`holdout_io.record_outcome` using the same occasion. `retention.selected` exposes
an overlay only when experiment and integrity evidence support HELPS; a stale
original or loss of support returns no overlay. A HURTS result rolls subsequent
trial choices back to the original. Keep an explicit randomized monitoring slice;
no new outcome information can appear without continued measurement.
