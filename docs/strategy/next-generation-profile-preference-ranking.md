# F6 preference-guided result ordering

The runtime turn previously returned decayed profile weights beside search
results without using those weights in the result ordering. Local and hosted
turns now apply a bounded preference rerank to the candidates that search
already returned.

Only dynamic records with `provenance.authored_by == "user"` participate.
Scope filtering happens before profile rows reach the reranker. Static and
custom profile text, assistant-authored text, and assertions containing
explicit negation words do not boost results. CommonTrace's existing half-life decay supplies each
effective weight. Matching profile terms then multiply a scored local result
by at most 1.2; hosted results without numeric scores can move by at most 20%
of their existing candidate window. The original result fields and scores stay
intact. This does not expand search, add query terms, or add anything to the
injected context, so profile preferences do not consume the memory budget.

Cognee's local retrieval code uses a bounded preference factor when a stored
`prefers` edge identifies a candidate. CommonTrace has no equivalent edge from
a preference to a fact, so this adaptation uses a conservative lexical match
against the bounded candidate set. Supermemory's current tools return profile
and search results together but do not apply profile weights to ranking.

`tests/test_profile_preference_ranking.py` checks weighted ordering, authorship
and scope isolation on the local turn. The hosted turn has a focused check in
`hub/tests/test_memory_records.py`. These are functional checks only: they do
not measure answer-quality uplift or ranking accuracy on a held-out dataset.
