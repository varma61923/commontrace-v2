# Frontier validation record

The roadmap is covered by seeded statistical tests, adversarial store tests,
CLI/MCP/gateway contracts, and PostgreSQL-backed Hub tests. Commands below use
Python 3.12 unless stated otherwise. Optional integrations use mocks or local
fixtures; no live vendor request, payment, enclave or robot actuation was tested.

| Check | Result |
|---|---|
| Ruff and whitespace | Pass |
| Configured strict mypy list | 65 modules pass |
| Bandit `-ll` | No medium/high findings |
| Added dependency requirements audit | No known vulnerabilities |
| Both generated OpenAPI contracts | Current |
| Hub image import allowlist | 11 tests pass |
| Alembic | One head: `fa02b3c4d5e6` |
| Graphify | 346 Python modules, 288 reachability candidates, no missing endpoints; within reviewed budgets |
| Local-tier latency gate | 119.5 ms at 6,400 lessons, fitted total exponent 1.30; limits 500 ms / 1.5 |
| Accumulated core/dev coverage | 85.27%, above the 80% gate |
| Final core/dev regression and feature run | 191 pass, including all failed cases from the broad coverage run |
| Core-only Python 3.10 repair run | 65 pass, 14 optional dependency skips |
| Isolated Python 3.10 native hooks | 43 pass |
| Hub console and incident workflow | 207 pass |
| Final Hub API, tenant fuzz, incident and image contracts | 33 pass, 1 optional skip |

The broad core/dev sweep exercised 8,025 passing tests; its six failures were
fixed and rerun in the final passing regression run. The broad Python 3.10 sweep
exercised 7,367 passing tests; absent-extra guards and the reported regressions
were repaired and retested. The broad PostgreSQL 16 sweep exercised 2,601 passing
tests; its console/OpenAPI and tenant-fuzz failures were fixed and included in
the final Hub contract run. The final tree has not had another complete fresh
sweep; the PR's CI is the authority for that rerun. Native hooks have 20–25 ms
deadlines and were additionally checked without competing bulk suites.

Statistical mutation checks used isolated source copies. Removing gold-label
inclusion weights, factorial contrast weights, cluster HT weights, the RATE
validation condition, or the replay cost cap made the corresponding regression
test fail. Earlier predictable-betting and conformal IPW mutations also failed
their seeded tests. No mutated source is committed.

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
