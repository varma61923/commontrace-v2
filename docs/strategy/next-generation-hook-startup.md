# Native hook bridge startup measurement

The native hook bridge runs as a fresh Python process for each host event. Its
successful warm-broker path only needs a fixed set of six CLI options, but
`argparse` imported about 11.2 ms of cumulative startup work. The bridge now
uses a small local parser and keeps the existing flag names, choices, unique
abbreviations, `--name=value` form, help output and legacy `--python` option.
Frame exchange, fail-safe output, receipt acknowledgement and journal commit
remain unchanged.

The test-first regression asserts that a successful bridge invocation does not
import `argparse`. It failed on the baseline and passes on the candidate. The
focused bridge, service and hook-kit suite passes 56 tests and covers the
generated option forms, invalid/missing values, all output formats, receipt
ordering and fail-safe behavior.

Three sequential runs used the same command and fixed directive through a warm
broker:

```sh
env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m benchmarks.injector_bench \
  --transport-only --repetitions 50 --out <round.json>
```

| Run | Baseline p95 | Candidate p95 | Candidate deliveries |
| --- | ---: | ---: | ---: |
| 1 | 55.16 ms | 43.47 ms | 50/50 |
| 2 | 49.51 ms | 42.58 ms | 50/50 |
| 3 | 49.02 ms | 43.37 ms | 50/50 |

All candidate rounds pass the `<50 ms` transport target, with zero withheld
samples. Mean p95 fell from 51.23 ms to 43.14 ms. The baseline had one miss.
The six public aggregate reports are `measurements/native-hook-p95-{baseline,candidate}-{1,2,3}.json`;
their hashes are in the [report manifest](measurements/native-hook-p95-rounds.sha256).
The candidate source was `5fdeb370be911cf3ac1ababdd2591ca9d6cb2f40` and the
measured bridge SHA-256 is recorded in each candidate report.

This fixture does not measure semantic relevance, cold broker/model startup,
or latency under broad host-load variation. Treat it as a measured pass for the
native transport path; universal hook latency remains an open operational
target.

Competitor inspection found no lower-startup local pattern to port while
preserving CommonTrace's per-prompt semantics. Mem0's hook runner searches only
the first prompt, EverOS performs cloud search per prompt, and Hindsight's
Codex integration prestarts a daemon before using local HTTP. CommonTrace
already has a warm local broker; the measured cost sat in client process
imports. The optimization therefore removes only the unused generic parser
dependency rather than changing delivery cadence or transport.
