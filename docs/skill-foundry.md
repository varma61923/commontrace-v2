# UniMem Causal Skill Foundry

Use the foundry to test a reusable procedure before placing it in an agent's
skill catalog. It induces candidates from live, authenticated, successful
trajectories. A deterministic template works without a model; the Python API
accepts an `Inducer` callable returning a bounded prose or program body. Programs
require an operator-configured sandbox for screening. UniMem does not execute
arbitrary generated code in its process.

## Evidence and limits

Induction and verifier screens are **heuristics**. A screen checks tasks linked
to measured failures, with instructions, a verifier, and distinct passing and
failing fixture cases. The core lexical verifier tests required procedure steps;
it does not prove task semantics. Sandbox runners provide task execution for
programmatic candidates and must enforce process isolation themselves.

A skill becomes eligible for live trials only after screening and configuration
of a frozen conformal harm gate on exchangeable, ungated pilot traffic. The
catalog checks that exact calibration on every exposure. Skill trials use a
`skill:<content-hash>` unit through canonical holdout assignment and a published,
hash-chained allocation schedule. Schedules take effect after five seconds;
calls before that fail. Retries reuse the canonical arm rather than adding
observations. Exact propensities and complete outcomes remain mandatory.

A bundle trial fixes at least four candidates, randomizes a shared size factor
(up to three versus the full list), then randomizes skill exposure within its
eligible size. The first three candidates are fixed by content order before
assignment. Excluded candidates are logged as ineligible, without a fictitious
memory-level holdout. The estimated size effect describes this nested catalog
policy, including its inner randomization; it is not a claim that every skill in
the larger catalog was consumed. Bundle members must share their scopes.
Size strata also appear in canonical assignment contexts. Segment labels reserve
space for that factor and are limited to 51 characters.

The canonical promotion certificate remains the release gate. AIPW and
predictable e-processes assume exact propensities, stationary task traffic,
consistent delivered catalogs, independent task occasions, bounded binary
outcomes and no interference. The conformal gate controls expected marginal
failure loss under exchangeability, not realized harm on every stream. Observed
IPW failure spend is an **estimate**; the calibration bound is separate. Rare
units report `UNDERPOWERED` and a normal-approximation design MDE instead of a
promotion verdict. Optional G29 pooling supports planning and does not replace
the local release certificate.

Promoted skills continue randomized monitoring. Publication fixes a monitoring
split and a conservative baseline effect bound. A subsequent harm or decay
e-process alarm removes the skill from the catalog; `retire` revokes its
certificate and removes managed export files. Skills cannot renew publication
after such an alarm. This is conditional directional evidence, not an arbitrary
distribution-shift detector.
Harm and decay alarms split the error budget with a Bonferroni correction.

## Worked workflow

Capture successful and failed trajectories with explicit outcomes. Their
receipts must authenticate user or operator authority; an agent's self-reported
success cannot grant instruction authority. Configure the experiment and collect
matched pilot traffic for the `unimem-skill` scorer before enabling the harm gate.

```sh
unimem skill induce ./induction.json --dest ./store
unimem skill screen ./screen.json --dest ./store
unimem skill trial ./trial.json --dest ./store
unimem skill status ./skill-id.json --dest ./store
unimem skill publish ./skill-id.json --dest ./store
```

`induction.json` contains `name`, `description`, `trace_ids` and optional scope
`context`. A screen request contains `identity`, `context` and `tasks`. Each task
has a failed `trace_id`, `instruction`, `verifier: {"contains": "required step"}`,
`passing` and `failing` response fixtures. A trial request includes `identity`,
`context`, fixed `alpha`, `rate`, `training_size` and `minimum_support`; optional
`bundle_ids` enrolls an entire fixed bundle. `skill-id.json` contains `identity`
and `context`. `status` also accepts `pooled: true` when verified G29 contributions
are configured. These commands accept inline JSON or a bounded request file.

The local MCP tool `skill_catalog(occasion, context, segment)` returns the exact
catalog to deliver. Honor it and record that occasion's outcome with the existing
outcome API. `skill_evidence(identity, context)` verifies current source,
certificate, calibration, expiry and transparency state before returning a card.
The Python API is in `unimem.skill_foundry` and `unimem.skill_io`.

Publication writes `store/skills/<name>/SKILL.md` with exactly the Agent Skills
`name` and `description` frontmatter, plus `EVIDENCE.json`. The signed card carries
the effect interval, segments, failure accounting, certificate, validity window
and Merkle proof. It binds the complete rendered body separately from the
canonical candidate-body commitment. Helpers appear as indented code and must
run in the configured sandbox. Verify signatures against a pinned store key;
a key included in an untrusted folder is not a trust anchor.
Provision Ed25519 signing with the security extra before collecting trial
evidence when outsiders need public verification. Default HMAC cards require a
shared verifier secret and explicitly say they are not publicly verifiable.

Managed skill discovery rechecks source admission and current evidence. Revoked
or erased cards cannot fall back to legacy loading, even if their evidence file
is removed. Cached skill objects recheck before loading their body. Erasure of a
source removes managed descendant blobs and exported skill files; signed
commitments remain. Previously distributed folders and source/backup files need
their own erasure adapters. Unrelated legacy skills retain their existing behavior.

References: Agent Workflow Memory, arXiv:2409.07429; Inducing Programmatic Skills
for Agentic Tasks, arXiv:2504.06821; SkillsBench, arXiv:2602.12670.
