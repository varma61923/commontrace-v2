# Measured performance work

Baseline: `b9f26ad`, `feat/effect-field`. Date: 2026-11-06.

The target was 10x throughout the system. That target is **not established
across all paths**. The results below distinguish large gains, smaller gains,
and unchanged or noisy paths. Research motivates an implementation; it does not
substitute for measurement of this product.

## Method

`benchmarks/performance_suite.py` runs thirteen seeded workloads. Each workload
and each implementation gets its own fresh Python process. Baseline/candidate
order alternates across three rounds, with eleven timed operations per round
after the first operation. Setup is outside the timed operation; the first
operation is also reported separately. BLAS/OpenMP threads are fixed to one,
and measurements run serially, without parallel tests or a profiler. The host
is Linux x86-64, Python 3.12.14, NumPy 2.5.4, with a two-CPU quota.

The accompanying JSON retains raw samples, round medians, implementation and
harness checksums, and output digests. Reported speedups are medians of paired
round ratios; their min/max are observed variation, **not confidence intervals**.
Latency columns are medians of round medians, so their quotient can differ from
the median of paired ratios.
All statistical floats are normalized to ten decimal places for benchmark
digests. Separate scorer tests require exactly equal returned vector scores and
IDs, and fitting tests compare witnesses, hyperparameters and posterior values
against the scalar reference. This is not a claim of bit-identical floating-point
arithmetic between BLAS implementations.

The storage workloads call the actual SQLite vector engine, canonical fact
writer, conversation store, signed journal, and gateway handler. Durability,
redaction, authentication, proof validation and generation checks stay enabled.
Gateway measurements include JSON handling but exclude HTTP sockets. No remote
model, provider credentials, downloaded evaluation corpus or GPU is used.

## Results

| Workload | Baseline p50 ms | Candidate p50 ms | Paired speedup | Round range |
| --- | ---: | ---: | ---: | ---: |
| effect-fit | 225.980 | 28.696 | 8.41x | 7.87–8.83x |
| context-fit | 675.804 | 58.349 | 11.52x | 10.92–11.58x |
| vector-random | 38.783 | 4.186 | 8.37x | 7.62–9.56x |
| vector-duplicates | 96.685 | 2.393 | 40.40x | 37.88–43.11x |
| vector-sqlite-random | 45.264 | 10.577 | 4.27x | 3.82–4.86x |
| vector-sqlite-duplicates | 100.350 | 8.956 | 11.21x | 10.01–12.28x |
| fact-selective | 0.750 | 0.831 | 0.90x | 0.86–1.01x |
| fact-broad | 10.737 | 10.641 | 1.01x | 0.95–1.02x |
| fact-write | 519.493 | 476.539 | 1.10x | 1.09–1.15x |
| conversation-read | 3.702 | 3.758 | 1.00x | 0.91–1.02x |
| conversation-write | 8.119 | 8.456 | 0.96x | 0.95–0.99x |
| signed-write | 1.062 | 1.065 | 0.91x | 0.81–1.13x |
| gateway-cycle | 6.307 | 5.114 | 1.18x | 1.01–1.44x |

[Raw results and source digests](performance-results.json). All thirteen output checks passed.
A value below 1x is a slower measured run; it is retained in the table. The small
unchanged paths do not support a claim of improvement. Their implementations
were not replaced just to alter benchmark numbers.

First operation in a fresh workload process (includes lazy initialization):

| Workload | Baseline ms | Candidate ms |
| --- | ---: | ---: |
| effect-fit | 252.262 | 63.870 |
| vector-random | 37.764 | 39.363 |
| vector-sqlite-random | 40.824 | 50.015 |

## Implementation and primary sources

1. **Contextual fitting:** weighted success/failure moments are grouped once per
   context and recombined for leave-one-context-out priors. The implementation
   avoids subtracting large nearly equal totals and retains `math.fsum`, the
   original effective sample size, Jeffreys variance floor, and fallback for
   nonbinary legacy input. The binary moment identity is our specialization;
   [Chan, Golub and LeVeque (1983)](https://doi.org/10.1080/00031305.1983.10483115)
   motivate the numerical-stability requirements, rather than a naive
   difference-of-squares variance rewrite.

2. **Effect-field fitting:** bounded, request-local Gram matrices and stacked
   covariance factorizations replace repeated Python arithmetic. Neighborhood
   shortlists retain conservative error margins and reproduce the original
   left-to-right lexical additions with `add.accumulate`, preserving witness
   ties. Baseline predictive densities are computed once; validated predictions
   are reused while their fitted public parameters match. The GP equations,
   leave-one-out evidence, search grid and validation threshold are unchanged.
   This follows the factorization-based computation in
   [Rasmussen and Williams, GPML, chapter 2](https://gaussianprocess.org/gpml/chapters/RW2.pdf),
   the [LAPACK/BLAS design](https://netlib.org/lapack/lug/node65.html), and
   [NumPy's stacked Cholesky contract](https://numpy.org/doc/stable/reference/generated/numpy.linalg.cholesky.html).
   [NumPy's accumulation contract](https://numpy.org/doc/stable/reference/generated/numpy.ufunc.accumulate.html)
   specifies the ordered additions used for lexical tie compatibility.

3. **Exact vector retrieval:** optional float64 blocks screen noncompetitive
   float32 vectors, then the established `math.fsum`/`math.hypot` scorer computes
   every potentially retained score. Boundary/tie candidates are never pruned;
   every row is validated, including later noncompetitive pages. Duplicate
   scoring uses a bounded byte memo within one query. It never caches tenant
   text or carries scores across queries. The conservative dimension-scaled
   screening allowance accounts for dot/norm rounding, informed by standard
   [summation error analysis (Higham, 1993)](https://doi.org/10.1137/0914050).
   Tests cover float32 subnormals, largest finite values, cancellation, reversed
   ties, corrupted later pages, and independent requests.

4. **Fact-cache accounting:** primitive leaves avoid repeated ABC/dataclass
   inspection, and field names are resolved once per dataclass type during a
   walk. Exact object-identity deduplication and conservative allocation sizes
   remain in place. The original 64 MiB snapshot budget is unchanged and its
   50,000-fact and independently traced retention tests still apply.

The numerical accelerators are optional:

```bash
python -m pip install 'unimem[performance]'
```

Core-only installations retain scalar implementations. Very small vector
queries avoid native initialization; insufficient effect-field evidence avoids
building a native Gram matrix. Lazy NumPy initialization has a first-call cost,
which is visible in the raw results. Warm throughput does not promise faster
process startup or faster first queries.

## Reproduce and verify

```bash
git worktree add --detach ../unimem-before-performance b9f26ad
python -m pip install -e '.[dev,performance]'
python benchmarks/performance_suite.py \
  --compare ../unimem-before-performance --rounds 3 --trials 11 > performance.json
```

The runner rejects output mismatches and source changes during an experiment.
It reports every workload, rather than filtering for improvements. Native
linear algebra, corruption checks, private scopes, exact vector ranking, scalar
fallback, insufficient evidence, and cache budgets are covered by regression
tests. Local verification completed:

- Full core and end-to-end suite: **9,086 passed, 145 skipped**, with warnings
  treated as errors. An additional focused run after the signaling-NaN rejection
  fix passed **132 tests**.
- A separate core-only installation without NumPy: **140 passed, 14 optional
  dependency skips**. The native accelerators remain optional.
- Full mypy check: **636 source files**; strict checks passed for the vector,
  effect-field and fact-index modules. Repository lint, configured formatting,
  OpenAPI drift checks, and wheel/sdist builds passed.
- Graphify refreshed **28,130 nodes and 94,201 edges**. Its audit gate passed:
  402 modules, 291 possible unreachable candidates, zero missing endpoints.
  Those candidates are conservative graph findings, not confirmed dead code.
- Bandit reported **zero medium/high findings**; the declared dependency audit
  reported **no known vulnerabilities** at verification time.

Hub PostgreSQL integration tests and TypeScript tests were not rerun for this
performance change; their implementations were unchanged. These local checks
do not assert completion of remote CI.

## Limits of the 10x target

Several paths already use indexes, incremental projections, bounded caches and
batched transactions. Canonical JSONL fact updates still rewrite and verify the
authoritative corpus; signed writes still honor their durable acknowledgement.
Replacing these with deferred persistence or stale authorization caches would
change the contract. Small unchanged-path timings should be read together with
the round ranges, not advertised as gains.

[Amdahl's original result](https://doi.org/10.1145/1465482.1465560) explains why
accelerating one component does not establish a universal end-to-end factor.
For example, SQLite retrieval also pays database/page/decoding costs that a
scorer-only benchmark excludes. The measurements do not establish 10x gains for
Hub PostgreSQL queries, network transports, hosted model inference, every corpus,
all CPU architectures, cold starts or every optional integration. No such gains
are claimed.
