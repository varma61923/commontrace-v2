---
name: lesson_example_serialize_subagents_same_file
description: When two sub-agents need to edit the same file, serialize them (never in parallel) to avoid silent overwrite
tags: [subagents, orchestration, concurrency, example]
agent_type: code
domain: subagents
importance: 5
importance_rationale: "Two parallel sub-agents on the same file silently overwrite each other; the work loss is silent and costly."
importance_history: []
applies_when: the orchestrator is about to launch >= 2 sub-agents whose file scopes overlap
do_not_apply_when: the sub-agents work on strictly disjoint files
uses: 0
last_hit: NEVER
source_traces: [2026-07-01_example-api-pagination]
source_episodes: [2026-07-01_example-api-pagination]
hub_trace_id: null
status: active
---

> **Illustrative example (fictitious data).** This file shows the FORMAT of a
> capitalized lesson; it is not a lesson from an actual run. Delete it once your
> own lessons have accumulated.

## Rule
Two sub-agents that touch the same file must execute in series (A finishes and commits, then B), never concurrently. If a parallel fan-out is needed, partition the work by file first.

## Why
Two concurrent writes to the same file via separate workspaces end in a "last write wins": the second commit overwrites the first with no conflict and no error message. The A+B pattern of `/commontrace` already serializes A->B for this reason; the rule generalizes to any fan-out.

## How to apply
In the orchestrator: before a `parallel(...)`, compute the intersection of the announced file scopes. If non-empty -> switch to sequential execution (or isolation via worktree + explicit merge). Only then parallelize.

## Counter-examples
Does not apply if each sub-agent has a disjoint file scope (e.g. one agent per independent module), in which case parallelism is safe and desirable.
