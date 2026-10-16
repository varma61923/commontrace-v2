# Memory Change Control Plan and promotion

A change plan fixes the candidate/incumbent policies, allowed change types,
minimum margin, alpha, support requirement, propensity floor, training size,
monitoring and rollback instructions before any trial outcomes. These criteria
cannot be changed during certification. Register it with `commontrace promote
register REQUEST.json`; request shapes follow `promotion.register`.

`promote evaluate REQUEST.json` reports the doubly robust difference and betting
confidence sequence. Its fixed initial training slice estimates nuisance means
and support. Unsupported contexts copy the incumbent (SPIBB); the deployed
supported policy, including its incumbent fallback, is what the certificate hashes.
Repeated looks never move the training/evaluation boundary.

`promote certify REQUEST.json` issues a signed certificate only when the lower
bound exceeds the preregistered margin, and anchors it in the transparency log.
`promote rollback REQUEST.json` records revocation. `allocation.plan` and
`commontrace allocate plan --require-certificate` refuse a Thompson schedule
without a matching unrevoked certificate for the actual proposed probabilities.

The finite-sample anytime claim assumes stationary task/context traffic, exact
logged propensities above the declared floor, binary bounded outcomes, consistent
actions and no interference. Certificates cover the measured supported policy,
not arbitrary unseen behavior. Evaluate model/embedding upgrades through a
randomized shadow-trial action whose actual outcomes are logged; off-policy
memory-inclusion data alone cannot identify a new model's behavior. Offline
signature verification authenticates issuance; current revocation needs a fresh
trusted log checkpoint.

For issuance, configure the experiment before registration, then use
`promotion.assign(root, plan, occasion, context=...)` for each actual shadow-trial
action. Supplied certification rows must exactly equal canonical trial rows;
assignments and outcomes must have transparency anchors after registration.
Compilation plans additionally bind `artifact_hash`; this field is omitted for
ordinary policies, preserving their existing canonical shape.

Certification requires every already-assigned trial occasion to have its final
outcome. Selectively completing only favorable occasions cannot issue a release
certificate. Pause new assignments and drain delayed outcomes before issuance.
