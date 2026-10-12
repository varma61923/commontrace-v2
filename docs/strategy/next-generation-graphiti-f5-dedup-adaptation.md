# Graphiti F5 dedup candidate-index adaptation

CommonTrace already has an exact-alias and entropy-gated MinHash entity
candidate tier. The missing piece was candidate indexing: `tiered_candidates()`
compared every entity pair (up to the existing 2,000-entity cap) before applying
its MinHash/Jaccard gates. This change adds exact-form postings and a four-wide
MinHash band index for the existing default 128-position signatures.

## Competitor pattern and adaptation

Inspected `/root/Test/graphiti` at revision
`964bb11d1e7020e3cc67fab244268b0736b6c219`. In
`graphiti_core/utils/maintenance/dedup_helpers.py`, Graphiti builds normalized
name indexes and MinHash LSH buckets (`_build_candidate_indexes`, lines
296–318), then attempts exact matching before entropy-gated fuzzy resolution
through the bands (`_resolve_with_similarity`, lines 321–378). Its default
fuzzy Jaccard threshold is 0.9, entropy floor is 1.5, and it uses 32
permutations grouped into bands of four (lines 31–36, 80–86, 118–129).

CommonTrace's `commontrace/entity_resolution.py` already provides a reviewable
exact → entropy-gated MinHash → optional embedding/judge flow. I adapted only
the indexing pattern, not Graphiti's scoring thresholds or automatic ingestion
merge behavior. Exact normalized surfaces are grouped through postings. Fuzzy
entities use the existing CommonTrace signature and unchanged thresholds; the
existing exact trigram Jaccard check still decides whether a shortlisted pair
is returned.

The band shortlist is lossless relative to CommonTrace's current MinHash
predicate at thresholds above 0.75: a 128-position signature with no identical
four-position band can agree in at most 96 positions (0.75). Thresholds at or
below 0.75, or a signature length that differs from the configured default,
fall back to the exhaustive high-entropy pair set. Exact matches remain
independent of the entropy gate.

## Validation and limits

`tests/test_entity_resolution.py` verifies that the shortlist produces the
same accepted default-tier pairs as exhaustive comparison on a mixed synthetic
name set, that lower thresholds use the exhaustive fallback, that low-entropy
exact aliases still appear, and that the existing tiered resolution tests pass.
The eight-name test fixture reduces its shortlist to 4 pairs from 28 possible
pairs. A separate deterministic ABBA diagnostic over synthetic five-token
names measured candidate generation plus the unchanged MinHash/Jaccard checks:

| Entities | Possible pairs | MinHash candidates | Accepted pairs | Exhaustive median | Indexed median |
|---:|---:|---:|---:|---:|---:|
| 200 | 19,900 | 389 | 100 | 132.44 ms | 8.80 ms |
| 700 | 244,650 | 3,330 | 350 | 1,641.34 ms | 56.02 ms |

Both sizes returned exactly the same accepted pair set. The source-hashed
[measurement report](measurements/f5-graphiti-lsh-candidate-2026-10-12.json)
records the samples and limitations. Reproduce it from the repository root:

```sh
python -m benchmarks.entity_resolution_lsh --sizes 200 700 --repeats 2 \
  --output docs/strategy/measurements/f5-graphiti-lsh-candidate-2026-10-12.json
```

This is a synthetic candidate-generation diagnostic, not a production runtime
or labeled recall measurement. It excludes graph I/O, ontology checks,
embeddings, and LLM judgment.

Validation does not measure end-to-end performance or entity-resolution
quality on a production corpus. Dense LSH buckets can still approach the
exhaustive pair count, and thresholds at or below 0.75 intentionally retain
the exhaustive path. MinHash estimator behavior and CommonTrace's thresholds
are unchanged; no benchmark result or scorecard claim is updated.
