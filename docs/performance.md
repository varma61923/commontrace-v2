# Performance evidence

Production measurements compare pinned implementations on local CPU hardware.
They establish specific workload improvements, not universal 30 ms latency or
superiority over competitor answer quality. No inference service is used by
the measured request profiles.

| Operation | Baseline | Improved |
| --- | ---: | ---: |
| Warm HTTP recall, 6,400 lessons, p95 | 219.922 ms | 22.951 ms |
| Warm HTTP recall, 1,000 lessons, p95 | — | 3.394 ms |
| Warm HTTP health, p95 | 44.080 ms | 0.733 ms |
| Node-only graph extraction, 520 nodes / 100,000 edges, median | 872.193 ms | 2.576 ms |
| Cold mapped exact retrieval, 200,000 vectors / ten eligible passages | 24.836 ms | 0.703 ms |

HTTP measurements use baseline `ccd131d` and improved revision `7376495`,
three seeded fixtures, 150 warm requests per endpoint and persistent loopback
HTTP. Responses match exactly. Improved HTTP recall p99 is 28.262 ms and its
maximum is 30.172 ms. Cold 6,400-lesson fixture setup still takes approximately
1.23 seconds. The vector row compares baseline `c8ce5c7`; full raw graph reads
also exceed 30 ms and include documented regressions.

The improved code passed 4,492 core/end-to-end tests (34 skipped), independent
reviews and all 30 push/PR CI jobs. The preceding security changes passed
2,464 local PostgreSQL Hub tests; CI repeats that matrix on Python 3.10–3.12.

## Detailed archive and reproduction

One-off scripts and machine-specific results were removed from the current
source tree. Their complete evidence remains available at immutable revision
`7376495fb1140cb864a669d2ca9472dad0ce1020`:

- [Request samples, source hashes and validation](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/latency30-http.json)
- [Request reproduction helper](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/profile_latency30_http.py)
- [Graph measurements, including regressions](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/latency30-graph.md)
- [Implementation and security review](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/phase6-production-review.md)
- [Pinned competitor commits, licenses and papers](https://github.com/varma61923/commontrace-v2/blob/7376495fb1140cb864a669d2ca9472dad0ce1020/research/competitor-analysis.md)
- [Verified push CI](https://github.com/varma61923/commontrace-v2/actions/runs/37398752232)
- [Verified PR CI](https://github.com/varma61923/commontrace-v2/actions/runs/37398755791)

Recover the optional HTTP profiler without restoring the research directory:

```bash
mkdir -p scratch
git show 7376495fb1140cb864a669d2ca9472dad0ce1020:research/profile_latency30_http.py > scratch/profile_http.py
python scratch/profile_http.py . --lessons 6400 --runs 3 --requests 50
```

The maintained local-only evaluation helper lives in `benchmarks/local_eval.py`.
Official judges, the conversation harness and their regression tests remain
because the CLI and tests use them. Runtime modules, source memory, fixtures,
protocol schemas, SDKs, distribution metadata and deployment assets are retained.
