# Bounded fact batch projection

The exact/lexical `add_facts` path now stages new FTS5 rows and inserts them
with bounded multi-row statements when the transaction flushes. Semantic
duplicate search and LLM conflict judging keep eager FTS visibility. New facts
also skip the existing-row probe and reuse conflict features computed while
staging. Full SQLite durability and the transaction boundary are unchanged.

The candidate was measured against the frozen `7e0a22402b9e69653c2fa190a374b42b92ccb83b`
million-fact result of 4.69 ms single-write p95 and 1,739 facts/s. The exact
candidate run used Python 3.12.3 on the same Linux host, a fresh SQLite store,
20 trials, and a 1,000-fact batch. It measured 4.263 ms p95 and 1,819 facts/s
at one million facts. The single-write gate (<20 ms) passes; the batch gate
(≥2,000 facts/s) still misses. These are separate source revisions and runs,
so the nominal increase from 1,739 to 1,819 facts/s does not establish a
controlled performance gain. The candidate also reports 0.230 ms sparse-overlap
search p95, which does not measure hybrid search.

The raw run and source hashes are in
[the candidate report](measurements/p0-batch-projection-candidate.json). The
implementation diff SHA-256 is
`849069bdd3c42c95ebcaf6d67d939caa936ff927b6a1174b1a95d86de2d3f66e`, based on
`b7b493d61c602120395907ff707e42e1b16c0caa`.

Reproduce with:

```sh
env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m benchmarks.next_generation_storage \
  --sizes 1000,10000,30000,1000000 --trials 20 --legacy-max 1000 \
  --output /tmp/p0-candidate-storage.json
```

The legacy JSONL comparison in this run is capped at 1,000 facts. The run does
not establish the 1M hybrid-search, server-latency, or zero-re-embedding
restart gates. The candidate's focused fact-store and conflict tests passed;
the 6,812-pass full suite was run on the frozen base before this change.
