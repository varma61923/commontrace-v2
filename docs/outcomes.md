# Outcome engine

Use `frontier outcome-correct REQUEST.json` with `rows`: each task has
`occasion_id`, binary `injected`, withholding `rate`, detector `prediction`,
`gold_probability`, optional `gold`, and explicit `environment` (`sim` or `real`).
Declare `propensity_floor` before sampling. Exact treatment and gold inclusion
probabilities are required; actively selecting gold without logging its inclusion
probability is unsupported. Gold selection must be independent Bernoulli sampling
conditional on the fixed predictions/outcomes. The residual correction removes
judge bias in expectation; the interval is deliberately conservative.

`frontier surrogate-fit` accepts `rows` with `experiment_id`, `signals`, `final`
and `environment`. An entire experiment is reserved for validation. `Surrogate`
returns agreement and `use_final`; call `monitor` with later paired predictions
and final outcomes and honor its fallback. Surrogates do not write final success
labels into the canonical experiment automatically.

`frontier sim-discrepancy` records `condition`, `simulated`, `observed` and
`valid_from` as a bitemporal fact and signed discrepancy entry. `simulation-weight`
returns a shrunk trust weight from prior paired discrepancies, with zero weight
for unknown conditions. This weight is a heuristic for a caller's simulator
signal, never permission to relabel simulation as real observations. Models and
corrected effects reject mixed sim/real input.
