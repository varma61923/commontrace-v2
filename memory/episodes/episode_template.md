---
name: YYYY-MM-DD_slug
description: one-line summary
agent_type: code         # this file is a "code-review" profile Trace (see protocol/PROTOCOL.md) — fields below are profile-specific extensions
task_invocation: verbatim invocation /commontrace ...
tags: [tag1, tag2]
project: project-name-from-cwd
verdict: CONFORM
importance: 3            # integer 1-5 REQUIRED, see rubric in protocol/PROTOCOL.md#10-importance-rubric
importance_rationale: "" # string 1-sentence concrete REQUIRED — why this score
n_iterations: 1
commit_sha: xxx
duration_minutes: 0
lessons_retrieved_by_alpha: []
lessons_hit: []
lessons_proposed_by_omega: []
lessons_validated_by_lambda: []  # renamed in v2.2 (automatic Lambda validation, no longer user validation)
---

## What happened
[5-10 factual lines]

## What surprised me
[Orchestrator retro extract — verbatim]

## What worked well
[List 0-N items]

## What worked less well
[List 0-N items]
