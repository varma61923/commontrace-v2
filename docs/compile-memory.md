# Governed compilation

Install `commontrace[compile]` and supply a `Trainer` plugin for real open-weight
training. Targets are LoRA, trained KV prefixes and atomic fact edits. The core
CPU reference trainer emits deterministic token-count metadata solely to test
governance; it does not claim to implement production LoRA, Cartridge or
AlphaEdit training.

`frontier compile-build` requires approved, stable, causally positive source
lessons, their exact content digests, an explicitly open-weight model and a
bounded artifact manifest. Hazard memories remain explicit context. Each
artifact derives from its sources in lineage. `compile-status` rechecks admission,
bytes, evidence and taint; retraction immediately removes the artifact from the
serving interface, requiring rollback/recompilation.

Configure an experiment, then `compile-trial` preregisters an F2 change plan
bound to the artifact hash. `compile_memory.assign` compares compiled behavior
against retrieval of the exact same sources and uses that plan's canonical
assignment. Record actual final outcomes and issue `promote certify` for the
plan. `serve` refuses absent, revoked, mismatched or stale certificates. A trainer
plugin must honor the immutable manifest and the application must honor the
returned serving decision; files already copied to an unmanaged model process
cannot be recalled by a Python library.
