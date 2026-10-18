# Transfer quarantine and forgetting sentinel

Enable the sentinel only after the existing canary has obtained its canonical
promotion certificate. Configuration, source registration, update registration
and ledger access require an undelegated operator. A new store remains disabled.

```sh
commontrace sentinel configure '{"alpha":0.05,"baseline_failure":0.1}' --dest ./store
commontrace sentinel register '{"source":"fact-example","origin":"support","segments":["support","coding"]}' --dest ./store
commontrace sentinel status '{"source":"fact-example"}' --dest ./store
commontrace sentinel quarantine '{"source":"fact-example","incident_id":"review-001"}' --dest ./store
commontrace sentinel ledger ledger-request.json --dest ./store
```

`register` pins the admitted source bytes, salt, existing assignment prefix and
segment family before the monitored outcomes. A source family cannot be changed
or restarted after observing outcomes. Each new source spends
`alpha / ((j+1)*(j+2))`, where `j` starts at zero. The sum never exceeds the
configured alpha. Old evidence cannot trigger this prospective test.

For each target segment, the crossing evidence is the minimum of the origin's
benefit e-process and that target's harm e-process. e-BH handles their shared
origin evidence at a common stopping time; neither products of dependent
e-values nor running maxima are supplied to e-BH. This controls false quarantine
under the stated conditional directional nulls, logged exact propensities,
complete outcomes and no interference. It does not establish safety when the
test has little power. MDE is explicitly an approximation for planning.

The read path, induction, compilation, reflection, dreaming, consolidation,
portable publication, governed handoff, edge updates and exports enforce retained
quarantine before producing generalized content. Source checks also run at these
boundaries before quarantine has been persisted. Delegates fail closed and cannot
change operator quarantine state. A source change or incomplete cohort blocks the
boundary pending review rather than authorizing generalization.

Quarantine taints descendants and publishes an optional scoped change-stream
event. Its journal retains hashes and scope labels, not private source bodies.
The optional incident freezes the existing evidence before read restriction.
Observed contagion radius is captured before quarantine; uninstrumented reads are
not covered. Mandatory hazard context remains explicit and uses the existing
operator safety workflow. Quarantine is a review restriction, not byte erasure;
canonical erasure checks continue to operate independently.

## Update ledger

The ledger accepts `action: "register"`, `"expose"` or `"status"`.
Registration requires the actual JSON `payload`, an operator-pinned `signer`, an
admitted artifact `canary` reference and its existing content-bound `certificate`.
`source_ids` links other admitted source memories for erasure propagation.
The payload commitment becomes `update:<sha256>`. Payload encoding must match
the existing promotion artifact hash exactly: an optimizer's base/delta/optimizer/
parent object, a skill's canonical body string, a bundle's member list, or the
compiled payload/retrieval-sources object. These existing hash formats do not change.

`expose` takes `update` and `occasion`; it independently draws the canonical
update and proven-canary factors and returns the actual payloads, or `null` for a
withheld factor. The deployment adapter **must honor both returned arms**, execute
in its own controlled environment and record their common actual outcome with
`holdout_io.record_outcome`. UniMem does not deploy arbitrary code or claim to
verify an uninstrumented external deployment. Retries preserve the exact catalog.

`status` reports the canary interaction: its randomized effect with the update
minus its effect in control. The interaction parameter ranges over `[-2,2]`.
Its HT score bound uses **both** factor propensities, so it is four at balanced
allocation. Confidence inversion rescales scores and that true bound together.
Every factorial cell needs the preregistered minimum support. A lower bound above
negative tolerance is necessary for release; missing evidence cannot pass. The
canonical promotion test remains an additional requirement. Enabling the sentinel
makes this ledger mandatory for new typed delta/skill/bundle/update promotions and
compiled-artifact serving.

Matching token logprobs optionally produce a sampled log likelihood ratio to the
base; absent logs remain `unchecked`. This is not an exact distributional KL.
Digest and signer are operator pins, not an inferred external signature attestation.
The four model/memory/tool/workflow monitors use frozen pre-treatment context
labels and a frozen conditional failure baseline. Their alarms flag degradation;
attribution of its cause remains a heuristic.

The seeded regression simulation quarantines a planted cross-segment poison and
preserves a beneficial heterogeneous memory. Statistical tests, actual canonical
factorial delivery, incomplete evidence, source changes, authority, scope and
quarantine-before-generalization are tested separately. Synthetic results do not
establish performance on a customer's deployment.
