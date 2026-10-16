# Frontier implementation tracker

Features follow the requested phase order. Implemented interfaces and tested specializations
are distinguished from deployment integrations in the linked workflows.

| Feature | Implemented capability | Workflow |
|---|---|---|
| F11 Authenticated lineage | Signed derivations, origin quorum, minimum integrity and taint cascades | [Details](lineage.md) |
| F12 Transparency log | Signed Merkle heads, inclusion/consistency proofs, tenant-isolated Hub history | [Details](transparency-log.md) |
| F3 Sequential evidence | Predictable e-processes, directional shared-task e-BH, nonoverlapping evidence snapshots | [Details](e-processes.md) |
| F1 Harm-budget admission | Opt-in conservative CRC/LTT gates and trajectory escalation | [Details](conformal-gate.md) |
| F15 Incident evidence | Freeze, legal hold, two-clock replay and signed evidence packs; Hub routes | [Details](incident.md) |
| F2 Change control | Preregistered canonical trials, frozen support, anytime certificates and rollback | [Details](promotion.md) |
| F18 Safety evidence | Reviewed protected hazards, durable recorder and intervention SPI | [Details](safety.md) |
| F8 Causal lifecycle | Randomized rewrite overlays, losing-rewrite rollback and learned utility budgets | [Details](causal-dreaming.md) |
| F9 Context economics | Opt-in value knapsack and deterministic proven-memory cache prefixes | [Details](context-packing.md) |
| F13 Subject-key erasure | Independent encrypted envelopes, key destruction, local cascade and conditional certificates | [Details](certified-erasure.md) |
| F14 Pre-action defenses | Quoted low-integrity data, scoped derivation, sensor checks, canaries and shadow red teams | [Details](memory-defenses.md) |
| F5 Outcome engine | Held-out surrogate agreement, active-gold residual correction and separate sim/real discrepancies | [Details](outcomes.md) |
| F16 Recovery registry | Calibrated signatures, exact fleet context, randomized lift ranking and withdrawal | [Details](recovery.md) |
| F4 Fleet interference | Cluster design, lineage write diversion, switchback burn-in and HT monitoring | [Details](interference.md) |
| F19 Valuation | Factorial value certificates, capped warranty ledger and underwriting/training metadata exports | [Details](valuation.md) |
| F17 Spatial objects | Bounded bitemporal deltas, corrections, historical beliefs and three planner MCP tools | [Details](spatial.md) |
| F6 Targeting | Disjoint DR learning, conservative RATE admission, positive-class transfer and exploration | [Details](uplift.md) |
| F7 Attribution | Budgeted randomized replay masks, sparse culprit diagnostics and restore audits | [Details](attribution.md) |
| F20 Clean room | Sealed runner interface, attested escrow, duplicate-cluster royalties and Laplace budget accounting | [Details](cleanroom.md) |
| F10 Compilation | Open-weight trainer contract, CPU governance reference, source rollback and artifact-bound F2 trials | [Details](compile-memory.md) |
| F21 Foreign stores | Pinned SDK/HTTP adapters, canonical measurement, lineage and redacted errors | [Details](vendor-evidence.md) |
| F22 Physical-AI bridge | Lazy ROS node, MCAP/LeRobot metadata ingest, witnessed edge cache and durable outcomes | [Details](physical-ai.md) |

The implementation uses bounded, conservative specializations where the full
research algorithm would require additional assumptions or dependencies. See
[guarantees](guarantees.md) for the exact claims. Production trainers, enclave
execution, vendor API authentication/version translation, native tensor/media
conversion, robot controls and remote backup erasure remain deployment plugins.
These are explicit boundaries, not simulated production integrations.

The `frontier` CLI accepts a bounded JSON request and `--dest STORE`. Run
`commontrace frontier --help` for the fixed operation list. Dedicated `lineage`,
`log`, `incident`, `promote`, `safety`, `experiment`, `allocate`, `dream`,
`retrieval` and `defense` workflows remain available. Existing stores keep their
canonical legacy hashes and defaults.

See the [validation record](frontier-validation.md) for executed checks and their limits.
