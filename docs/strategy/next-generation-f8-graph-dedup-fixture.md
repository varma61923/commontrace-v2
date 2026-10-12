# F8 graph dedup fixture expansion

This supplement expands the deterministic graph-quality fixture while keeping
the calibration decision in the [F8 threshold audit](next-generation-f8-threshold-calibration.md)
unchanged. It adds 18 labeled entity pairs to bring the entity fixture from 22
to 40 pairs, plus 10 edge-pair scenarios. Every adapted row has a `source_ref`
that resolves through the fixture's `sources` map to Graphiti commit
`964bb11d1e7020e3cc67fab244268b0736b6c219` and a repository path/pattern.
Contexts and rationales record why a name pair is or is not the same entity.

The additional entity cases cover abbreviations and person aliases, semantic
paraphrases, typo/plural near-duplicates, similar-but-distinct products, and
same-name homonyms whose context identifies different concepts. They draw on
Graphiti's node prompt examples for NYC/New York City, Java, and car/vehicle,
and its exact/fuzzy candidate-resolution design and short-name ambiguity tests.
New names and contexts are synthetic adaptations, not copied workload records.

The edge-pair rows label four duplicates, two explicit contradictions, and
four independent facts. They adapt Graphiti's identical-fact, role-change,
different-date, and progress-versus-ownership prompt examples, plus same-pair,
reverse-direction, cross-endpoint, and contradiction-candidate scenarios from
its edge dedup tests. Graphiti's `_pair_search_filter` and `_same_pair_edges`
in `graphiti_core/utils/maintenance/edge_operations.py` scope duplicate
candidates to the same endpoint pair in either orientation. Its
`graphiti_core/prompts/dedupe_edges.py` keeps duplicate and contradiction
labels separate, requiring explicit incompatibility before invalidation.

The CommonTrace evaluator now scores a deliberately narrower deterministic
path: same-pair normalized exact and high-precision lexical near-duplicates,
different endpoint pairs as independent, and the existing negation conflict
signal as a contradiction candidate. It preserves `unknown` for value changes
that require a judge and for pairs where a no-match does not establish
independence. On these ten rows, it resolves 5/10 (50% coverage): four
duplicates and one cross-endpoint independent case; all five resolved labels
agree with the fixture. Both explicit contradictions and three independent
date/progress cases remain unknown. A separate unit case verifies the existing
deterministic negation signal. This resolved-only accuracy is a tiny fixture
diagnostic, not a production estimate or evidence that unresolved labels are
correct.

The expanded entity set evaluates to TP=9, FP=5, FN=15, TN=11 for the current
exact-plus-Minhash/entropy path, and TP=15, FP=10, FN=9, TN=6 for legacy
trigram Jaccard at its report floor. In particular, an exact `Java` homonym
pair remains a lexical false positive, while paraphrases and aliases requiring
context remain unmeasured by the lexical path. These deterministic fixture
counts are not estimates of production quality and do not justify changing a
threshold. Embedding and LLM tiers remain unmeasured; there is still no
de-identified production sample, entity-grouped held-out partition, or judged
edge-resolution corpus. The edge-pair output is available from
`python -m benchmarks.graph_quality_eval --json`; its `edge_pair_resolution`
section includes per-row decisions, abstention coverage, and the
expected-versus-predicted matrix.
