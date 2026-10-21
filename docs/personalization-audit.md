# Personalization without replacing factual evidence

The audit randomizes an actual catalog containing one memory or a fixed memory
class. It grades three distinct effects: task outcome, factual accuracy and
agreement with the user. Agreement never substitutes for either of the first two.

```sh
unimem experiment sycophancy '{"action":"register","identities":["fact-example"],"domain":"science","context":["domain:science"],"queries":["conversion"],"query_texts":{"conversion":"How should I check a unit conversion?"},"horizon":2048}' --dest ./store
unimem experiment sycophancy '{"action":"expose","identity":"delta:YOUR_AUDIT_HASH","occasion":"science-001","query_id":"conversion","context":["domain:science"]}' --dest ./store
unimem experiment sycophancy '{"action":"report","identity":"delta:YOUR_AUDIT_HASH","context":["domain:science"],"apply":true}' --dest ./store
```

Registration requires admitted, non-hazard sources with exactly one explicit
matching `domain:` scope, preserving all other owner scopes. It freezes query
texts, cyclic matched-query order, source digests, horizon, margins and family
alpha before outcomes. Later audits spend a summable part of the first audit's
family budget. Actual delivery includes the frozen query and the canonical arm's
catalog; the adapter must honor both. A retry cannot change the query or catalog.
There is no randomized withholding of mandatory safety context.

Record independently graded secondary labels before the primary outcome:

```python
from unimem import holdout_io

holdout_io.record_outcome(
    root, "science-001", succeeded=True,
    secondary_metrics={"accuracy": 1.0, "agreement": 0.0},
)
```

The same optional `secondary_metrics` object is accepted by `MemoryClient.outcome`,
`CausalMemory.record_outcome`, `/v1/memory/outcome` and `/v1/outcome`. Both labels
must be present, finite and between zero and one. Secondary payloads are separately
stored with source-linked erasure and an ordered transparency commitment. Existing
primary outcome records and their hashes retain their original format. A missing
label cannot be filled in after the primary outcome; contradictory labels cannot
replace an existing observation. Old stores and calls without the option retain
their existing behavior.

## What a flag means

Three simultaneous fixed-horizon Hoeffding intervals use exact propensities and a
known constant nuisance baseline. The first preregistered horizon is the entire
decision cohort. Incomplete outcomes, missing labels or missing matched cells
cannot change the gate. These intervals permit a fixed completed look; they are
not confidence sequences for optional stopping.

A memory is flagged as **flattering** only when its agreement lower bound exceeds
the preregistered agreement threshold while its outcome and accuracy upper bounds
are at most the declared practical-benefit margin. This establishes small benefit
within that margin under the assumptions, not exactly zero effect. Lack of a
significant benefit on an underpowered trial cannot trigger the flag.

Applying a completed report down-weights the matching current source content in
the causal gate, retaining its configured exploration floor. This is a conservative
allocation heuristic for a class result, not an individual causal certificate for
every member. A newer cohort supersedes older applied evidence; reapplying an old
cohort cannot revive old factual permission. Current source content is rechecked.

Facts explicitly marked `memory_type="personal-opinion"` and registered personal
memory sources are excluded from `action_class="factual:DOMAIN"` (or
`"factual-claim"`) without a current non-negative accuracy lower bound. Existing
scope admission also blocks cross-domain reads. An unknown opinion does not gain
permission from missing evidence. A positive class effect cannot establish each
member's individual accuracy effect, so per-memory factual permission requires a
single-member audit. Opinion classification and factual action classes are explicit
application inputs; this does not claim to detect every opinion from free text.

Applied audit summaries are captured in incident evidence packs at freeze time.
The original domain and scope labels remain attached. Erasure of a source cascades
through audit, delivery, report and secondary metric payloads; retained journals
contain commitments and review restrictions, not raw memory text.

## Offline acceptance adapters

The adapters map field subsets checked against pinned primary documentation:

| Benchmark | Source pin | Mapped condition and licensing |
| --- | --- | --- |
| PersistBench | [`ivaxi0s/PersistBench@d2ded18`](https://github.com/ivaxi0s/PersistBench/tree/d2ded18be6d15ebb14008d081a44bd03c20670e4) | `query`, supplied `memories`, and the three failure types. No license file was present at this pin; resolve data-use rights before running real data. |
| MemSyco-Bench | [`XMUDeepLIT/MemSyco-Bench@3c3aa01`](https://github.com/XMUDeepLIT/MemSyco-Bench/tree/3c3aa013ac5bee2f0e252fe8748644edc7f9af0a) | Five canonical tasks, historical dialogue, query and separate grader annotations. Repository/data card specify MIT, with upstream source-dataset obligations such as TruthfulQA attribution. |

Datasets are downloaded by the operator and never bundled or downloaded by these
commands. `benchmark` reads a local bounded JSON/JSONL subset only after an explicit
`license_notice` and `license_confirmed: true`. The repository contains authored
synthetic schema fixtures, not benchmark examples.

```sh
unimem experiment sycophancy '{"action":"benchmark","name":"memsyco-bench","path":"./local-cases.jsonl","license_notice":"Reviewed local data card and upstream source obligations","license_confirmed":true}' --dest ./store
```

`sycophancy_io.acceptance` runs normalized cases through caller-provided answer and
independent grader functions, using previously registered audits with the exact
case query text. The answer function receives only the query and actual admitted
catalog. The grader receives its reference and the observed answer, then supplies
task success, accuracy and agreement. Completed occasions survive a restart without
regrading. No live benchmark score or model execution is claimed by normalization.

MemSyco's [data card](https://github.com/XMUDeepLIT/MemSyco-Bench/blob/3c3aa013ac5bee2f0e252fe8748644edc7f9af0a/data/README.md)
identifies `memory` and `evaluation` as gold annotations. They are never supplied
to the answer function. The operator constructs the audited memory from historical
dialogue independently of those annotations. The adapter implements this registered
retrieved-memory condition; it does not reproduce every upstream baseline or prove
the independence or factual correctness of external grader/model implementations.
