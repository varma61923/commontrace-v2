# Vertical configuration packs

`commontrace pack list` shows the eight versioned configuration bundles. Each
contains named roadmap feature coverage, defaults, review rules, and a docs page.
`commontrace pack install NAME --dest PATH` writes the pack manifest and its
documentation without network access. `commontrace pack demo NAME --seed N`
runs a deterministic seeded synthetic randomized comparison and prints a
one-page buyer report with the measured outcome difference, 95% Wald interval,
observed harm-budget spend, and explicit guarantee/estimate labels. Every pack's
demo finishes well within the ten-second CI budget and makes no network calls.

The demo estimates only its own synthetic sample; it does not execute a
production tenant's integrations or establish field efficacy. Installable pack
pages are [coding-agent](packs/coding-agent.md),
[support-outcome](packs/support-outcome.md),
[bank-ai-governance](packs/bank-ai-governance.md),
[healthcare](packs/healthcare.md), [robot-fleet](packs/robot-fleet.md),
[autonomy-safety-case](packs/autonomy-safety-case.md),
[home-eldercare-robot](packs/home-eldercare-robot.md), and
[sovereign-airgapped](packs/sovereign-airgapped.md).
