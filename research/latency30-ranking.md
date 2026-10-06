# Exact lexical ranking latency

The candidate retains the existing five lexical scorers, tokenization, weighted
field presence, floating-point addition order, adaptive relevance floor, raw
scores, reliability/recency/graph adjustments, and stable corpus-order ties.
It changes temporary candidate storage and defers work until after the adaptive
floor is known. No retrieval budget, dependency, official judge, or model call
changes.

## Baseline and method

Baseline: `ccd131d4b76bf187c94f15d58426f14f996ae293`, whose production files
are identical to `598409064558cec0ea1f5642381dcfb16d3fe7ff`. The former adds
a Python 3.10 test compatibility fix. The candidate is the working diff on that
baseline; the integrating commit supplies the final SHA.

`profile_latency30_ranking.py` uses the repository's existing real Markdown
lesson fixture generator: 6,400 lessons, corpus seed 20260907, query seed 7,
36 six-term queries repeated three times, top-k 3, all five scorers. It measures
cold loading/indexing separately from warmed ranking and records wall time and
process CPU time. It also measures a warmed rare query on 100,000 documents.
The helper does not call an API or load a model. Commands:

```sh
python research/profile_latency30_ranking.py --checkout /path/to/baseline --output baseline.json
python research/profile_latency30_ranking.py --checkout /path/to/candidate --output candidate.json
python -m pytest tests/test_latency30_ranking.py tests/test_retrieval_speed.py -q
```

The compact checked-in JSON excludes the 540 full answer records; the helper
retains them so every returned field can be compared, including matched terms,
raw scores, relevance, and adjustments. Paths are normalized to basenames for
temporary-directory reproducibility. Compare baseline and candidate `answers`
arrays for every scorer, rather than just top IDs.

## Implementation

The former accumulator allocated a list of matched strings for every candidate,
then materialized adjusted result tuples even for rows subsequently excluded by
the adaptive tail. The new path accumulates numeric scores and coverage,
computes the identical relevance floor first, and constructs adjusted tuples
only for surviving candidates. It decodes matched terms only for selected rows.
An explicit corpus position tie key replaces sorting all candidate IDs.

Candidate state uses sparse dictionaries when the combined posting count is at
most one quarter of corpus size, avoiding whole-corpus allocations for rare
queries. Broad queries use dense numeric arrays. This threshold changes only
the representation, never candidate eligibility or scoring.

Matched-term masks include only terms with postings and are limited to 64 bits.
Absent terms still count toward the original query normalization. Queries with
more than 64 present terms use per-hit matched-term lists instead, bounding
storage by actual posting matches and avoiding arbitrarily large integers per
document. Independent differential tests exercise both representations,
large absent queries, and disjoint present terms.

Persisted corpus format, source identity checks, scoped corpus selection,
snapshot invalidation, and memory provenance remain unchanged.

## Evidence and limits

The final bounded-mask candidate returned identical complete answers in all
540 timed samples. Warm ranking wall times in milliseconds:

| Scorer | Baseline median | Candidate median | Baseline p95 | Candidate p95 |
| --- | ---: | ---: | ---: | ---: |
| adaptive-v1 (default) | 7.233 | 2.989 | 46.651 | 3.563 |
| idf-v3 | 7.852 | 6.638 | 51.994 | 7.692 |
| idf-v2 | 7.525 | 6.229 | 49.520 | 7.290 |
| bm25-v1 | 7.945 | 7.536 | 48.419 | 8.224 |
| count-v1 | 6.292 | 5.692 | 46.246 | 6.654 |

The default scorer's median process CPU gain is 2.42×. Other scorers' measured
median CPU gains are smaller (1.05–1.21×). The rare 100k query takes 6.24µs
baseline versus 6.98µs candidate median CPU and 1,247 versus 1,592 peak
temporary bytes: a small fixed overhead, with no whole-corpus allocation.
The sparse branch prevents a large rare-query regression; it does not improve
this already inexpensive one-hit path.

See `latency30-ranking.json` for cold timings and full measurement metadata.
These are local lexical ranking measurements, not
end-to-end HTTP guarantees, official benchmark accuracy scores, dense vector
retrieval measurements, or competitor superiority claims. Shared-host activity
and Python allocation/collection affect latency tails; the parent integration
report records the separate final HTTP measurements.

The rare-query check matters: dense arrays must not make a one-hit query on
100,000 documents allocate proportional to corpus size. Both baseline and
candidate use approximately microseconds of CPU and kilobytes of temporary
storage for that warmed query. Broad-query ranking remains proportional to
posting matches; there is no claim of constant-time retrieval.

The supplied roadmap's HNSW and ONNX timing budgets are proposals rather than
measurements of this implementation. Their dependencies, recall tradeoffs,
hardware assumptions, and independent model quality need evidence before
adoption. The relevant competitor lesson here is to use independent sparse
candidate generation with inverted postings, already present in CommonTrace,
and reduce avoidable per-candidate work while preserving exact evidence.
No competitor source code was copied.
