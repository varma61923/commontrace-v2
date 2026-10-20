# Recovery registry

`recovery.signature` calibrates a detector against prior binary failure labels
and carries a bounded structured explanation and scene description. Calibration
is an empirical local estimate; it does not certify collision avoidance.

1. Configure a local randomized experiment with `commontrace experiment`.
2. Run `commontrace frontier recovery-register REQUEST.json --dest STORE` with
   `failure`, `recovery`, and `context` containing exactly `site`, `robot`,
   `embodiment`, and `environment` (`sim` or `real`). Optional `source_episodes`
   retain derivation links.
3. Call `recovery.trial(root, id, occasion, context=...)` before executing the
   recovery. Record actual final outcomes with `holdout_io.record_outcome`.
4. MCP `recall_recovery(signature, context)` returns measured lift, interval and
   sample count. Exact context ownership is checked before matching or ranking.
   Harmful recoveries are withdrawn; unmeasured effects are null.

The seeded fleet contract exercises useful and harmful recoveries and a protected
hazard. Hazard memories are always delivered and create no fictitious randomized
assignment. `interventions_per_hour` uses measured episode durations; zero
exposure returns no rate. A recovery suggestion is planner context, not a motor
command or an authorization to bypass a robot interlock.
