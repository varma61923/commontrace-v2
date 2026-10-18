# Decision 0028: living robot safety case

Represent hazards as claims connected to evidence that names source and
dependency digests. Invalidate evidence when a referenced model, software, or
world model changes, and queue it for revalidation. Mine only reproducible
simulator failures and near-failures. Require explicit coverage reporting and
keep the generated OpenSCENARIO template grammar deliberately bounded.

The graph is auditable evidence management, not a functional-safety certificate.
Simulation eligibility never substitutes for real-world trial evidence.
