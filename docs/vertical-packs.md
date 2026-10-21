# Vertical configuration packs

`unimem pack list` shows the eight versioned configuration bundles. Each
contains named roadmap feature coverage, defaults, review rules, and a docs page.
`unimem pack install NAME --dest PATH` writes the pack manifest and its
documentation without network access. `unimem pack demo NAME --seed N`
executes real local feature workflows in temporary namespaces, then runs a
deterministic seeded synthetic comparison. Its report includes the synthetic
outcome difference, 95% Wald interval,
observed harm-budget spend, and explicit guarantee/estimate labels. Every pack's
demo finishes well within the ten-second CI budget and makes no network calls.

Feature checks exercise the library APIs listed in each manifest. They include
fail-closed shield decisions, clearance-filtered recall, keyframe retention, and
safety-case revalidation. Simulated attestation is identified as simulated, and
transparent proofs carry no privacy guarantee. Checked clients refuse network
access for the duration of each workflow without changing process-wide settings.

The outcome estimate describes only its synthetic sample; it does not execute a
production tenant's integrations or establish field efficacy. Installable pack
pages are [coding-agent](../unimem/vertical_packs/coding-agent.md),
[support-outcome](../unimem/vertical_packs/support-outcome.md),
[bank-ai-governance](../unimem/vertical_packs/bank-ai-governance.md),
[healthcare](../unimem/vertical_packs/healthcare.md), [robot-fleet](../unimem/vertical_packs/robot-fleet.md),
[autonomy-safety-case](../unimem/vertical_packs/autonomy-safety-case.md),
[home-eldercare-robot](../unimem/vertical_packs/home-eldercare-robot.md), and
[sovereign-airgapped](../unimem/vertical_packs/sovereign-airgapped.md).
