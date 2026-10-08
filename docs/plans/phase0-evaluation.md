# Phase 0: evaluation correctness and a bounded truth baseline

Execute on `feat/commontrace-memory-evolution`, preserving the fixed assignment
contract and the existing proof/billing authority. Foundation commit: `b099251`.

## Problem and sequence

The current synthetic release fixture is a correctness demonstration. It cannot
establish matched answer accuracy, real agentic performance or vendor rankings.
The conversation benchmark already has readers, judges, source-bound evidence
metrics and reference modes. Extend that path before introducing another engine.

1. Bind completion caches to provider/account configuration, request implementation
   and generation settings; refuse reuse of old unbound rows.
2. Reserve worst-case inference cost before every dispatch, cap output/retries,
   validate usage, and report historical cached cost separately from run spend.
3. Pin tokenizers and reader/judge configurations. Record actual model use;
   fallback does not count as a dense or reranked experiment.
4. Download/pin official LoCoMo and LongMemEval-S inputs outside canonical memory.
   Run bounded development comparisons with per-case outputs, reference controls,
   paired/cluster uncertainty and a ranked failure list.
5. Run executable vendor adapters under the same evidence/reader contract. Mark
   unavailable models, infrastructure and vendors explicitly as not reproduced.
6. Only after a truth baseline, implement the highest measured retrieval defect
   and confirm it against an untouched evaluation set.

## Invariants and boundaries

- No change to v2 assignment hash, prior arms, canonical source identity or
  approval authority. Research estimates do not become billing proof.
- No mandatory heavy dependency. Models and tokenizers remain optional.
- Credentials and credential-bearing URLs never enter cache metadata or reports.
- Unknown prices/usage cannot silently become zero paid cost.
- A partial, malformed, mismatched or budget-aborted judged run cannot be called
  a completed leaderboard result. Gold evidence and question identities remain
  fixed across comparisons.
- Reports distinguish evidence coverage, judged correctness, estimates, provider
  token usage, cached costs, current spend and setup/offline cost.

## Validation and rollback

Regression tests must show endpoint/account/generation cache isolation, unchanged
configuration reuse, refusal before unaffordable dispatch, multi-call judge
accounting, missing/invalid usage rejection and source/manifest matching. Use
real-store benchmark tests and core/e2e checks. Record Hub checks separately.
Independent Reviewer B audits each completed slice.

New cache storage has a separate versioned table; old rows remain untouched.
Rollback selects the prior code and a fresh output/store directory rather than
rewriting canonical memory or experiment logs. No report is promoted to a public
claim without its explicit publishing review.

## Exit gate

A pinned bounded real-data report, with known effective model configuration and
all unavailable conditions recorded, drives the next feature task. Completion of
cache/budget tests alone does not close the model-backed comparison gate.
