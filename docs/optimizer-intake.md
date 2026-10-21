# Optimizer proposal intake

UniMem treats an optimizer's edit as a hypothesis. Its claimed validation score
is retained as unverified metadata. A preregistered randomized trial decides
whether the edit helps, and the existing promotion gate issues the certificate.
UniMem never executes the proposed code; the application supplies a sandbox and
must deliver the returned catalog exactly and resolve every assigned outcome.

## Candidate formats

The generic JSON schema has exactly five fields:

```json
{
  "base_artifact": "release-program-v1",
  "delta": "Verify the checksum before deploying.",
  "optimizer": "external-reflector",
  "claimed_validation_score": 0.99,
  "parent": null
}
```

The base is an artifact reference supplied by the caller. Use `source_ids` for
canonical memories on which the edit depends: intake checks their scope, binds
their current admitted content, and refuses delivery after a source changes or
is erased. Protected hazard sources and protected base artifacts cannot become
optimizer trials. Artifact identities bind the base, edit, optimizer and parent;
claimed scores are excluded. The separate public pooling fingerprint hashes the
edit content, without tenant-private base references.

| Adapter | Input in addition to `base_artifact` | Verification |
|---|---|---|
| `generic` | The complete five-field object above | UniMem v1 |
| `gepa` | `gepa_result`: GEPAResult JSON containing `candidates`, `parents`, `val_aggregate_scores` | Verified serialization subset at commit below; no optimizer execution |
| `dspy` | `dspy_state`: named predictor states from `dump_state()` | Verified prompt-state subset at commit below; no SDK execution |
| `ace` | `bullets`: strings or `{delta, score, parent}` objects | UniMem export envelope; upstream format unverified |
| `training-free-grpo` | `experiences`: the same bounded edit objects | UniMem export envelope; upstream format unverified |
| `code-diff` | `diffs`: edit strings, normally unified diffs | UniMem interchange; no patch application or semantic verification |

GEPA also accepts an unverified normalized `candidates` edit envelope. DSPy also
accepts an unverified normalized `programs` envelope. A verified serialization
subset is not a claim of compatibility with every release or native object.

The pinned GEPA result schema is
[`f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819`](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/src/gepa/core/result.py).
Candidates are named prompt maps; parent indices must refer to earlier candidates.
Their scores and vendor Pareto selections never become causal evidence.

The pinned DSPy schema is
[`c7e9752debcce5715fee62cd5f1ae538c14d5ce4`](https://github.com/stanfordnlp/dspy/blob/c7e9752debcce5715fee62cd5f1ae538c14d5ce4/dspy/primitives/base_module.py),
with the matching `Predict.dump_state` and `Signature.dump_state`. The adapter
retains instructions, signature field descriptions and bounded demonstrations.
It drops LM configuration, credentials, URLs, custom classes, traces and training
runtime state. It never deserializes pickles or instantiates a supplied model.
Authored contract fixtures check these fields without shipping vendor code or data.

## Run an edit trial

Configure the experiment and a frozen conformal harm gate on matched, completed,
ungated pilot traffic before intake. The scorer name and scale must match the
candidate's declared `harm_scorer` and `harm_score`. An uncalibrated trial is
refused. The reference score is operator-supplied; it is not a learned guarantee.

```sh
unimem skill optimize intake '{"proposal":{"base_artifact":"release-v1","delta":"Verify checksum.","optimizer":"reflector","claimed_validation_score":0.99,"parent":null}}' --dest ./store
```

This returns a `delta:<hash>` unit and its prepublished allocation schedule. Once
the schedule is effective, the application obtains each occasion's actual arm:

```python
from unimem import holdout_io, optimizer_io

catalog = optimizer_io.expose(
    "./store", [candidate_id], "release-occasion-1", segment="default"
)
# Run the same sandbox task with precisely catalog["catalog"], including []
# for the control arm. Measure its outcome independently.
holdout_io.record_outcome("./store", "release-occasion-1", succeeded)
```

Each ACE bullet is a separate memory-level typed unit. Multiple candidates form
independent factors, with the joint delivered catalog recorded. Removing a
bullet on retry, changing the segment, or substituting a smaller catalog is
refused. Masked or harm-gated candidates are ineligible rather than fabricated
controls. A canonical harm alarm withdraws a bullet even when the reflector's
claimed score was high. G8 records observed source reads for delivered edits.

```sh
unimem skill optimize status '{"identity":"delta:<returned-hash>"}' --dest ./store
unimem skill optimize frontier '{"segment":"default"}' --dest ./store
```

`status` returns reflection-ready negative evidence, a minimum detectable effect,
observed IPW failure spend and the canonical report. To request a certificate,
set `certify: true`; every assignment must have an outcome, the harm calibration
must still match, and the canonical promotion criteria must pass. Changing the
calibration requires a newly versioned candidate/trial, not editing the old plan.
`pooled: true` adds G29 evidence matched by the public edit-content fingerprint;
it does not replace the local promotion certificate.

The MCP tool `submit_candidate` performs the same intake and returns format
status. Python callers use `optimizer_io.intake`, `expose`, `status` and `frontier`.
Delegated reads require the `optimizer-candidates` namespace and
`optimizer-delta` class as well as canonical source permissions; a facts-only
capability cannot disclose an optimizer artifact. Intake and certification require
an undelegated operator.

## What the numbers establish

The canonical estimator, integrity audit, e-process and promotion certificate
remain the evidence of record. Guarantees assume correct arm delivery, exact
logged propensities, complete bounded outcomes, fixed training criteria,
stationary traffic and no cross-episode interference. Segment membership must be
fixed before treatment. Low support produces uncertainty and an approximate
planning MDE, not proof of no effect.

The segment frontier is an estimate: it maximizes the estimated binary-outcome
effect and minimizes the conservative negative lower-bound risk coordinate.
Coordinates are explicitly projected into the binary-effect parameter space;
the original estimate and interval remain in the report. This is exploratory
selection, without a simultaneous optimality guarantee. Claimed validation
scores cannot enter any coordinate, gate or certificate.

Limits: 64 edits per intake/catalog, 16 preregistered segments, 64 canonical
sources, 60,000 characters per edit, 128 KiB per adapter input, and a 16-level /
10,000-node JSON limit. No vendor SDK or network dependency is added to core.
