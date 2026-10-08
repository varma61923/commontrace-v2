# Memory evolution validation

Validated on Linux x86-64, Python 3.12, on 2026-10-08. All changes belong to
`feat/commontrace-memory-evolution`; the eight reference checkouts remain clean.

| Check | Result |
|---|---|
| Full `python3 -m pytest tests/ e2e_tests/ -q` | 5,877 passed, 118 skipped; no failures |
| Final evolution, gateway hardening, MemFS and dream checks | 55 passed |
| Chromium console contracts, including Memory Palace at 390/1440 px | 18 passed |
| Existing `python3 -m benchmarks.memory_contracts` | 19 cases passed |
| Ruff, compileall, JavaScript syntax and Git whitespace checks | Passed |
| Independent Reviewer B | CONFORM; 47 focused tests passed |
| Linux one-file build and HTTP smoke | Passed: ADD/reflect APIs, console assets and benchmark site |

The final focused checks cover the review fixes made while the full suite was
running, including repeated shared curated recall without cache writes. Browser
contracts exercise real refresh/rejection requests, revision handling and literal
rendering of untrusted text. Optional integrations account for skipped tests.
The test environment uses MCP SDK 2, matching the repository's supported API.

Run `./reproduce.sh` to independently reproduce the synthetic fixture in two
processes. Generated reports carry the committed source revision, configuration,
dataset hash and seed; machine-specific latency remains descriptive. The binary
artifact is generated at `dist/commontrace-local` and is deliberately untracked.

This validation does not establish paid-vendor rankings, live business effects,
paper-specific speedups, ~50 ms guarantees, downloaded GLiNER model quality,
security theorems, learned-policy training or internet deployment. See
[the implementation guide](MEMORY_EVOLUTION.md) and
[the research digest](RESEARCH_DIGEST.md) for feature boundaries.
