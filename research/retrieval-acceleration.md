# Scope-aware exact mapped retrieval

Baseline: `c8ce5c792916c10e23b0618c59259960294b7bcc`. This change reduces
cold access to a small source-filtered scope in a large prepared vector corpus.
The canonical fixed-order scorer, retrieval limits, query facets, hybrid fusion,
context budgets, model dependencies and official judges are unchanged.

## Bottleneck and implementation

Loading a mapped snapshot previously checked passage-id ordering across every
record before a session-filtered query could use even ten passages. Passage ids
are interleaved with 768-dimensional vectors, so that check touched pages across
the entire file. The exact filtered search itself needed only a small subset.

The loader continues to validate file format, dimensions, source identity,
generation and the authoritative SQLite passage count. A cold scoped view
instead validates each selected passage id, source turn and content hash against
the source snapshot before scoring it. Binary-search results must match every
eligible source id; missing ids, corrupt ordering that prevents those matches,
or mismatched provenance trigger a complete restart through the source/vector
cache. Previously accumulated partial rankings cannot escape that restart.
Source selection occurs before top-k and uses the existing indexed SQLite lookup.

A partially validated view is never inserted into the global `_INDEX` cache.
Broad recalls still require full positive/increasing-id validation, and already
fully validated warm indexes retain their normal behavior. Source filters,
updates, deletion, temporal eligibility, frozen-store restrictions and private
snapshot/publication handling remain in force. Nothing from unselected rows is
returned or inferred as evidence. The selected id/turn/hash checks are stronger
than the previous full-file ordering check for the evidence actually selected.

## Measurements

| Prepared vector corpus | Eligible passages | Before median | After median | Gain |
|---|---:|---:|---:|---:|
| 50,000 × 768 dimensions | 10 | 11.299 ms | 0.591 ms | 19.12× |
| 200,000 × 768 dimensions | 10 | 24.836 ms | 0.703 ms | 35.33× |

Both workloads use eight query vectors, a top-k allowance of 64, deterministic
normalized sine passage vectors and seed 2026. The real SQLite vector cache is
prepared before timing; no model is loaded or invoked during timed searches.
All returned passage ids **and float scores are bit-identical** between the
baseline and candidate. The snapshot files occupy 79,201,776 and 316,801,776 bytes.

Each timed query evicts the process index entry and advises the OS to drop clean
pages for that snapshot only, using `POSIX_FADV_DONTNEED`. The advisory nature of
this operation and shared-workspace scheduling can affect timings. Ingestion,
model inference, answer generation and HTTP transport are outside timing.
These are cold scoped retrieval gains, not a universal latency multiplier or
an official answer-quality comparison against Mem0 or other memory systems.
Warm tiny scopes can pay a little additional header/count/source-proof work
when no fully validated global index exists; the partial view deliberately does
not acquire the stronger global-cache trust needed by broad recalls.

Raw samples, environment, baseline revision, matching ids/scores and rejected
experiment measurements are in `retrieval-acceleration-results.json`.
Reproduce against independent baseline/candidate checkouts:

```bash
OPENBLAS_NUM_THREADS=1 python research/retrieval-acceleration-profile.py CHECKOUT \
  --passages 50000 --scoped --runs 5
OPENBLAS_NUM_THREADS=1 python research/retrieval-acceleration-profile.py CHECKOUT \
  --passages 200000 --scoped --runs 5
```

The helper also supports ordinary retained/mapped searches without `--scoped`,
so improvements can be checked against unrestricted workloads. NumPy is required;
no remote embedding, generation or judging service is used.

## Rejected native scoring experiment

An exploratory scorer used float32 BLAS proposals, conservative rounding
envelopes, known global heap cutoffs and canonical refinement of every row that
could reach a cutoff. It retained tied candidates and fell back for unsafe or
degenerate inputs. Isolated math tests looked promising and provisional ranking
tests retained exact scores. End-to-end acceptance failed: alternating searches
on the same 50,000-passage corpus at eight facets/top-64 measured canonical
189.150 ms versus native/refinement 279.908 ms. The native helper and its
production wiring were removed. The final implementation keeps the existing
canonical scorer. Raw exploratory samples remain recorded for transparency;
the reproducer measures the retained scoped-cache change.

## Verification and research context

Four new scoped regressions independently corrupt selected ids, turn ids and
content hashes after some earlier batches have ranked successfully; source-backed
restart restores the complete ranking without duplicate rows or excluded-source
encoding. Another test proves a cold scoped map cannot warm the global index for
a later broad recall. Together with the existing snapshot, frozen-store,
deletion/update, corruption, batch/tie and fallback tests, 78 targeted checks pass.
Ruff and whitespace checks pass.

The pinned competitor/paper audit in `competitor-analysis.md` and
`algorithm-audit.md` motivates keeping metadata filtering, source provenance and
independent sparse/dense candidates. This implementation applies those principles
to CommonTrace's own exact local index; no competitor source was copied. The
workstream preserves the existing recall objective rather than introducing an
unmeasured approximate candidate index or asserting quality superiority from
latency alone.
