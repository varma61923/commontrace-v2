---
name: lesson-slug
description: one-line summary (used by Alpha / the Retriever for relevance check)
tags: [tag1, tag2]
agent_type: code         # REQUIRED, open vocabulary: code | support | sales | hr | marketing | ops | custom
domain: git-safety       # open vocabulary, see protocol/PROTOCOL.md#7-taxonomy-open-not-closed for recommended starter lists per agent_type
importance: 3            # integer 1-5 REQUIRED, see rubric in protocol/PROTOCOL.md#10-importance-rubric
importance_rationale: "Why this importance score is justified (1 concrete sentence, not generic)" # string 1-sentence concrete REQUIRED — why this score (not generic)
importance_history: []   # log of changes: [{date: YYYY-MM-DD, old: N, new: M, reason: "..."}]
applies_when: precise semantic activation condition (>= 1 concrete sentence, not generic)
do_not_apply_when: explicit counter-condition (prevents over-generalization)
uses: 0
last_hit: NEVER
source_traces: []        # IDs/slugs of the Traces (see protocol/schemas/trace.schema.json) that produced this lesson
source_episodes: []      # deprecated alias, kept for the code-review profile's episode files
hub_trace_id: null        # set by `commontrace sync` once this lesson is promoted to a Hub trace
status: active
---

## Rule
[1 actionable sentence]

## Why
[Factual observation or source incident, grounded in project reality]

## How to apply
[When to invoke it, how to use it concretely in the A or B brief]

## Counter-examples
[Cases where the rule does NOT apply]
