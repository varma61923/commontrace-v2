# Bounded summary work

Baseline: `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`. The summary path now
reuses the source-count validation already performed by `Store.summaries()`
before fetching any message bodies. Unchanged summaries skip source hydration.
Forced model summaries fetch ordered pages of at most 16 messages, stopping
when the existing 24,000-character transcript budget is filled. The prompt,
date label and truncation prefix remain identical to the baseline.

Summary generation reads a consistent source snapshot, and publication checks
the source revision under the writer transaction. A concurrent append or
deletion cannot certify a partial transcript as a current summary. This guard
uses the space revision, so unrelated-session changes conservatively require a
retry too. Extractive summaries still examine all source sentences when they
need regeneration; this change does not replace their frequency objective.

## Measurements

Five timed runs used a 10,000-message session in real SQLite, followed by a
separate allocation measurement. Baseline and candidate commands ran sequentially
on the same shared environment with a two-CPU quota. Corpus ingestion and the
initial summary were prepared before timing. Model completion was a stub:
the second workload measures prompt construction, not inference or answer quality.

| Workload | Baseline median | Improved median | Speedup | Hydrated messages |
|---|---:|---:|---:|---|
| Reuse an unchanged summary | 71.410 ms | 0.509 ms | 140.3× | 10,000 → 0 |
| Force a model summary; construct its bounded prompt | 84.198 ms | 1.350 ms | 62.4× | 10,000 → 32 |

Model prompt SHA-256 matched exactly between both versions. Peak traced Python
allocation fell from 15,757,879 to 879 bytes for reuse, and from 32,629,672 to
121,691 bytes for model prompt construction. These are allocation peaks from
separate untimed runs, not process RSS. Concurrent workloads can affect timings;
the message counts and identical prompt provide independent evidence of the
avoided work. The row cap permits up to 16 large messages to be loaded together;
it is not a 24,000-character bound on source-page memory.

## Reproduction and correctness

Run the helper shipped in this change against both independent checkouts:

```bash
python research/profile_summary.py /path/to/baseline --turns 10000 --runs 5
python research/profile_summary.py /path/to/candidate --turns 10000 --runs 5
```

Raw samples and environment details are in
[summary-performance-results.json](summary-performance-results.json).
New regressions check reuse without message hydration, forced and changed
summaries, exact prompt equivalence with bounded source pages, lazy input
consumption, and concurrent evidence changes before publication. Existing
summary, provenance and archive tests remain applicable. No judge, source
evidence, benchmark answer or remote model service was changed.
