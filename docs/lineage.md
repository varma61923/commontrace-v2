# Authenticated lineage

New origin-bound records and lesson writes create signed in-toto-style derivation
attestations in `memory/lineage.jsonl`. Existing record hashes and lesson revisions
are unchanged. Each input carries its content digest; missing lineage has unknown
integrity. Summarizing, echoing or deriving from an external input cannot raise it.

`commontrace lineage show ID --dest STORE` explains ancestors, descendants and
review flags. `lineage taint ID --reason REASON` propagates a review requirement;
`lineage invalidate ID` marks supersession. Fact supersession triggers this
automatically. Retrieval facts include integrity and lineage digest metadata.

The corroboration API requires disjoint authenticated root origins with measured
causal source reputation. Operators must provision genuinely independent origins.
Distinct self-created accounts are not independence. Missing reputation cannot
raise trust. Legacy unsigned objects remain unknown, never implicitly reviewed.

Attestations authenticate claims, not semantic truth or actual model execution.
Local signing keys and filesystem access are trusted. Traversal refuses graphs
above 2000 nodes and fan-in above 256 rather than returning an incomplete trust
answer. Rotate journals before the 100000-row bound.
