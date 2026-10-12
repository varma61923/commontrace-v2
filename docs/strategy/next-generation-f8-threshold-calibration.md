# F8 graph threshold calibration audit

## Decision

Do not change the entity-resolution or classification defaults from the current
hand-labeled fixture. It is useful as a deterministic regression corpus and a
diagnostic sweep, but it cannot support a defensible train/held-out calibration
or a production-quality claim. No implementation threshold was changed.

## Evidence inspected

Graphify's current CommonTrace graph links
`benchmarks/graph_quality_eval.py::_entity_report` to
`entity_resolution.tiered_pair_signals`, and the evaluator's classification
report to `ontology_classify.classify`. It also keeps the legacy n-gram path
separate. The existing tests check exact metrics, determinism, and that both
positive and negative fixture labels exist; they do not check a held-out split.

`benchmarks/fixtures/graph_quality_v1.json` contains 22 entity pairs: 13
positive and 9 negative. The positives are 3 exact, 6 MinHash, 3 semantic-only,
and 1 exact-alias example. The negatives are 6 hard, 2 easy, and 1 near-negative
example. Several rows reuse the same entity names (for example, the Acme and
Meridian pairs), so a pair-level random split can put one entity's related
examples on both sides. Grouping rows that share an entity leaves only 18
entity-linked groups, with singleton strata such as exact-alias and
near-negative. A grouped holdout would therefore be too small and too sparse to
choose among the entropy, MinHash, and Jaccard thresholds reliably.

At the current tiered defaults, the fixture reports TP=8, FP=1, FN=5, TN=8.
Lowering only the MinHash floor from 0.80 to 0.50 changes that in-sample result
to TP=9, FP=1, FN=4, TN=8; the entropy sweep from 1.0 through 3.0 makes no
difference, as does the Jaccard sweep from 0.50 through 0.80. This is a
candidate for a future hypothesis, not a held-out result. The legacy trigram
path's report floor of 0.60 gives TP=11, FP=4, FN=2, TN=5, while its apply
floor of 0.85 gives TP=8, FP=1, FN=5, TN=8. These are scores on the same
hand-authored pairs used to inspect the thresholds.

The classification fixture has 18 examples: 14 typed labels and 4 unknowns.
It covers service four times; organization, person, and place twice each; and
event, document, tool, and error once each. Its keyword result is 18/18 at the
current 0.60 threshold, and remains 18/18 through 0.75. At 0.90 it misses three
typed examples; at 0.95 it misses all 14. These examples explicitly contain
the ontology cue words that the classifier scores at fixed levels (0.70,
0.75, 0.90, or 0.95), so the sweep mostly demonstrates those score bands. It
does not measure ambiguous production mentions or robustness across entity
types. The embedding threshold has no labeled embedding scores, and no LLM
judgments are present.

The fixture's relation cases contain 15 short authored sentences and 10 gold
triples. They can measure the current pattern extractor on those examples, but
there is no learned relation threshold to calibrate and the examples are not a
natural-language sample.

## Competitor comparison

The checked-out Graphiti source is commit
`964bb11d1e7020e3cc67fab244268b0736b6c219`. Its
`graphiti_core/utils/maintenance/dedup_helpers.py` uses exact normalized-name
matching first, then an entropy/length gate, MinHash LSH candidate buckets,
and a character-shingle Jaccard floor of 0.90; candidates not resolved by
those deterministic steps remain available to its LLM resolution path. Its
entropy floor is 1.5, name-length guard is 6 characters or 2 tokens, and its
MinHash uses 32 permutations with 4-value bands.

Those values describe a different candidate-generation pipeline. CommonTrace's
tiered path computes exact aliases, a 128-permutation overlap estimate and
trigram Jaccard before optional cosine and LLM stages. Graphiti's constants are
useful comparison points, but they are not labeled outcomes for CommonTrace's
signals. The inspected Graphiti entity-type flow is prompt-driven; it does not
provide a comparable labeled calibration set for CommonTrace's keyword
classification threshold.

## Evidence needed before tuning

Collect a larger corpus sampled from real, de-identified graph workloads. Keep
the source/workload and entity identity groups so examples cannot leak across
partitions. Include hard non-duplicates, aliases, short names, context-sensitive
cases, and ambiguous/unknown classifications. Record candidate scores for each
active signal; collect judgments for embedding and LLM paths separately. Select
thresholds only on a training partition, then report the frozen selection on
an entity-grouped held-out partition with per-stratum precision and recall.
Until that evidence exists, retain all current defaults and treat the fixture
sweeps as regression diagnostics only.
