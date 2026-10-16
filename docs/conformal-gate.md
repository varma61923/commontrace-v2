# Opt-in conformal harm budget

Enable `commontrace retrieval --harm-budget 0.02 --per-context --dest STORE`
(optionally `--harm-method ltt` and `--propensity-floor P`); turn it off with
`--disable-harm-budget`. `commontrace allocate gate` reports the calibrated
threshold per stratum and the realized IPW injected-failure rate on occasions
logged while the gate was on; MCP retrieval includes compact `harm_budget`
metadata (the threshold in force for that context). Without this configuration,
admission behavior is unchanged. An empty calibration set rejects optional memory
rather than inventing a threshold.

Calibration is computed once, when the budget is configured (rerun the command to
recalibrate), and admission only reads that snapshot, so the gate adds no
log-scanning work to retrieval. It uses randomized single-memory occasions logged
while no gate was filtering: rows admitted under a gate only cover scores above its
threshold, and would bias every lower threshold's estimated loss downward. The
snapshot records the scorer label whose relevance scale it was fitted on; a
retrieval scored by a different scorer abstains. Contexts are normalized exactly
as the holdout log records them. Core (operator-pinned, never randomized) and
reviewed hazard memories are outside the discretionary budget. In `recall`, the
budget applies to the lessons channel, on its logged relevance scale; facts, graph
and conversation channels are not randomized injections and are reported as
outside its scope rather than gated on an incomparable fused score.

Randomized parallel arms do not identify whether the same task would succeed
without a memory and fail with it. The gate therefore bounds individual harm by
injected-arm failure, using exact logged propensities. It is conservative even
when a task would have failed either way. The calibration score is a fixed bounded
transform of the pre-task relevance score, applied identically at admission.

CRC controls **expected marginal discretionary loss**, averaged over calibration
and a fresh task, under exchangeability, bounded outcomes, exact propensities,
consistent delivery and no interference. It does not promise that every finite
stream's realized harm fraction is below alpha. Finite preregistered strata can
be calibrated separately; unseen strata abstain. Shared-outcome multi-memory rows
are excluded from automatic calibration rather than counted as independent tasks.
Bundled policies need their own task-level randomized trial.

The Python Learn-then-Test mode handles finite families of metrics and thresholds
with a simultaneous high-probability bound. `TrajectoryAlarm` uses a mixture of
restart e-processes and halts when a conditional step-risk null is rejected.
Online threshold updates under drift are labelled heuristics and require fresh
calibration before making a new exchangeability-based claim. Robot action sets
ask for human input when the conformal set is ambiguous or empty.

Reviewed hazards are outside this discretionary-memory budget and always bypass
it. Fresh sensors, planner controls and physical interlocks are still required.

Declare `propensity_floor` before calibration. The concentration bound uses this
fixed design limit, never the minimum propensity found after looking at data.
Unknown contexts abstain. Hazard exemption is independent of this budget.
