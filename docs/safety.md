# Protected hazard memories and safety evidence

Hazards require a content-bound review by an operator. Use
`commontrace safety mark ID --class zone|object|condition|human-proximity
--reviewer REVIEWER --reason REASON`. Lessons additionally require a current
lesson-admission receipt. Marking makes a reviewed lesson core and removes its
automatic expiry. Facts must be active without a scheduled end or expiry.

The signed registry excludes hazards from experimental assignment, optional
harm gates, compression, unattended dreaming/consolidation and forgetting/erasure.
Dosage and recall preserve their full bytes even when the optional context budget
is exhausted; operators must budget for mandatory safety context separately.
An operator must create a fresh review and explicitly `safety.release` protection
before editing a hazard, then review and mark its replacement.

MCP `safety_context(site, robot)` returns caller-admitted, scoped hazard context.
Gateway protected prefixes continue to work and registered hazards are protected
there too. Every retrieval still requires ordinary scope/authority admission.
Export the registry and recorder with `safety recorder export` for an offline
cache. The export is signed and includes a transparency head; pin keys and require
consistency when accepting later edge updates.

The Python safety recorder appends decisions, corrections, interventions and
software versions, retaining decision payloads for at least 366 days and tracing
metadata/interventions for at least five times 366 days. Records survive restarts
and are anchored in the transparency log. This release does not automatically
prune those records. `safety spi EPISODES.json` computes near-miss counts and
interventions per hour from bounded episode summaries; these are descriptive
leading indicators, not proof that a robot is safe.

Verify the Machinery Regulation 2023/1230 Annex III 1.2.1 wording against the
Official Journal. Planner constraints, fresh sensors, physical interlocks and a
human correction path remain necessary.
