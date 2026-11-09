# Production audit — 2026-11-09

Branch: `feat/effect-field`. Starting revision: `c381309`.

This pass combines a local Graphify dependency map, source review, deterministic
failure reproductions, static analysis, dependency inventories, and executable
contracts. Review covered the effect field and causal/reward arithmetic, measured
learning on SQLite and PostgreSQL, source grounding and authorization, durable
robot outcomes, capsule lifecycle, asynchronous stores, gateway framing/deadlines,
HTTP cleanup/retry ownership, and SDK contracts. Generated graphs, logs, virtual
environments and the disposable PostgreSQL instance are outside tracked source.

## Fixed findings

| Area | Trigger and resulting behavior |
| --- | --- |
| Learning source grounding | A parent could be revised or retired between optimistic source admission and the learning write transaction, while the leaf revision remained unchanged. The final check now applies canonical visibility and recursive grounding on the same SQLite snapshot protected by `BEGIN IMMEDIATE`. Assignment, delivery and policy evaluation refuse the changed prerequisite and roll back their writes. Six deterministic interleavings cover parent revision changes and retirement across all three operations. |
| Bounded evidence revalidation | The first evidence read was bounded, but the second read materialized the entire concurrently growing cohort under the write lock. Both reads now use the same `limit + 1` bound. A concurrent addition still changes the comparison and forces a retry; the evaluator never silently samples an oversized cohort. |
| Raw reward arithmetic | Reciprocal probabilities and squared weights could overflow, producing an exception or NaN effect/intervals. The reward-scale estimator now forms each arm's weights as `p_min / p`. Common scaling preserves relative weights, the Hajek mean and its variance. Independent expected means and standard errors cover probabilities of `1e-200` and `1e-310`. Stored reward values must also satisfy the finite fraction contract. |
| Experiment settings publication | Truncating `experiment.json` exposed an empty/partial file to concurrent readers, which then used the default salt/rate. An interrupted write also damaged the previous configuration. Settings now use the existing atomic temporary-file/rename writer, retaining writer serialization and descriptor fsync. Readers see the complete previous or replacement settings. Failure injection verifies preservation and temporary-file cleanup. |
| Configuration validation | Boolean/non-numeric rates, non-string salts and object-valued notes could disable an experiment accidentally, cause an unexpected TypeError, or persist unusable settings. Validate before filesystem changes and report ValueError consistently. Valid existing configurations and the generated-salt behavior are preserved. |

The learning check adds recursive admission work while holding the SQLite writer
reservation. This is necessary to prevent canonical parents changing between
validation and commit; existing grounding depth/work limits still apply. It does
not establish a distributed transaction with external relationship providers or
file-backed policy services. Numeric work and checkpoints retain their existing
transactional rollback behavior. No public endpoint or persisted record format
was changed.

## Graphify, unwired code and removals

Installed Graphify 0.9.92 from the same immutable upstream revision as CI:
`86c57be8204673c2a1b9849a3292a7dd3ab373d8`. Its parser dependencies are isolated
from UniMem's code extra. Extraction uses `GRAPHIFY_BACKEND=none`, `--code-only`
and `--no-cluster`, so no LLM backend receives repository content.

The starting map has **29,068 nodes, 101,082 edges, 404 core Python modules and
274 possible unreachable callables**. The graph integrity and existing reviewed
surface budgets pass. Scoped queries located the learning revision-check caller
and inspected suspected disconnected helpers against source. Representative
candidates include `ReadPool` construction/cleanup/fork callbacks, the default
agent executor, import-time native-dot selection, robot adapter methods, browser
listeners, and standalone reference benchmarks. These have dynamic, public or
command-line uses; static reachability alone is not a removal criterion.

Re-extraction of the refactored production source retains **404 modules and 274
possible unreachable callables**, with no missing endpoints, integrity violations
or budget violations.

The refactor removes direct destructive settings writes, the unbounded second
evidence fetch, the leaf-only validation branch, and overflow-prone reciprocal
arithmetic. Ruff checks undefined names and unused imports throughout the
repository. No unrelated public adapter or callback was deleted.

Reproduce the final architecture gate:

```sh
GRAPHIFY_BACKEND=none graphify extract . --code-only --no-cluster --max-workers 2 \
  --out ../unimem-architecture-final
python scripts/graphify_audit.py \
  --graph ../unimem-architecture-final/graphify-out/graph.json \
  --output ../unimem-architecture-final/audit.json \
  --source-root . --max-modules 404 --max-unreachable 318
```

## Verification

The grounding and numerical regressions were first run against the starting
implementation: all three retirement interleavings failed to raise the required
conflict, and both extreme-probability examples failed with overflow/NaN. The
configuration regressions reproduced transient default settings and damage after
an interrupted write. Assertions were then rerun against the refactored source.

Completed local checks before publication:

- Learning/backend/transport/robot/reward selection: **171 passed**. After adding
  active-parent revision cases, all **six** grounding interleavings and the
  concurrent cohort-growth regression pass (**seven** focused cases).
- Experiment, writer holdout, effect field and causal gate selection:
  **266 passed**, including all twelve configuration publication/validation cases.
- Final reward/configuration selection after both refactors: **67 passed**.
- TypeScript SDK build and **51 tests** pass.
- Repository Ruff lint/format and `git diff --check` pass. Mypy reports no issues
  in **644 source files**, with its normal untyped-body limitations.
- Gateway/SDK and Hub OpenAPI exports are current. The measured-learning stream
  benchmark passes `--check`; all six public conformance-vector checks pass.
- Bandit reports no medium/high findings in production Python. The installed
  Python environment, Hub transitive lock and TypeScript production dependency
  inventories report no known vulnerabilities. These are point-in-time scans.
- Selected GitHub-token, AWS-key, live-Stripe-key and private-key-header patterns
  in tracked files matched only test fixtures; no credential values are included
  in the inventory or this report. This is not a history-wide secret scan.

The broad core/end-to-end coverage suite and complete Hub suite were started
before the implementation edits; they serve as baseline diagnostics. Focused
checks above verify the changed source. The branch's GitHub Actions runs verify
the final pushed commit, including the supported Python matrix, coverage gates,
optional engines, browser accessibility, SDKs, deployment assets, supply-chain
checks and Graphify budgets. Check the exact commit's runs on
[PR #186](https://github.com/varma61923/unimem/pull/186) for final CI evidence.

Live customer data, hosted identity/payment/model providers, robot hardware and
native RISC Zero proving are outside this local audit. A green test suite and
static graph do not prove the absence of defects in those integrations.
