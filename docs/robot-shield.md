# Bounded STL robot monitor

`commontrace fleet shield spec` registers a bounded discrete-time Signal
Temporal Logic formula against a memory ID. Predicates compare named numeric
signals. `always`, `eventually`, and bounded `until` use closed intervals in
seconds over timestamped samples. Robustness is computed from predicate margin;
non-negative values pass. Missing referenced memory, missing signals, malformed
or out-of-order telemetry, and negative robustness hold the action. A caller
supplies the failure type and exact site/robot/embodiment/environment context;
the monitor resolves fallback memories through the scoped recovery registry and
surfaces only reviewed hazard-protected candidates. The robot still holds for
the controller to apply its own recovery policy.

This monitor evaluates caller-provided traces and stores the verdict and digest.
It does not control motors, sample sensors, certify a robot, or replace the
robot's independent safety-rated controller, emergency stop, or risk assessment.
Validate the full installation against applicable standards, including the
currently adopted ISO 10218 edition, before deployment. The monitor complements
and never replaces ISO 10218 functional safety.

```sh
commontrace fleet shield spec '{"spec_id":"clearance","memory_id":"stop_rule","embodiment":"arm-v1","description":"Maintain 0.5m clearance","formula":{"op":"always","from":0,"to":1,"arg":{"op":"predicate","signal":"distance","cmp":">=","value":0.5}}}'
commontrace fleet shield check '{"spec_id":"clearance","action":{"grasp":"start"},"source_memory_ids":["stop_rule"],"trace":[{"t":0,"distance":0.8},{"t":1,"distance":0.6}]}'
```
