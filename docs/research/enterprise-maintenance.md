# Enterprise maintenance: research, contracts and measurements

Review date: 2026-10-14. CommonTrace baseline: `3a41e8f` on
`codex/next-generation-complete`. The [existing competitive matrix](competitive-capabilities.md)
pins all eight supplied competitor checkouts and distinguishes source inspection
from hosted capabilities. This iteration uses that matrix, a refreshed CommonTrace
AST graph, Hindsight's checked-out worker implementation and current official
deployment/configuration documentation. No hosted competitor workload or paid
model evaluation was run.

## Capability decisions

| Area | Evidence and production gap | Category | Implemented contract |
| --- | --- | --- | --- |
| Background runtime and persistence | CommonTrace already queues ingestion, extraction, summaries and mental-model refresh in SQLite WAL. Its old completion/failure updates identified only the job, so a reclaimed worker could overwrite a newer attempt. [Hindsight's worker](https://github.com/vectorize-io/hindsight/blob/f843f173bb41f001e322cc83c917371639e47fbf/hindsight-api-slim/hindsight_api/worker/poller.py) explicitly tracks claimed operations, worker ownership and reconciliation. | Critical operational reliability | Unique attempt tokens fence worker completion, failure and renewal; live handlers renew leases; crash loops stop at the attempt budget. Existing queue files migrate transactionally. |
| Summary maintenance and developer overhead | CommonTrace's rolling selector ranked every candidate before retaining twelve. The selection prefix is all that its persisted state and default answer context need. Hindsight documents precomputed mental models and separate concurrency caps for background work in its [configuration reference](https://hindsight.vectorize.io/developer/configuration). Those surfaces do not establish comparative summary quality. | Competitive differentiator | Stop selection at the retained/output prefix; retain exact text, candidate order, temporal/novelty scoring and evidence state. Add an executable paired workload and CI gate. |
| Enterprise tenant security | [PostgreSQL's RLS documentation](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) explains that disabled RLS leaves policies installed and owners normally bypass policies without FORCE. CommonTrace counted policies but did not inspect those states, and a required diagnostic could fail open on catalog/connectivity errors. | Critical deployment safety | Required startup verification refuses unknown status, disabled policy-backed tables and owner bypass. Compose, Kubernetes and Helm explicitly require verified enforcement. |
| API and configuration ergonomics | CommonTrace already has OpenAPI generation, external-IdP MCP OAuth discovery, linked identities, SCIM, retention/holds and deployment assets. New queue behavior must not alter public job JSON or require a hand-maintained SDK. | Table-stakes compatibility | Preserve job JSON, keep claim tokens private, expose a tested worker lease CLI option, and retain generated OpenAPI/SDK pipelines. No new HTTP endpoint or manual SDK is introduced. |

These changes address implementation defects and operational contracts. They do
not establish competitive answer accuracy, regulatory certification, a universal
10x gain, or validation of a customer's cloud deployment.

## Architecture evidence

Graphify extracted CommonTrace's code AST into a scratch directory outside Git.
Queries and call paths connected the CLI/daemon to `jobs.run_pending`, claimed
work to lease renewal, rolling summaries to `_select_candidates`, and Hub startup
to `check_row_level_security`. Optional Terraform/SQL parsers were unavailable in
the local extraction; those files were inspected directly and existing dedicated
CI validation remains authoritative for their declared contracts.
Helm default/optional rendering and AWS/GCP Terraform initialization, validation
and formatting also passed locally using the checksum-verified versions pinned
in CI. Local `make deploy-validate` now supplies its required image repository;
Alembic declares portable path separation without its legacy warning.

Reproduce the code graph with:

```bash
graphify extract . --code-only --max-workers 2 --out /tmp/commontrace-architecture
graphify query 'background jobs leases summaries' --graph /tmp/commontrace-architecture/graphify-out/graph.json
graphify explain commontrace_jobs_run_pending --graph /tmp/commontrace-architecture/graphify-out/graph.json
```

## Measured rolling-summary workload

`python -m benchmarks.session_summary` creates chronological operational notes in
a real conversation Store. The baseline restores the former full-candidate ranking;
the candidate selects the needed prefix. Both include SQLite paging, evidence hash
chains, summary persistence and matching force/rebuild semantics. Arms alternate
order over three trials. The report includes every timing sample, output/state
fingerprints and a source hash for reproducing the implementation.

Measurements on the shared development executor:

| Session turns | Candidate sentences | Reference median | Optimized median | Speedup | Exact persisted state and text |
| ---: | ---: | ---: | ---: | ---: | --- |
| 100 | 200 | 24.94 ms | 6.72 ms | 3.71x | Identical |
| 1,000 | 2,000 | 3,065.23 ms | 99.33 ms | 30.86x | Identical |
| 2,000 | 4,000 | 7,545.71 ms | 104.53 ms | 72.19x | Identical |

The [raw reports](measurements/enterprise-summary.json) include timing samples,
fingerprints and the implementation hash. The 1,000-turn report was repeated
during concurrent validation; executor load affects absolute timings.

This is an operational-note simulation using the production rolling-summary path,
not customer traffic. It excludes model inference, network latency, competitor
systems and other CommonTrace operations. Shorter inputs have less redundant
ranking work; the 100-turn result does not meet 10x. Transient collection still
scales with new source sentences; durable state is bounded to twelve candidates
and the existing vocabulary limit. CI enforces output equality and at least 10x
on the named 1,000-turn workload, rather than applying that claim to the product.

## Acceptance contracts

- [Job regressions](../../tests/test_jobs.py) cover competing workers, stale
  completion/failure/renewal, retry-token reuse, expiry during lock acquisition,
  automatic heartbeat renewal, crash-budget exhaustion, old-schema migration,
  invalid leases and oversized-result failure.
- [Summary equivalence](../../tests/test_conversation_summary_scaling.py) compares
  persisted state against the full-ranking prefix for default and larger output
  requests; the [paired-workload test](../../tests/test_session_summary_benchmark.py)
  checks restoration of benchmark hooks and bounded inputs.
- [Database enforcement](../../hub/tests/test_row_level_security.py) exercises
  required verification errors, disabled RLS, owner bypass and FORCE behavior
  against PostgreSQL. [Manifest checks](../../hub/tests/test_deployment_roles.py)
  keep all shipped server profiles in required-enforcement mode.
- [Hook transport regressions](../../tests/test_hook_bridge.py) distinguish
  deadline expiry from functional receipt ordering. The receipt test runs an
  isolated process with explicit integration scheduling headroom and holds the
  peer open until client exit. Production 25/20 ms deadlines and latency gates
  remain unchanged. Capture-only checks await the specific asynchronously
  persisted session instead of assuming persistence finishes with stdout.

See the [enterprise operations guide](../enterprise-readiness.md) for upgrades,
idempotent side effects, monitoring and the remaining customer-specific rehearsal.
