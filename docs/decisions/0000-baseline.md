# UniMem next-wave baseline

Base: `08a251c` on `feat/frontier-causal-governance` (2026-10-16).
The user's request keeps implementation on this branch; it overrides the
attachment's suggestion to create `feat/next-wave`.

| Gate | Observed result |
|---|---|
| Ruff | Pass |
| Configured strict mypy | 65 source files pass |
| Bandit `-ll` | No medium/high findings |
| Declared dependency audit, including extras | No known vulnerabilities found |
| Pinned Hub dependency audit | No known vulnerabilities found |
| Gateway and Hub OpenAPI freshness | Pass |
| Hub image import closure | 11 passed |
| Graphify | 346 modules, 288 reachability candidates, 0 missing endpoints |
| Local retrieval latency | 19.9 ms at 6,400 lessons; fitted total exponent 0.95; gates 500 ms / 1.5 |
| Python 3.12 core/dev coverage run | 8,074 passed, 127 skipped, 1 failed; 85.32% coverage |
| Python 3.10 core install | 7,452 passed, 345 skipped |
| PostgreSQL 16 Hub suite | 2,614 passed, 46 skipped |

The failed test was the stalled receipt peer's disconnect assertion. Under CPU
load, the native hook's 25 ms response deadline can close the socket before the
test thread sends its reply. `BrokenPipeError` therefore observes the same
required disconnect as an EOF. The test now handles both signals while retaining
its timeout and disconnect assertion. The transport's deadlines and behavior
are unchanged. The full hook-service file passes (9 tests), and the native hook,
bridge and HookKit files pass (62 tests). A parallel rerun exposed the import-isolation
fixture making the same scheduling assumption; that functional check now uses
the existing test wrapper while preserving the isolated `-I -S` interpreter.
Dedicated deadline tests retain production timing. All 71 related hook tests
pass together under parallel execution. The focused coverage rerun passes
with 85.32% accumulated coverage. No existing test is removed or skipped.

These are measurements of this execution environment. Optional integrations use
fixtures; no live vendor service or robot actuation is covered by these runs.
