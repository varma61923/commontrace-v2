---
name: 2026-07-01_example-api-pagination
description: Added cursor-based pagination to a REST list endpoint, via the A+B pattern
agent_type: code
task_invocation: /commontrace add cursor-based pagination to GET /items (limit + opaque cursor, stable order), criteria — existing behavior preserved, regression test green, OpenAPI doc up to date
tags: [api, rest, pagination, refactor, example]
project: demo-api
verdict: CONFORM
importance: 3
importance_rationale: "Demonstration run of the A+B pattern on a refactor task with partially constant behavior; serves as anchor for the two example lessons."
n_iterations: 1
commit_sha: 0000000
duration_minutes: 18
lessons_retrieved_by_alpha: [lesson_example_regression_test_before_refactor]
lessons_hit: [lesson_example_regression_test_before_refactor]
lessons_proposed_by_omega: [lesson_example_serialize_subagents_same_file]
lessons_validated_by_lambda: [lesson_example_serialize_subagents_same_file]
---

> **Illustrative example (fictitious data).** This episode shows the FORMAT;
> it is not a real run. Delete it once your own episodes have accumulated.

## What happened
A first wrote a characterization test freezing the current response of `GET /items` (order + payload), ran it on the unmodified code, then introduced cursor-based pagination (`limit` and opaque `cursor` parameters, stable order on `(created_at, id)`). Immediate commit. B, as an independent reviewer, replayed the characterization test and verified that the first page without a cursor exactly reproduced the old behavior, then checked cursor stability under concurrent insertion. Verdict CONFORM in 1 iteration.

## What surprised me
Alpha had surfaced `lesson_example_regression_test_before_refactor` with high confidence as early as Phase 0; the A brief therefore integrated it from the start, which avoided the usual back-and-forth "B requests a missing regression test".

## What worked well
- Characterization test written BEFORE the refactor -> clean oracle for B (lesson `regression_test_before_refactor` effectively hit).
- Opaque cursor (base64 of `(created_at, id)`) rather than offset -> no skip/duplication under insertion.

## What worked less well
- Omega noted that A and B nearly edited `openapi.yaml` in parallel (doc + response example) -> proposal of the lesson `lesson_example_serialize_subagents_same_file`, validated by Lambda.
