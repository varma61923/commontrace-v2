# Frontier validation record

The roadmap is covered by seeded statistical tests, adversarial store tests,
CLI/MCP/gateway contracts, and PostgreSQL-backed Hub tests. Results below were
produced on Python 3.13 unless stated otherwise. Optional integrations use mocks or local
fixtures; no live vendor request, payment, enclave or robot actuation was tested.

| Check | Result |
|---|---|
| Ruff and `git diff --check` | Pass |
| Configured strict mypy list | 65 modules pass |
| Bandit `-ll` | No medium/high findings |
| Both generated OpenAPI contracts | Current |
| Hub image import allowlist | 11 tests pass |
| Alembic | Upgrade from scratch, `alembic check` clean, one head: `fb13c5d7e9a1`; downgrade/upgrade of the frontier column round-trips |
| Graphify module budget | 346 Python modules under `commontrace/` (no modules added by the hardening round) |
| Local-tier latency gate | 46.9 ms at 6,400 lessons, fitted total exponent 1.00; limits 500 ms / 1.5 |
| Core/dev suite with coverage (Python 3.13) | 8,064 passed, 138 skipped, 0 failed; 85.27% coverage, above the 80% gate |
| Core-only install (Python 3.10, no extras) | 7,452 passed, 345 skipped, 0 failed |
| Hub suite (PostgreSQL 16) | 2,635 passed, 25 skipped, 0 failed |

The hardening round measured end-to-end MCP `retrieve` latency with a running
holdout experiment and 200 lessons: the original roadmap commit took p50 26.4 ms
against 18.1 ms before the roadmap (+46%), driven by per-occasion signed log heads
and calibration recomputed on every call; after the fixes it measured p50 20.7 and
22.9 ms against 19.5 and 19.6 ms in two runs on this shared machine. Lesson writes
cost about 5 ms against 1.6 ms: two fsynced signed appends (lineage attestation,
transparency leaf) are the price of durable provenance on every write. Spatial
updates with the SQLite fact store stay near 5 ms after 1,200 updates. These are
measurements of this environment, not deployment latency promises.

Statistical mutation checks used isolated source copies. Removing gold-label
inclusion weights, factorial contrast weights, cluster HT weights, the RATE
validation condition, or the replay cost cap made the corresponding regression
test fail. Earlier predictable-betting and conformal IPW mutations also failed
their seeded tests. In the hardening round, the previous `experiment.analyze`
fails the harm-withdrawal test, the previous `recall.pack` fails the hazard
property test, and an unweighted cluster estimate misses the planted effect by
more than 0.2 in an unequal design. No mutated source is committed.

To reproduce the main gates:

```bash
make lint
make coverage
python -m pytest hub/tests -q       # set HUB_TEST_DATABASE_URL to disposable Postgres 16
python scripts/export_openapi.py --check
python scripts/export_hub_openapi.py --check
bandit -r commontrace/ hub/ -x '*/tests/*,*/test_*' -ll
```

Use the strict mypy command and Graphify invocation in `.github/workflows/ci.yml`
and `.github/workflows/graphify.yml`. The timing result is a measurement of this
execution environment, not a deployment latency promise. Consult
[guarantees](guarantees.md) and the [feature tracker](frontier-roadmap.md) before
interpreting a certificate or enabling an external integration.
