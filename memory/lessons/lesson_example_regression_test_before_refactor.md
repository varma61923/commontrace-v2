---
name: lesson_example_regression_test_before_refactor
description: Write a regression test capturing current behavior BEFORE starting the refactor, not after
tags: [testing, refactor, regression, example]
domain: testing
importance: 4
importance_rationale: "Without a pre-capture test, B cannot distinguish an intentional behavior change from a silent regression."
importance_history: []
applies_when: the A brief requests a refactor / rewrite of a module whose observable behavior must remain identical
do_not_apply_when: creating a brand-new feature with no prior behavior to preserve
uses: 1
last_hit: 2026-07-01
source_episodes: [2026-07-01_example-api-pagination]
status: active
---

> **Illustrative example (fictitious data).** This file shows the FORMAT of a
> capitalized lesson; it is not a lesson from an actual run. Delete it once your
> own lessons have accumulated.

## Rule
Before any behavior-preserving refactor, have A write a test that captures the current behavior (golden/characterization test) and make it pass on the code BEFORE modification.

## Why
On the `2026-07-01_example-api-pagination` run, the pagination refactor could have changed the result order without anything flagging it. The characterization test written first served as an oracle: B was able to verify that the observable behavior was preserved rather than reviewing line by line.

## How to apply
In the A brief: "Step 1 — write a test that freezes the current behavior of `<module>` and verify it PASSES on the unmodified code. Step 2 only — refactor. The test must remain green." In the B brief: require proof that the test existed and passed before the diff.

## Counter-examples
Does not apply when there is no prior behavior (new feature), nor when the refactor INTENTIONALLY changes behavior — in that case, the test must be explicitly updated and the test diff is part of the review.
