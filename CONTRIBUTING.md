# Developing CommonTrace

Use Python 3.10 or later. Core memory is stdlib-first with PyYAML; optional
semantic, MCP and Hub dependencies stay in their existing extras. Read
[AGENTS.md](AGENTS.md) before changing source or stored memory.

## Local setup

```bash
python -m venv .venv
# Activate your environment using your platform's normal activation command.
python -m pip install -e '.[dev]'
python scripts/dev.py lint
python scripts/dev.py test
```

Core/end-to-end tests are under `tests/` and `e2e_tests/`; Hub tests are under
`hub/tests/`. Hub tests need a disposable PostgreSQL database: their fixtures
create/drop schemas and truncate tables. Never point `HUB_TEST_DATABASE_URL`
at production. Run suites sequentially against one database, or give each process
its own disposable database; truncating Hub fixtures conflict with concurrent end-to-end transactions. Install `hub/requirements.txt`, configure that variable and run
`python scripts/dev.py test --hub` separately. Optional service tests can skip
when the required local capability is unavailable; inspect skip reasons.

## Changes and review

Use argparse, explicit errors, source-relative paths and existing configuration
types. Preserve protocol, CLI and import compatibility. Public APIs need a concise
behavioral docstring; use Parameters/Returns sections when arguments or outputs
are nonobvious. Explain provenance, chronology and concurrency invariants beside
their implementation. Preserve vendor authentication algorithms and durable IDs.

Add regressions for meaningful behavior changes. Seed generated tests explicitly;
prefer independent oracles and metamorphic properties to implementation mirrors.
Use real disposable SQLite/PostgreSQL fixtures for transactions and isolation.
Mock paid model services at the boundary; never change official judges to improve
scores. Measure performance against a pinned baseline, report cold/warm behavior
and separate tracing from clean timings. Document allocation and latency tradeoffs.

Run relevant tests, Ruff and `git diff --check`; security-sensitive changes also
need Bandit and a reviewer. CI repeats core, dev and PostgreSQL tests on Python
3.10–3.12 and checks deployment/container/SDK/security/latency behavior.

## Debugging and documentation

`commontrace doctor` checks a local install. CLI commands expose `--help`.
`python scripts/dev.py serve --dest /path/to/store` runs the local gateway;
its default is loopback. Keep tokens and raw source content out of logs.
Use `python -m pydoc commontrace.retrieval` for Python API documentation.

`python scripts/dev.py docs` refreshes source-derived MCP/API and environment
inventories; `docs --check` checks drift without writing. Update explanations
when behavior changes. See [architecture](docs/architecture.md),
[operations](docs/operations.md) and [performance evidence](docs/performance.md).

Do not commit bytecode, vector indexes, result folders, credentials or local
profiling captures. Do not use stash/clean/restore to erase another worker's edits.
