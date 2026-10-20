# UniMem governed reinforcement-learning exports

Instrument each resolved randomized occasion with `rl_io.capture`. It binds the
actual prompt, response, task cluster, source labels and optional token log
probabilities into an authenticated trajectory receipt. Capture is instrumentation;
it does not prove that an agent executed the assigned treatment.

Export the complete admitted cohort, including unsuccessful occasions:

```sh
commontrace experiment export-rl '{"units":["memory-id"],"context":["team:release"]}' --format dpo
commontrace experiment export-rl request.json --format advantage
commontrace experiment export-rl request.json --format replay
```

The authenticated gateway accepts the same request plus `format` at
`POST /v1/experiment/export-rl`. It streams chunked `application/x-ndjson` and
returns `X-UniMem-Manifest`, the SHA-256 of the exact JSONL bytes. Container
credentials narrow admission; agent credentials cannot invoke the export route.
Information-flow labels preserve authenticated scope lists as well as legacy
scalar classifications. The existing encoded-canary egress detector blocks a
matching response before export and is checked again when streaming starts.
It is a bounded detector, not comprehensive data-loss prevention.

Each request is bounded to 64 units, 2,048 complete occasions and 256 content
shards. Missing, duplicate, unresolved, erased or unauthenticated responses fail
closed before streaming. The deployment must segregate scoped trial namespaces:
a caller cannot silently export only its visible subset of a mixed cohort.

DPO rows contain `prompt`, `chosen`, `rejected` and `meta`. Disjoint pairs match
the unit, task cluster, context, exact prompt and full information-flow labels.
They compare an actual successful response with an actual unsuccessful response
from opposite randomized arms. Metadata explicitly says these are observed-task
matches, not individual counterfactuals, and carries the population effect,
interval, propensities and segment.

Advantages subtract a doubly robust control baseline from the observed outcome.
Their expectation is the improvement of the logged treatment mixture over the
control policy, not an individual treatment effect. The separate AIPW score
estimates the treatment-versus-control effect. Nuisance values are fixed public
predictions of 0.5. Statistical interpretation requires complete outcomes, exact
propensities, stable effects and consistent delivery of the assigned arm.
Confidence sequences use an alpha split across exported units.

Replay scores are `(exploitation * max(0, lower) + exploration) * interval_width`.
Defaults are 0.8 and 0.2; normalized weights are a sampling heuristic. They are
not a guarantee of downstream policy improvement. Zero total scores use uniform
sampling. KL fields report the supplied mean sampled token log ratio, including
sampling error; they do not certify the full policy KL. Missing log probabilities
are marked `unchecked`. A sampled estimate above `kl_budget` blocks export.

`connector: "agent-lightning"` maps a record to state/input/action/output/reward,
`training_record` and metadata. `connector: "grader"` maps it to item/sample and
metadata. Both mappings are **unverified against vendor schema**. The generic
JSONL schema is tested locally; no vendor APIs are called.

Content stays in deletable, scoped shards; signed journals contain commitments
and source references. Every read rechecks source admission, and erasure purges
managed training content and invalidates affected manifests. Streaming checks
again at each shard boundary (at most 64 records). Bytes already delivered or in
flight require downstream erasure acknowledgement; deleting a local export does
not erase an external training run. `rl_io.status` flags invalidation and required
retraining rather than claiming that downstream work was undone.
