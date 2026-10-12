# Bounded dense candidates for aggregate recall

`commontrace.recall._dense_facts` previously loaded every fact and scored the
whole corpus for each dense recall request. For current-time requests backed by
the Lance fact projection, recall now asks the projection for a bounded set of
nearest candidates and rechecks each candidate against the canonical fact
store. The query uses the configured model tag; a configured model path stays
on the exact cached-vector path because the projection is keyed by model tag.

Writes newer than the projection watermark are scored from their current
canonical rows and merged into the candidate set. Deleted, expired, forgotten,
out-of-scope, evidence-ineligible and lineage-revoked rows are filtered before
return. Historical `as_of` queries continue to use exact cached-vector scoring,
because the current projection only indexes active facts and cannot represent
superseded facts' historical windows. A missing or malformed watermark and an
unavailable projection retain the existing exact fallback.

The bounded candidate request is `max(200, min(1000, k * 16))`. As with any
bounded ANN candidate stage, this limits dense recall and is not a quality
guarantee. This change has correctness tests but no 1M end-to-end timing or
real-model recall measurement. The F1 acceptance harness measures
`search_recipes.search`, a separate production retrieval path.

Validate the implementation with:

```sh
python -m pytest tests/test_recall_projection.py tests/test_bootstrap_runtime.py \
  tests/test_multichannel_recall.py tests/test_hybrid_retrieval.py \
  tests/test_fact_vector_projection.py tests/test_fact_store.py -q
```

The focused integration group passes 8 tests; the broader retrieval and fact
store group passed 111 tests. Ruff and `git diff --check` pass on the changed
source and tests.
