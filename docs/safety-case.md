# Living robot safety cases

`commontrace safetycase` stores immutable claims, arguments, scenario records,
and evidence references in a local append-only journal. Evidence binds its
source digest and model/software/world-model digests. When a dependency changes,
affected evidence is marked stale and queued for a fresh simulation gate; a
simulation pass still requires a real-world trial before promotion. Coverage
reports show missing, weak, and stale evidence per hazard. They are review aids,
not a certificate or a substitute for a safety lifecycle.

Scenario mining accepts a caller-provided deterministic replay adapter. It keeps
only a failure or near-failure reproduced twice with matching canonical result
digests. Export emits the supported OpenSCENARIO DSL 2.1 template subset. The
built-in validator checks that subset only; use the ASAM toolchain for complete
grammar, semantics, and simulator validation. No simulator is bundled.

Example:

```sh
commontrace fleet safetycase claim '{"claim_id":"stops_for_person","hazard":"person_crossing","statement":"The configured robot stops before entering the pedestrian zone."}'
commontrace fleet safetycase coverage '{"required_hazards":["person_crossing"]}'
```

Evidence registration is an assertion by the caller about an external artifact.
CommonTrace binds the supplied digest; it does not independently attest a
certificate, test run, model build, or simulator result.
