# Local-tier retrieval latency vs corpus size

Median of 3 runs per point, top_k=3, warm filesystem cache, lesson_cache ON.

| lessons | load (parse) | rank (score) | total | load share |
|---:|---:|---:|---:|---:|
| 100 | 0.3 ms | 0.2 ms | 0.4 ms | 62.8% |
| 400 | 1.0 ms | 0.3 ms | 1.3 ms | 80.5% |
| 1,600 | 3.1 ms | 1.0 ms | 4.1 ms | 76.6% |
| 6,400 | 13.2 ms | 3.9 ms | 17.0 ms | 77.4% |

| stage | fitted alpha | |
|---|---:|---|
| load | 0.92 | LINEAR-OR-WORSE |
| rank | 0.77 | sublinear |
| total | 0.88 | LINEAR-OR-WORSE |

`alpha` is the fitted exponent in `latency ~ lessons**alpha`, an OLS slope on log-log axes -- the same estimator `hub/bench_scaling.py` uses.
