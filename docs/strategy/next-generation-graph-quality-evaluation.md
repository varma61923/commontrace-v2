# F5/F8 graph-quality baseline

The graph-quality thresholds previously had behavior tests, but no labeled
precision/recall evaluation. This adds a deterministic, hand-labeled fixture
and an offline evaluator for the legacy trigram candidate/apply floors, the
exact/MinHash entity cascade, keyword ontology classification, and the offline
relation-pattern extractor. It records a
diagnostic baseline; it does not tune defaults from this small synthetic set.

Reproduce the checked-in measurement from the repository root with:

```sh
python -m benchmarks.graph_quality_eval --json \
  --out docs/strategy/measurements/f5-f8-graph-quality-fixture.json
```

The fixture hash is `2c18d69c98d94bf13d225faf264e24a0a7e1d7a05cc14aa2e127afc1a54d7e3e`;
the harness hash for this run is
`f0682991d1765f7fe5166b7ea76180ccca43d9d2adfb5f9a0ae25a95df0470f2`.
The measured source hashes are recorded in the JSON artifact for
`entity_resolution.py`, `ontology_classify.py`, and `relation_extraction.py`.

| Evaluated path | Labeled examples | TP / FP / FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|
| Legacy n-gram candidates at report threshold 0.60 | 22 (13 duplicates) | 11 / 4 / 2 | 0.7333 | 0.8462 | 0.7857 |
| Legacy n-gram candidates at apply threshold 0.85 | 22 (13 duplicates) | 8 / 1 / 5 | 0.8889 | 0.6154 | 0.7273 |
| Exact + MinHash entity pairs | 22 (13 duplicates) | 8 / 1 / 5 | 0.8889 | 0.6154 | 0.7273 |
| Keyword ontology type classification | 18 (14 known, 4 unknown) | 14 / 0 / 0 | 1.0000 | 1.0000 | 1.0000 |
| Offline relation patterns | 15 sentences, 10 gold triples | 7 / 0 / 3 | 1.0000 | 0.7000 | 0.8235 |

These are fixture results only. Names and sentences are hand-authored, the set
is small, and its class proportions are not drawn from production traffic. The
legacy report floor favors recall on these pairs, while its stricter apply floor
trades recall for precision. That is diagnostic evidence about this fixture,
not enough to change either default.
Entity-pair errors include two typo aliases missed by the current MinHash and
trigram floors, three semantic-only aliases outside the lexical tiers, and one
hard negative accepted as a MinHash candidate. The fixture demonstrates why
embedding and LLM tiers matter; it does not measure their quality.

The current `tiered_candidates()` cascade is exact normalized forms, then a
character-entropy gate followed by MinHash agreement and trigram Jaccard, then
an optional embedder's cosine candidates (top 15 per entity at cosine 0.6),
then one batched LLM judgment for the remaining embedded candidates. Exact and
MinHash defaults are entropy 2.0, MinHash 0.8, and trigram Jaccard 0.8. The
graph resolver still uses its existing n-gram/embedding path by default;
`graph resolve --tiered` opts into this cascade. Embedding and LLM branches are
explicitly marked unmeasured in the report because the fixture has no labeled
model scores or judgments. The optional `--judge` CLI flag also requires an
embedder to produce a remainder batch.

The keyword classifier's default 0.6 threshold scores perfectly on these 18
examples. Its sweep stays perfect through 0.75, then recall falls to 0.7857 at
0.90. This small, synthetic set does not justify changing the default. Its
embedding threshold (0.45) remains unmeasured. The relation-pattern baseline
has no confidence cutoff to tune; its 1.0 precision and 0.70 recall show the
expected conservative behavior on the authored cases, not a general extraction
quality estimate. LLM extraction is not measured.

## Duplicate, contradiction and expiry behavior

CommonTrace batches the LLM's duplicate and contradiction decisions in
`fact_conflicts.judge_joint()`, restricts responses to offered fact IDs, and
rejects a fact classified as both. The new regression test drives a judged
contradiction through the real write path: in `supersede` mode the older fact
is retained with `status=superseded`, `superseded_by`, `valid_until`, and
`retracted_at`; `as_of` before and after the transition returns the appropriate
fact. If the temporal guard detects disjoint windows or a replacement that
predates the old assertion, it refuses to supersede and records a flagged
conflict. Value changes remain review-only unless the joint judge classifies
them as contradictions.

TTL expiry (`expires_at`) is separate from contradiction supersession. Expired
facts remain stored and can be inspected with `show_expired=True`; default
fact listing and search hide them. Historical `as_of` reads evaluate expiry at
the requested time. No fact is hard-deleted by either operation.

The Graphiti code graph and source inspected for comparison were at local
commit `964bb11d1e7020e3cc67fab244268b0736b6c219`. Its `dedup_helpers.py`
performs normalized exact matching before entropy-gated MinHash/LSH and fuzzy
Jaccard, while `edge_operations.py` sends duplicate and contradiction indices
through one `EdgeDuplicate` response and closes invalidated edges with temporal
fields. CommonTrace follows the same broad tier ordering and non-destructive
history model; the benchmark here adds labeled evidence only for the local,
non-model tiers.

Focused verification:

```sh
env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  tests/test_entity_resolution.py tests/test_quality_tiers.py \
  tests/test_graph_quality_eval.py tests/test_fact_conflicts.py -q
# 41 passed

env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  tests/test_relation_extraction.py tests/test_ontology_classify.py \
  tests/test_fact_store.py -q
# 85 passed
```
