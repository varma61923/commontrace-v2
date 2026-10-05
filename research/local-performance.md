# Local performance and verification

No paid APIs or generation/judge inference were used. These results describe
local implementation performance and regression coverage, not a competitor
leaderboard or official answer-quality score.

The dense comparison uses 50,000 passages and 768-dimensional deterministic
vectors matching the real cache's existing float16 precision. Baseline checkout:
`10571317a3e85eb87db686d8b7e3ffb4d55e0878` (the preceding implementation commit; same source tree as local `1a1c80d`). Both runs used the same Python
3.12 host and `OPENBLAS_NUM_THREADS=1`. Machine-readable details are in
`local-performance.json`.

| Metric | Baseline | Improved |
|---|---:|---:|
| First dense search | 876.251 ms | 798.153 ms |
| Second dense search | 923.802 ms | 154.680 ms |
| Vectors read over two searches | 100,000 | 50,000 |
| Retained vector cache | None: exceeds 128 MiB limit | 76,800,000 bytes |
| Float32 full matrix equivalent | 153,600,000 bytes | Compact vectors retain existing float16 precision |
| Top ten ids | Same | Same |

The fake encoder isolates indexing and scoring overhead; it does not measure
model inference, realistic content distributions, persistent-cache disk latency,
end-to-end answer generation or official 10M-token questions. Cold construction
and repeated-query savings vary with hardware and corpus. Dense search remains
linear in corpus size; over-budget indexes stream instead of materialising the
whole log. The configured byte limit bounds retained vector matrices, not total
Python process RSS or metadata overhead.

Reproduce from the final checkout (numpy required):

```sh
git worktree add --detach ../commontrace-phase1 10571317a3e85eb87db686d8b7e3ffb4d55e0878
OPENBLAS_NUM_THREADS=1 python research/profile_dense_memory.py ../commontrace-phase1
OPENBLAS_NUM_THREADS=1 python research/profile_dense_memory.py .
```

Validation performed:

* Core and end-to-end suite: `python -m pytest tests/ e2e_tests/ -q` — 4,228
  passed, 28 skipped. A subsequently added nested entity traversal regression
  also passed in the focused 41-test memory suite.
* Hub UI/authentication/signup suite against isolated local PostgreSQL 16:
  310 passed. Signup was checked again after its final shared-card styling change.
* Ruff across the repository, `git diff --check`, and the existing Bandit
  medium/high security gate. SQL findings were reviewed: schema aliases and
  eligibility clauses are fixed, while all query/filter values are bound.
  A regression checks adversarial owner/kind/source/filter values.
* Existing latency CI gate at 100/400/1,600/6,400 lessons: passed; largest median
  42.4 ms, fitted total exponent 1.10 (limits 500 ms and 1.5).
* Chromium: all seven local routes at 1440 px light, 390 px light and 1440 px
  dark; shared headings/themes, no horizontal page overflow or runtime errors.
  Read-only command help and explicit light-theme persistence were checked.
  Hub operator and signup renderer previews were inspected; DB-backed Hub tests
  cover their request behavior and CSP. This is not a complete accessibility audit.

The earlier lexical profile compared original commit `5119efe` with `1a1c80d`:
10,000 messages, median uncached recall 390.046 → 7.278 ms, manual memory insertion
19.053 → 0.224 ms, ingestion 0.567 → 0.884 s. That result is recorded as prior
local evidence rather than a new official benchmark run.

Official judges, datasets and scoring code were not changed. `benchmarks/` was
retained because the conversation CLI and tests import it. Category accuracy,
abstention calibration, learned reranker gains and competitor supremacy remain
unmeasured. See `competitor-analysis.md` for the remaining architectural limits.
