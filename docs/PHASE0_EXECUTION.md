# Phase 0 execution record

Work is on `feat/commontrace-memory-evolution`. All nine requested repositories
were cloned; product changes are confined to CommonTrace. The earlier memory
foundation remains documented in [MEMORY_EVOLUTION.md](MEMORY_EVOLUTION.md).
This record covers the subsequent evaluation and validation work. It is not a
claim that the research program or A–G acceptance targets are complete.

## Completed implementation

- Cache entries bind provider/account/endpoint/configuration, generation settings
  and completion implementation. Unbound legacy entries miss.
- Benchmark reader and rubric-judge calls reserve their maximum configured charge
  before HTTP dispatch. Output caps, temperature and no retries are explicit;
  redirects cannot forward credentials. Unknown usage retains the reservation
  and prevents subsequent calls. Provider prices must be configured.
- Optional exact context-text counting accompanies the existing character-based
  budget estimate. Prompt framing/output usage and returned context are distinct.
- Source-bound local Mem0 raw ADD/native hybrid search and Graphiti raw episodic
  BM25/RRF profiles execute upstream code. They omit LLM extraction, entity-graph
  construction and managed/cloud behavior, and are not vendor-wide scores.
- The Phase 0 runner uses one frozen harness across original/current products,
  pinned official inputs, exact question identities, dense/reranker ablations,
  full-context/no-memory controls, per-question outputs, clustered intervals and
  recorded unavailable/invalid attempts.

## Verification already completed

| Check | Result | Scope |
|---|---|---|
| Core/e2e suite | 5,969 passed; 96 skipped | Core suite at `390ad32`; optional/native dependencies are explicitly skipped |
| Hub suite | 2,471 passed; 1 skipped | Private ephemeral local PostgreSQL/pgvector; no production database |
| Memory contracts | Passed | Shared protocol/admission/retrieval journeys |
| Independent synthetic reproduction | Matching functional metrics | Two processes; fixture scores are not real-data accuracy |
| Fresh isolated wheel onboarding | 5.923 seconds to first recall | Prebuilt wheel, warm pip cache, core dependencies only; not public publication |
| Native Graphiti namespace regression | 14 passed, independently reviewed | Simple, hyphenated and underscored case IDs; future history excluded |
| Strict-cap integration | 158 passed; 3 native opt-in skips, independently reviewed | Real cl100k smoke plus source/proof, reference and failure boundaries |
| Final benchmark checks | 194 passed; 3 native opt-in skips | At `1694a2d`: cache, spend, counter, sampler, provenance, comparisons, reproduction and report rendering |
| Final independent Reviewer B | CONFORM | Raw hashes, all 416 caps, disjoint/balanced questions, rendered source bindings, coverage/interval claims and historical reproduction guidance |

The wheel's SHA-256 is
`5a20d733687c3c7a2ec530299d5f297b4e67536925de3dfe20ab3d74193c08f3`.
Optional benchmark libraries were installed in the execution environment;
CommonTrace's mandatory dependencies and protocol authority were unchanged.

## Frozen development experiment

The original product is `f44bde0`; the candidate product is `390ad32` with the
same benchmark harness overlaid on the original checkout. Seed: `20261008`.
There are 154 LoCoMo questions across ten conversations and 30 stratified
LongMemEval-S questions, with complete source histories. Budgets are **estimated**
1,000/2,000 units; cl100k text counts are separately reported. Three reader modes
and seven retrieval conditions per dataset produce fourteen sequential runs.
All fourteen runs completed and passed the mechanical source/configuration
checks. Twelve are credible retrieval conditions; the two original Graphiti
conditions are preserved configuration-defect artifacts, excluded below.

Official input SHA-256 values:

- LoCoMo: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- LongMemEval-S: `08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894`

MiniLM and the cross-encoder execute pinned local weights. Reports bind recursive
model artifacts, upstream source, canonical source data, normalization, harness,
configuration and question/gold identity. Runs exclude future history before
vendor indexing. No gold answers or gold evidence are supplied to retrieval.

Timing follows each native lifecycle. CommonTrace's dense index is built lazily
during first recall; vendor embeddings/indexing are charged to ingestion. Read
both ingestion and recall timings, plus whole-condition elapsed time. These
shared-host measurements do not establish production latency or an ANN limit.

Reader/judge inference is disabled: no endpoint/model budget has been configured.
Answer accuracy, task lift and business causal benefit therefore remain unknown.
Evidence-ID coverage, ranking, lexical completeness and answer-string presence
are separate diagnostic metrics; none substitutes for a judged reader answer.
Source-turn coverage can credit a selected excerpt even when its answer-bearing
span is absent; it is not proof of answer completeness.
These subsets/budgets are not comparable to historical full-split scores.

## Measured defects and corrections

The LoCoMo lexical baseline exceeded the nominal 1,000 cap in 79/154 contexts
when counted with cl100k. The published product contract is an estimate, so the
original reports are retained. An opt-in strict benchmark controller lowers the
native assembly budget and repeats the complete public path until context text
fits or its bounded attempts fail. It retains the final native source selection;
it never truncates returned context. Full-context remains uncapped. Directive
preservation follows the native assembler's contract. The selected vocabulary,
regex, enforcement code and settings are bound to the evaluation provenance.

The initial Graphiti profile returned no contexts for hyphenated LoCoMo case IDs.
A native one-turn reproduction retrieved under `demo` but not `conv-26`. Binding
canonical case identities to punctuation-free SHA-256 group namespaces fixes
native ingestion/search matching; source references and cutoff stay intact.
Those original empty-context reports are configuration-defect artifacts, not
credible vendor performance. The corrected profile must be measured separately.

Original/current canonical conversation evidence coverage is identical on both
development subsets at both budgets, including the dense/reranker condition.
The foundation's SDK/control features do not establish a gain in this canonical
conversation workload. Most dense/reranker evidence differences have paired
intervals including zero. LoCoMo dense-to-dense-plus-reranker at the estimated
1,000 budget has an exploratory +3.95 percentage-point difference, with a
marginal 95% interval of +0.87 to +7.01 points. That unadjusted diagnostic does
not establish reader-accuracy or a general ranking gain; defaults are unchanged.

Strict confirmation uses 40 LoCoMo and 12 LongMemEval-S questions excluded from
this change's development selection **before** sampling. It keeps original
question IDs and full histories, binds the exclusion list and refuses overlaps.
These public questions are not claimed to be unseen during model training.
LongMemEval selection is seeded and balanced across six primary question types,
with two questions per type. An earlier interrupted selection exposed an
unbalanced sampling path; that path was corrected and independently reviewed.
Another partial run stopped during an execution-environment restart. Both
partial directories retain explicit interruption records and are excluded from
the final aggregate report.

## Completed strict confirmation

Candidate revision: `1694a2d`; original product: `f44bde0`, with the same frozen
harness overlaid. Seed: `20261009`. All eight conditions and all twelve
budget-specific paired comparisons completed and passed source/configuration
validation. All **416 memory contexts** satisfy their selected cl100k text cap,
with zero exceedances at 1,000 or 2,000. Full-context/no-memory controls remain in
the raw reports; full-context is intentionally uncapped. No reader/judge calls ran.

| Dataset / local retrieval profile | Source coverage at 1,000 | At 2,000 |
|---|---:|---:|
| LoCoMo / CommonTrace lexical | 68.33% | 77.08% |
| LoCoMo / Mem0 raw ADD | 55.42% | 69.58% |
| LoCoMo / Graphiti raw episodic | 28.75% | 35.42% |
| LongMemEval-S / CommonTrace lexical | 70.14% | 80.56% |
| LongMemEval-S / Mem0 raw ADD | 86.81% | 84.72% |
| LongMemEval-S / Graphiti raw episodic | 45.14% | 58.33% |

Original/current lexical coverage remains identical at both budgets. These are
mean source-turn coverage diagnostics, not reader accuracy or vendor-wide scores.
LoCoMo paired local-profile coverage differences favor lexical CommonTrace with
marginal intervals excluding zero; LongMemEval differences include zero. The
intervals are conversation-cluster percentile bootstraps: ten LoCoMo clusters
and twelve LongMemEval clusters. They do not correct for multiple comparisons,
establish a general ranking or establish the 90% recall target. A larger budget
can change the native selected sources; coverage need not increase monotonically.

The reviewed local report contains twelve credible development conditions and
eight strict confirmation conditions in separate tables. Original raw artifacts
are preserved, including the two defective Graphiti development conditions.

## Reproduction

The commands below describe the current workflow, not an exact replay of the
preserved historical development artifacts. For that replay, create a detached
checkout/worktree at `390ad32` and run `benchmarks.phase0` there with seed
`20261008`; its Graphiti namespace defect is intentionally part of those frozen
outputs. Run confirmation from `1694a2d` with seed `20261009`, consuming that
development manifest, then render with the reviewed current report module.
Do not replace historical output directories. Running development from current
HEAD uses the corrected harness and creates a new experiment.

Use fresh output directories and the pinned official inputs above. Native local
profiles and exact counting require the optional benchmark environment; core
package dependencies are unchanged. These commands make no reader/judge calls.

```bash
python -m benchmarks.phase0 --data-dir "$DATA_DIR" --output "$DEV_OUT"
python -m benchmarks.confirmation --data-dir "$DATA_DIR" \
  --development-manifest "$DEV_OUT/manifest.json" --output "$CONF_OUT"
python -m benchmarks.phase0_report --development "$DEV_OUT" \
  --confirmation "$CONF_OUT" --output "$SITE_OUT"
```

The local HTML and reviewed JSON bind the consumed manifest, scorecard and raw
condition hashes. They show development and confirmation separately because
question selections and budget contracts differ. They retain unknown reader
accuracy and omit defective Graphiti development conditions and comparisons.

## Remaining gates

The fourteen-condition development sweep, eight-condition excluded-question
confirmation and source-checked local report are complete.
No reader-accuracy, 90%-recall-at-1,000-text-tokens, agentic top-three, million-item
ANN, adversarial security or public distribution target has been established.

Model reader/judge configuration and a maximum inference budget are required for
scored Phase 0B outcomes. Product ranking changes remain gated on that failure
analysis; the strict counter correction is evaluation fidelity work. Marginal
exploration propensities do not establish support for arbitrary joint-slate,
ranker or prompt off-policy comparisons. Fixed/anytime coverage, censoring,
procedure-versus-raw causal benefit and governance/federation remain separate
workloads to validate. Existing proof and billing approval authority is retained.

The existing independent two-arm planning approximation requires 1,570 occasions
per arm (3,140 balanced total) for a five-point lift from a 50% baseline at 80%
power. A normal approximation at 2,000 balanced independent occasions gives
about 61% power. These calculations do not establish clustered or anytime
coverage and cannot support a universal 2,000-occasion guarantee.

No repository push, public benchmark claim, package publication or deployment
has been performed in this execution.
