# Hybrid graph expansion and multi-query evidence

The indexed hybrid retriever currently adds independent dense candidates but
seeds its graph walk from only the first three BM25 hits. A paraphrase found
only through embeddings consequently cannot retrieve a linked supporting fact.
Its multi-round retriever also sums ranks across all subqueries, allowing
repeated common hits to displace evidence answering a unique facet.

Reuse the existing retrieval recipe and canonical fact admission. Seed the
existing bounded two-hop walk from the RRF-60 ranking of admitted lexical and
dense first-pass hits, together with explicit centers/entities. Deduplicate and
cap seeds at eight. Add the seed list and graph-candidate count to explanations.
Apply action-specific authority before choosing seeds, and retain validity,
expiry, scope, evidence and authority checks on graph additions. Keep ANN
threshold 0.6, lexical candidate addition and the existing cross-encoder stage.

For multi-round retrieval, keep independently labelled query blocks and an
optional judge-selected core of already retrieved ids. Assemble core first,
then each block's best hit, then the RRF-60 fill, all within the requested limit.
Stop if the judge repeats exhausted subqueries; never exceed three rounds or
three new subqueries per round. Keep bounded block/core decisions in the
redacted training trace. Model decisions may select only known candidate ids.
These are retrieval-policy changes; they do not demonstrate model-judged answer
accuracy or the million-fact hybrid latency gate.

## Competitor source evidence

This adapts algorithms to CommonTrace's existing implementation; no source text
is copied and no competitor runtime dependency is added.

- Graphiti, Apache-2.0, commit
  `964bb11d1e7020e3cc67fab244268b0736b6c219`,
  `graphiti_core/search/search.py:342-366`: default BFS origins come from the
  union of configured first-pass search methods. Unlike its default RRF
  constant, CommonTrace retains k=60. The checked-out commit differs from the
  original brief's `a9ef13f`; this note cites the inspected source.
- EverOS, Apache-2.0, commit
  `fcd41b57cab6c577211120e166f801038d21b7ae`,
  `src/everos/memory/search/llm_multiround.py:738-882` and
  `src/everos/memory/search/dto.py:39-51`: independent query blocks, accumulated
  core and per-query top-hit guarantees prevent consensus from hiding facets.
- mem0, Apache-2.0, commit
  `b7ad69afda6b6ed030347c66d48a13e4de9dec08`,
  `mem0/memory/main.py:1642-1707`: semantic candidates receive keyword/entity
  scores, but keyword-only results do not enter the candidate set. Preserve
  CommonTrace's existing independent BM25 candidate channel instead.
- Hindsight, MIT, commit
  `a9eeed77f9bce99d8f97d39a30d3281747300824`,
  `hindsight-api-slim/hindsight_api/engine/search/recall_boost.py`: strategy
  boosts distinguish retrieval-source evidence from reranker scores. Retain
  CommonTrace's existing per-channel score breakdown rather than adding another
  ranking path.

Measurements and exact validation commands are recorded after implementation.

## Observed validation

Four new retrieval-policy test modules pass **19 tests in 1.92 seconds**, and
the protocol retrieval vectors pass **5 tests in 0.68 seconds**. A paired
isolated-process policy harness against the frozen `7e0a224` source and this
candidate observed: dense-only graph support changed from absent to present;
unique facets recovered increased from 1/2 to 2/2; repeated-query exhaustion
reduced judge calls from 3 to 2; a fact withdrawn during judging changed from
returned to excluded; and a lexical seed invalidated during ANN lookup changed
from 2 returned rows to 0. The scope is synthetic policy fixtures over real
temporary SQLite and does not measure model quality or latency. The signed or
protocol causal acceptance suite is separate.

```sh
python -m pytest tests/test_indexed_search.py tests/test_hybrid_graph_seeds.py \
  tests/test_multiround_evidence.py tests/test_retrieval_policy_vectors.py -q
python -m benchmarks.hybrid_evidence --source /baseline --source /candidate \
  --output docs/strategy/measurements/hybrid-evidence.json
```
