# UniMem writer contribution trials

Writer holdouts estimate the effect of making an authenticated writer's
non-hazard memories available to a team. They are disabled unless the operator
registers a writer and the integrator explicitly opens a parent reader context.
Legacy unsigned records have no authenticated writer and cannot be attributed
by guessing an author from their text.

```sh
commontrace team register-writer '{"writer":"reviewers","members":["agent-a","agent-b"]}'
commontrace team writers
commontrace team mask '{"occasion":"release-17","memories":["fact-id"]}'
commontrace team reliability '{"writer":"reviewers"}'
```

Configure the canonical experiment first. Registration publishes an immutable
typed `writer:<id>` trial and an allocation schedule; wait until its effective
time. Deliver exactly the returned arm and report the occasion outcome once.
The Python integration wraps the entire team episode:

```python
with writer_holdout.reader(root, occasion, context="release"):
    # Canonical reads and CausalMemory honor the parent intervention.
    memories = causal_memory.recall(query, occasion_id=occasion)
```

The writer factor is assigned before all memory factors. A masked writer's
memories and authenticated descendants are ineligible for the inner trial;
they are never recorded as memory controls. Their eligibility journal says
`ineligible (writer-masked)`. Protected hazards stay visible, including when a
caller pins or graduates other memories. Do not select the parent arm based on
outcomes or open a different reader context midway through the episode.

Estimates reuse canonical AIPW/e-process analysis. Interpretation requires
exact propensities, complete outcomes, consistent delivery and stationary team
traffic without interference across occasions. Parent effects describe the
writer's contribution under the other randomized factors; inner memory effects
are conditional on eligibility. They answer different questions.

Reliability shrinks the estimated contrast toward zero with a configurable
prior strength (default 20 occasions). The `writer_reliability` MCP tool exposes
the same report. Explicit `retrieve`, `rank` and `route` APIs use this **estimate**
as an admission, ranking or routing prior. They still require canonical source
admission, and thresholding does not remove protected hazards. Reliability
cannot upgrade an agent's authority or establish semantic truth. Use a separate
governed store for each tenant's trial outcomes.

`commontrace team mast` accepts an existing frozen incident ID and a narrative.
It attaches rule-based tags from MAST's 14 modes in three categories
(arXiv:2503.13657). An optional Python tagger must return taxonomy codes; unknown
codes fail validation. Only the narrative digest is journaled. These tags are
review aids, not a causal diagnosis. `incident.mast_tags` and
`attribution.writer_contributions` provide the corresponding Python surfaces.
