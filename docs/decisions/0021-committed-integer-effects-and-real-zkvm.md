# 0021: Complete cohort commitments and a real optional zkVM

Use one closed integer profile implemented independently in stdlib Python and
Rust. Register the complete cohort and hiding/noise commitments before a
verifier nonce and every outcome. Join managed witnesses to canonical outcomes;
never accept an arbitrary subset as a managed trial.

Keep transparent recomputation available without native dependencies. RISC Zero
3.0.6 verifies actual succinct/Groth16 receipts for an operator-pinned image and
binary; fake/development receipts and silent fallback are forbidden. Guest
compilation is conditional on its custom toolchain. The proof establishes the
estimator, not agent execution, reporter truth or seed entropy.

Use conservative Hoeffding intervals and explicit rounding bounds instead of
presenting a self-normalized estimate as unbiased. Optional finite-grid noise
suppresses exact auxiliary statistics and binds its seed before outcomes; one
immutable managed release prevents same-study retries from averaging noise.
Larger recursive circuits remain an explicit externally verified extension.
