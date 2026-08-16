# Memory Base Benchmark /commontrace — Status as of 2026-05-27

> Status doc updated with each major evolution of the benchmark or its metrics.
> /commontrace version covered: **v2.3** (Alpha + A + B + Omega + Lambda + attention layer).

---

## 1. Overview

The benchmark measures the quality of the long-term memory system integrated into the `/commontrace` skill. It analyzes episodes (one per run, written by Omega in Phase 10) and lessons (one file per procedural rule, validated by Lambda in Phase 11) stored under `$COMMONTRACE_ROOT/memory/`, and computes metrics along 3 complementary axes:

- **lesson_quality** — quality of Omega generation (Lambda validation)
- **implicit_retrieval** — quality of Alpha retrieval (precision + richness)
- **transfer_gap** — cross-project transfer capability (generalization)

Position in the /commontrace ecosystem: **off-hot-path tool**, manually invocable at any time. It never blocks a run and consumes no LLM tokens (local parsing of YAML frontmatters). No dependency on the orchestrator, sub-agents, or the attention layer. Read-only on `memory/`.

What it concretely provides:
- Gives the operator a quantitative signal on the health of the memory base (beyond the qualitative feel from runs)
- Detects drift (declining Omega quality, underperforming Alpha retrieval, accumulating never-hit lessons)
- Provides input to future mechanisms (Dreamer v2.4, temporal decay, automatic archival)
- Documents evolution over time (reproducible snapshot)

Invocation:
```bash
python3 benchmark/measure_performance.py
```

See §6 for flags and recommended frequency.

---

## 2. What Exists

### 2.1 Script `measure_performance.py`

Single canonical source: `$COMMONTRACE_ROOT/benchmark/measure_performance.py` (~430 lines, of which ~100 are markdown/HTML rendering). Standalone, no package, no tests, no external config.

**Architecture** (logical modules in the same file):

1. **Parse frontmatter** (`parse_frontmatter`, `parse_yaml_minimal`): extracts the YAML block between the `---` at the beginning of each `.md` file. Uses PyYAML if available, otherwise falls back to a minimal regex parser (sufficient for the current episode/lesson format).

2. **Load episodes/lessons** (`load_episodes`, `load_lessons`): `glob` on `memory/episodes/2*.md` and `memory/lessons/lesson_*.md`. Ignores templates (`*_template`). A lesson's slug is the `basename` without `.md`. No cache, full re-read on each invocation.

3. **Compute metrics** (`compute_lesson_quality`, `compute_implicit_retrieval`, `compute_transfer_gap`, `compute_extras`): one computation per axis + an aggregate computation for appendices (top hits, never-hit, unvalidated proposals, importance distribution, domain coverage).

4. **Render** (`render_markdown`, `render_html`): text output (stdout) or HTML (file under `memory/benchmark_reports/YYYY-MM-DD_HHMMSS.html`). JSON is rendered inline (`json.dumps`) without a dedicated helper.

**CLI flags**:

| Flag | Effect |
|---|---|
| (default) | Markdown stdout, all episodes |
| `--n=N` | Keep only the N most recent episodes (sorted by name = by date) |
| `--html` | Write an HTML report to `memory/benchmark_reports/` |
| `--json` | Raw JSON stdout (for downstream pipeline) |

**Output formats**:
- **Markdown**: fixed sections (Main metrics 3 axes, Per-episode detail table, Top 5 hits, Never-hit, Proposed-not-validated, Importance distribution, Domain coverage). Directly quotable in a discussion or commit message.
- **HTML**: wraps the markdown in a simple HTML template with inline styling. No charts, no interactivity — just browser-readable.
- **JSON**: complete structure (timestamp, n_episodes, n_lessons, 3 axes, episodes verbatim, extras) — payload ready for run-to-run comparison or ingestion by a future Dreamer.

No automatic hook: the benchmark is neither launched in Phase 11 nor as a git hook. Manual invocation by the user or an agent that decides to.

### 2.2 Metric 1 — lesson_quality

**Definition**: mean across episodes of the fraction of Omega proposals validated by Lambda.

**Formula**:
```
lesson_quality = mean(  |validated_by_lambda| / |proposed_by_omega|  )
                 over episodes with non-empty proposed_by_omega
```

The source field is `lessons_proposed_by_omega` (Omega proposals in Phase 10, before Lambda audit) and `lessons_validated_by_lambda` (ACCEPTED verdicts applied by the orchestrator in Phase 11). Backward-compatibility fallback: if `lessons_validated_by_lambda` is absent (v2.1 or earlier), reads `lessons_validated_by_user` — this is handled by the `get_validated()` function that manages the v2.2 pivot.

**Interpretation**:
- ~100%: Omega calibrates its proposals well, Lambda accepts them
- < 80%: degraded Omega quality signal (proposals outside criteria, duplicates, poorly calibrated importance) — also see the "proposed-not-validated" appendix metric
- > 100% (theoretical): can occur if an Omega proposal from a previous run is validated retroactively by a Lambda in a later run (backlog retro-validation) — empirically observed on the current snapshot

**Exclusions**: episodes with no Omega proposals (`lessons_proposed_by_omega == []`) are excluded from the computation — counting 0/0 would be meaningless. The effective count is reported (`over N valid episodes`).

**What it measures**: Omega-Lambda alignment. More precisely, the rate of proposals that pass the 4 Lambda criteria (formal quality + non-duplicate + generalization + importance calibration) without modification.

**What it does not measure**: the absolute quality of a proposal (Lambda may validate a lesson that never gets used). Important distinction: `lesson_quality` measures conformance to criteria, not actual usefulness. For usefulness, see the "never-hit" appendix metric (§2.5).

### 2.3 Metric 2 — implicit_retrieval (strict + permissive)

Alpha retrieval is evaluated through two complementary angles. Both are systematically computed and reported.

**Definitions**:

```
strict     = mean(  |hit ∩ retrieved| / |retrieved|  )
permissive = mean(  |hit|             / |retrieved|  )
             over episodes with non-empty retrieved
```

The source fields are `lessons_retrieved_by_alpha` (formal Alpha selections in its report, "Applicable lessons" block) and `lessons_hit` (lessons actually useful according to A/B reports + orchestrator retro).

**Why two angles**: RETEX from run 3 (`2026-05-27_formalize-lambda`). The structural debate: should `hit ⊂ retrieved` (strict, hit cannot exceed what Alpha surfaced) or can `hit` include background lessons (counter-examples, implicit methodological rules never formally selected)? The frozen design decision: `hit` is unbounded, which makes `permissive` meaningful. The strict ratio measures Alpha's **precision** (selections used), the permissive ratio measures **richness** (can exceed 100% if Omega counts hits outside retrieved).

**Diagnostic interpretation**:
- strict ≈ permissive ≈ 100%: Alpha is precise AND complete (ideal case)
- strict high, permissive >> strict: Alpha is precise but under-retrieves (Omega supplements with unselected lessons)
- strict low, permissive low: Alpha retrieves but few hits (non-applicable lessons or severe orchestrator retro)
- strict low, permissive high: atypical pattern — investigate

**Exclusions**: episodes with `retrieved == []` excluded (typically the first run, empty memory base). The effective count is reported.

**What it measures**: functional quality of Alpha retrieval — does what Alpha surfaces actually serve A/B? More reliable than Alpha's self-declared confidence (which is subjective).

**What it does not measure**: false negatives (relevant lessons that Alpha should have surfaced but didn't). Detecting this would require an annotated ground truth — not available today.

### 2.4 Metric 3 — transfer_gap

**Definition**: fraction of hits whose source lesson originates from a different project than the current project.

**Formula**:
```
transfer_gap = cross_hits / total_hits
where total_hits  = hits whose source_episodes are traceable (project identifiable)
      cross_hits  = hits where none of the source_episodes belong to the current project
```

Algorithm: for each episode, for each `hit_slug` in `lessons_hit`, resolve the lesson, read its `source_episodes`, collect distinct `project:` values, and increment `cross_hits` if the current project is not in the set. If `source_episodes == []` (seeded lesson without a source episode), the hit is marked `untraceable` and excluded from the computation.

**Interpretation**:
- 0%: all hits come from lessons seeded on the same project (no transfer)
- > 0%: empirical proof that a lesson from project X actually helps on project Y
- N/A: no traceable hits (single-project base or lessons without source_episodes)

**Current state**: single-project — all episodes have `project: project-x`, so the metric is mechanically 0% (no hit can be cross-project). The `transfer_gap` is not meaningful as long as the base remains single-project.

**What is needed for the metric to become measurable**:
- At least one distinct project in the memory base (e.g. `project: module-a` separate from `project: project-x`)
- Multiple runs on each project to have a statistically comparable corpus
- Lessons seeded in one project that get retrieved and hit on another

Currently the module-a episodes have `project: project-x` because module-a is a sub-project of project-x. The project distinction could perhaps use a finer breakdown (e.g. `project: module-a`, `project: module-b`, `project: module-c-v2`) to make the metric exploitable even within project-x.

### 2.5 Appendix Metrics

The benchmark additionally computes 5 appendix indicators useful for qualitative audit:

- **Top 5 lessons by uses**: ranking by `uses` counter (incremented in Phase 11 ACCEPTED). Reveals which lessons are most mobilized in practice. Also identifies lessons that could benefit from an importance promotion (empirical correlation uses-utility).

- **Never-hit lessons**: `uses == 0` AND not templates. Candidates for eventual archival (`status: active -> archived`). Identifies seeded lessons without traction that pollute Alpha retrieval without benefit.

- **Lessons proposed by Omega but never validated by Lambda**: `(proposed ∪ all_episodes) - (validated ∪ all_episodes)`. Degraded Omega quality signal: if a proposal has NEVER been accepted by Lambda in any run, it's probably a systematic false positive.

- **Importance distribution**: number of lessons and episodes per importance level 1-5. Detects calibration bias (cf. RETEX 7.5 doc: no cases at 1-2 in the seeds).

- **Domain coverage**: number of lessons per domain (`git-safety`, `cuda-gpu`, `refactor`, `testing`, `subagents`, `performance`, `other`). Reveals over-represented domains (`subagents` today) and under-represented ones (`git-safety`, `performance`).

These appendices are inexpensive to compute and provide qualitative context to the 3 main metrics. Particularly useful for answering "is my memory base healthy?" beyond the aggregate numbers.

---

## 3. What Works Well (empirical validation on the current base)

### Current Stats (snapshot 2026-05-27)

- **n_episodes** = 12 (v2 bootstrap run + 4 /commontrace meta-runs + 7 real module-a runs)
- **n_lessons** = 26 (5 cuda-gpu->1, refactor->6, subagents->10, testing->5, other->4 + seeded lessons)
- **lesson_quality** = **104.5%** over 11 valid episodes
- **implicit_retrieval**: strict **88.2%**, permissive **97.3%** over 11 valid episodes
- **transfer_gap** = 0.0% over 55 traceable hits (single-project)

Verbatim detail is in §7.

### Detected Patterns

**Emergence of a natural usage hierarchy**. The top 5 by uses shows a non-trivial distribution:
- `lesson_semantic_check_not_just_syntactic`: 7 uses (cross-cutting refactor)
- `lesson_no_tmp_results`: 5 uses (paths/results)
- `lesson_serialize_subagents_same_files`: 4 uses (subagents)
- `lesson_subagent_double_review_pattern`: 4 uses (subagents)
- `lesson_alpha_brief_quality_drives_a_quality`: 3 uses (subagents)

This hierarchy accurately reflects the empirically observed pattern: "subagents" and "refactor" lessons dominate because `/commontrace` is precisely a skill for refactors via subagents — this is expected and auditable.

**Omega/Lambda distinction structurally revealed**. The strict ratio (88.2%) vs permissive (97.3%) shows that Alpha is precise: ~88% of Alpha selections actually serve. The approximately 9-point gap between strict and permissive corresponds to background-hit lessons (counter-examples, methodological rules) that Omega counts but Alpha did not formally select. Expected and consistent pattern with the design — see §2.3.

**lesson_quality > 100% empirical**. The observed 104.5% is not a computation anomaly but a retro-validation effect. A Lambda from a recent run can validate a proposal that remained pending from a prior run (the v2.1 backlog that had accumulated). When dividing validated by proposed on the current episode, you can therefore get a ratio > 1 if validated includes slugs proposed elsewhere. Pattern to clarify semantically: "% of proposals validated by Lambda" is ambiguous if validation is asynchronous.

**module-a lessons empirically useful**. The lesson `lesson_audit_cascade_may_reveal_noop_scope` was created on 2026-05-27 from episode `module-a-v04-pipeline-multi-tf`, hit 3 times afterwards (`module-a-v04-g2-eventref-tf-qualifiable`, `module-a-v04-g3-nested-outer-level-persistent`, `module-a-task4-setup-12-runnable-g4-candidate`) — empirical proof that the mechanism captures transferable rules within the same class of runs (multi-dimensional module-a refactor).

### Operational Insights Enabled by the Benchmark

- **Identify never-hit lessons**: 4 lessons at `uses: 0` currently (`lesson_e2e_test_xfail_when_dependency_bugs_identified`, `lesson_gp_gpu_non_deterministic`, `lesson_signal_absence_confirmed_by_regularization_convergence`, `lesson_triangulate_before_architectural_report`). None is an immediate archival candidate (all < 1 month old), but worth monitoring. The presence of `lesson_gp_gpu_non_deterministic` (seeded 2026-05-26) without hits reflects the fact that no /commontrace run has touched GPU code since — expected.

- **Identify Omega proposals rejected by Lambda**: 2 proposals to date (`lesson_couple_skip_flags_for_coherent_state`, `lesson_horizon_dominates_context_at_long_h`). First Omega quality signal — proposed by an Omega but rejected by Lambda as non-generalizable or too specific. Worth auditing to understand the rejection criteria.

- **Non-trivial importance distribution**: 16 lessons at importance 3, 9 at importance 4, 1 at importance 5. No monoculture (not everything at 5), suggesting that calibration is defensible — but also confirms the bias identified in RETEX 7.5 (no 1-2 cases). The "everything drifts to 5" scenario is not observed on this snapshot.

- **Auditable domain coverage**: `subagents` dominates (10 lessons) because it's the core of the skill; `git-safety` and `performance` are at 0 lessons (empirical blind spot — no run has yet generated these patterns). `cuda-gpu` at 1 only (the seed `lesson_gp_gpu_non_deterministic`) reflects the fact that recent runs are on module-a (CPU/algo) not on module-b/module-c (GPU).

### Cross-file Consistency

The benchmark re-reads all sources (individual episodes + lessons) on each invocation without depending on INDEX.md. This is intentional: `lesson_<slug>.md` is the source of truth, INDEX.md is merely a reflection. If INDEX.md drifts (unsynchronized manual edit), the benchmark continues returning the true values, which also allows **detecting** the drift (manually comparing INDEX.md vs benchmark output).

---

## 4. Current Limitations

### 4.1 Lambda Report Not Persisted in the Episode

The current episode captures `lessons_proposed_by_omega` and `lessons_validated_by_lambda` but **not the complete Lambda verdict** (ACCEPTED / REJECTED / NEEDS REFINEMENT + 2-3 sentence justification). Consequence: the benchmark cannot compute **the effective Lambda rejection rate** per episode nor distinguish "rejected" from "needs refinement" (both appear as "not validated"). The degraded Omega quality signal is limited to "proposed but never validated anywhere", which is more permissive than "proposed and explicitly rejected by Lambda".

Impact: we lose a valuable diagnostic signal (why did Lambda reject? what families of proposals are systematically rejected?). Future solution: add a `lambda_decisions: {proposal_slug: verdict}` field to the episode frontmatter (cf. §5).

### 4.2 transfer_gap single-project so far

All episodes have `project: project-x` — including module-a episodes (module-a being a sub-project of project-x, the project field captures the git repo not the sub-domain). Consequence: transfer_gap = 0% mechanically, metric not meaningful. The mechanism's cross-project generalization capability (primary objective vs Thomas AI) is not empirically measured.

Impact: one of the 3 main benchmark axes is effectively neutralized. To unblock: tag distinctly by sub-project (or use the mechanism on a truly separate repo, e.g. ~/other-project).

### 4.3 N episodes still modest (12)

12 episodes, of which 4 are `/commontrace` meta-runs (skill modifying itself) and 7-8 module-a runs concentrated on a single day (2026-05-27). Consequence: high statistical variance — a single aberrant run shifts the averages by several points. The current numbers (88.2% strict, 104.5% lesson_quality) are indicative but not statistically robust.

Impact: need to reach N >= 30 episodes distributed over several weeks before the metrics provide a reliable signal. Worth monitoring: if variance remains high beyond N=30, it's probably a methodological signal (poorly calibrated rubric, poorly defined episode format) rather than a data shortage.

### 4.4 No Alert Thresholds

The benchmark returns raw numbers without automatic interpretation. No warning if `lesson_quality < 50%`, no flag if `never_hit` exceeds 20% of lessons, no notification if importance distribution becomes unimodal. The user must manually interpret at each reading.

Impact: risk of missing slow drift. Simple solution: add configurable thresholds (constants at the top of the file or CLI flag `--alert-thresholds`) that highlight out-of-range values (color flag in HTML, non-zero exit code in CLI).

### 4.5 No Temporal Trend

The benchmark is a **single snapshot**: it computes metrics at time T across the entire base, without historization. Consequence: impossible to answer "is lesson_quality improving or degrading over the last 10 runs?" without manually re-running with `--n=10` and mentally comparing.

Impact: cannot detect slow drift, nor measure the effect of a skill modification (e.g. adding a Lambda criterion, refining the Omega brief). The data exists (`timestamp` in JSON output) but is not stored run-to-run. Solution: persist each run under `memory/benchmark_reports/*.json` then add a comparison mode (cf. §5).

### 4.6 No Comparable Structured Export

`--html` exists but produces a simple HTML (markdown wrapped in `<pre>`, no charts, no dynamics). `--json` renders the complete structure but without a versioned schema — a future consumer (Dreamer, dashboard) would have to parse without a formal contract. No output format versioning.

Impact: difficulty building a downstream consumption tool without breaking on each benchmark evolution. Solution: version the JSON output (`schema_version: "1.0.0"`), document the contract in the code.

### 4.7 Dreamer Hooks Absent from the Benchmark

The attention layer exposes a stable `index.npz` contract for Dreamer v2.4 (cf. DOCUMENTATION.md §6.4), but the benchmark **does not use** this contract. It could compute an additional "semantic redundancy" metric via pairwise cosine between lessons (pairs > 0.85 = fusion candidates), which would be a direct input for Dreamer. Not implemented.

Impact: missed opportunity to provide Dreamer with a pre-computed qualified input (semantic fusion candidates). Solution: add a `compute_semantic_redundancy(lessons)` module that loads `index.npz` and computes the matrix, returns the top-K pairs above a threshold.

### 4.8 No Alpha Latency/Cost Measurement

The benchmark measures retrieval quality but not its cost. No measurement of Alpha latency (retrieval time), no measurement of the number of lessons read by Alpha, no measurement of the token cost of the enriched Alpha brief. Consequence: impossible to make an empirical decision on "when to switch to a different embeddings backend", "when to consolidate the base", or "when to activate Dreamer in the background".

Impact: the doc §4.6 decision (scaling via attention layer) remains theoretical. Without latency/cost measurement, we don't know at what volume (100 lessons? 500? 5000?) the mechanism starts becoming expensive. Solution: instrument Alpha to log latency + token usage + number of files read, add an "Operational Cost" section to the benchmark.

---

## 5. Planned Improvements (roadmap)

In decreasing priority order (the first ones unblock missing metrics, the last ones are progressive improvements).

### P1 — Persist Lambda Reports in the Episode

Add a frontmatter field `lambda_decisions: {proposal_slug: verdict}` or structured block `## LAMBDA OUTPUT` (verbatim) in the episode body. Proposed format:
```yaml
lambda_decisions:
  lesson_xxx: ACCEPTED
  lesson_yyy: REJECTED
  lesson_zzz: NEEDS_REFINEMENT
```

Unblocks: "Lambda acceptance rate" metric (% ACCEPTED / total proposed), "rejection reasons" metric (categorization of Lambda justifications via regex or tags). Allows identifying recurring rejection patterns (e.g. "Lambda rejects 60% of proposals due to importance calibration" -> direct signal on the rubric).

Effort: ~30 min (modify `SKILL.md` Omega brief + Lambda brief + episode format + benchmark parser).

### P2 — Activate transfer_gap via Multi-project Tagging

Tag distinctly as `project: module-a`, `project: module-b`, `project: module-c-v2` etc., rather than collapsing everything under `project: project-x`. Progressive adoption: new frontmatter for new runs, retro-tagging existing episodes (optional, manual edit).

Unblocks: transfer_gap becomes measurable within project-x. A hit on `lesson_audit_cascade_may_reveal_noop_scope` (seeded on module-a) on a module-b run would count as cross-project.

Effort: ~10 min (modify `SKILL.md` "project detection" paragraph + manual retro-tagging).

### P3 — Temporal Trend via Run Persistence

Store each benchmark invocation in `memory/benchmark_reports/YYYY-MM-DD_HHMMSS.json` (default mode, not just `--html`). Add a `--diff` mode that compares the 2 most recent runs and flags significant deltas. Add a `--history` mode that plots the evolution of the 3 main metrics.

Unblocks: slow drift detection, impact measurement of a skill modification, post-hoc audit "when did quality change".

Effort: ~1h (systematic JSON persistence + diff/history mode).

### P4 — Configurable Alert Thresholds

Add default thresholds (configurable via flag):
- `lesson_quality < 0.7` -> WARNING (Omega/Lambda misaligned)
- `implicit_retrieval_strict < 0.5` -> WARNING (Alpha imprecise)
- `never_hit_ratio > 0.3` -> WARNING (useless lessons)
- `distribution_importance` unimodal (95%+ in a single level) -> WARNING (broken rubric)

Output: "Alerts" section at the beginning of the markdown report if thresholds breached, non-zero exit code in CLI if `--strict`.

Effort: ~30 min.

### P5 — Alpha Latency and Operational Cost Measurement

Instrument Alpha to log: total retrieval time (ms), number of lessons read (parsed frontmatters), number of candidates surfaced by attention layer, token count of the enriched Alpha brief. Log to a `memory/alpha_telemetry.jsonl` file (one object per run). Add an "Operational Cost" section to the benchmark: latency p50/p95, tokens p50/p95.

Unblocks: empirical decision on "when to consolidate the base" and "attention layer ROI". Also allows detecting a pathologically slow Alpha (signal of index corruption or candidate explosion).

Effort: ~1h (Alpha instrumentation + benchmark parser + aggregation).

### P6 — Dreamer Input Integration

Dreamer v2.4 (not implemented) could use the benchmark as input: the "never_hit" list serves as archival candidates, the "lesson_quality" metric serves as a timing signal (Dreamer only runs if quality < threshold), the pairwise cosine between lessons serves as fusion candidates. The benchmark should expose this data stably (dedicated JSON field, versioned contract).

Effort: ~30 min on the benchmark side (stable extraction), to be coordinated with the Dreamer implementation.

### P7 — Enriched HTML Visualization

Replace the current HTML (wrapped markdown) with a template featuring:
- Sparkline chart of lesson_quality / strict / permissive evolution over the last N runs (if history available)
- Importance x domain heatmap (to visualize coverage)
- Color-highlighted alerts

Effort: ~2h (HTML template + inline chart, minimal Chart.js or D3).

### P8 — Semantic Near-duplicate Detection

Load `memory/attention/index.npz`, compute `emb @ emb.T`, list lesson pairs with cosine > 0.85 as fusion candidates. Output in a "Semantic near-duplicates" section of the benchmark, with manual recommendation (no automatic action).

Unblocks: direct input for Dreamer, base quality audit without a Dreamer run.

Effort: ~30 min (already documented in DOCUMENTATION.md §6.4 as a snippet).

---

## 6. How to Use the Benchmark

### Standard Invocation

```bash
# Markdown stdout, all episodes (default)
python3 benchmark/measure_performance.py

# Limit to the N most recent episodes
... measure_performance.py --n=10

# HTML to memory/benchmark_reports/
... measure_performance.py --html

# Raw JSON for downstream pipeline
... measure_performance.py --json > snapshot.json
```

### Recommended Frequency

- **After each batch of 5-10 /commontrace runs**: default invocation, quick read to verify that the 3 axes remain in healthy ranges
- **Before a Dreamer run** (when v2.4 is implemented): `--json` invocation to provide the input
- **Before a skill modification** (e.g. adding a Lambda criterion, refining Omega): baseline to measure impact afterwards
- **At the start of a session**: `--n=5` for a quick signal on recent runs before continuing

### Quick Interpretation

At each reading, check in order:
1. **n_episodes**: enough data? (< 10 = high variance expected)
2. **lesson_quality**: if < 80%, look at "proposed-not-validated" to understand which proposals are rejected
3. **implicit_retrieval strict vs permissive**: gap > 20 points = Alpha under-retrieves
4. **transfer_gap**: N/A or 0% = single-project, ignore
5. **Top 5 hits**: consistent with what you expect from the skill? If an "exotic" lesson is in the top, investigate
6. **Never-hit**: if > 30% of lessons, archival candidates or reflection on seed relevance
7. **Importance distribution**: if everything is concentrated on one level, poorly calibrated rubric

---

## 7. Appendix: Values as of 2026-05-27

Verbatim block of the benchmark output (default invocation, markdown stdout):

```
# /commontrace Memory Benchmark Report

**Date**: 2026-05-27T17:57:46
**Episodes analyzed**: 12
**Lessons in base**: 26
**YAML parser**: PyYAML

## Main Metrics (3 axes)

### lesson_quality
% of lessons proposed by Omega validated by Lambda.
-> **104.5%** over 11 valid episodes

### implicit_retrieval (2 angles)
Precision and richness of Alpha retrieval. `hit` is not bounded by `retrieved` —
see semantic doc (counter-examples / background rules can count as hits).
- **strict** = mean(|hit ∩ retrieved| / |retrieved|) -> **88.2%**
  (precision: proportion of Alpha selections that actually served)
- **permissive** = mean(|hit| / |retrieved|) -> **97.3%**
  (richness: can > 100% if Omega counts hits outside Alpha retrieved)
- over **11** valid episodes

### transfer_gap
% of cross-project hits (transfer outside source project).
-> **0.0%** over 55 traceable hits (+ 0 untraceable)

## Per-episode Detail

| Date / slug | Verdict | Imp | Retrieved | Hit | Proposed | Validated | Project |
|---|---|---|---|---|---|---|---|
| `2026-05-26_create-commontrace-v2` | CONFORME | 3 | 0 | 0 | 1 | 1 | project-x |
| `2026-05-27_add-attention-layer` | CONFORME | 4 | 5 | 7 | 2 | 2 | project-x |
| `2026-05-27_extend-commontrace-importance` | CONFORME | 3 | 4 | 4 | 2 | 2 | project-x |
| `2026-05-27_formalize-lambda` | CONFORME | 4 | 5 | 7 | 2 | 1 | project-x |
| `2026-05-27_module-a-p22-calibration-k-coefficients` | CONFORME | 3 | 4 | 4 | 3 | 3 | project-x |
| `2026-05-27_module-a-p23-context-modulators-fix-collision` | CONFORME | 3 | 5 | 5 | 1 | 2 | project-x |
| `2026-05-27_module-a-p24-sharpness-magnitude-discovery` | CONFORME | 4 | 5 | 5 | 3 | 3 | project-x |
| `2026-05-27_module-a-p25-dimensionless-weights-magnitude-regularization` | CONFORME | 4 | 12 | 6 | 2 | 2 | project-x |
| `2026-05-27_module-a-task4-setup-12-runnable-g4-candidate` | CONFORME | 4 | 5 | 5 | 1 | 1 | project-x |
| `2026-05-27_module-a-v04-g2-eventref-tf-qualifiable` | CONFORME | 3 | 5 | 5 | 0 | 0 | project-x |
| `2026-05-27_module-a-v04-g3-nested-outer-level-persistent` | CONFORME | 4 | 5 | 3 | 1 | 1 | project-x |
| `2026-05-27_module-a-v04-pipeline-multi-tf` | CONFORME | 3 | 5 | 4 | 1 | 1 | project-x |

## Top 5 lessons by uses

- `lesson_semantic_check_not_just_syntactic`: 7 uses
- `lesson_no_tmp_results`: 5 uses
- `lesson_serialize_subagents_same_files`: 4 uses
- `lesson_subagent_double_review_pattern`: 4 uses
- `lesson_alpha_brief_quality_drives_a_quality`: 3 uses

## Never-hit lessons (eventual archival candidates)

- `lesson_e2e_test_xfail_when_dependency_bugs_identified`
- `lesson_gp_gpu_non_deterministic`
- `lesson_signal_absence_confirmed_by_regularization_convergence`
- `lesson_triangulate_before_architectural_report`

## Lessons proposed but never validated (degraded Omega quality signal)

- `lesson_couple_skip_flags_for_coherent_state`
- `lesson_horizon_dominates_context_at_long_h`

## Importance distribution

**Lessons**:
- importance 3: 16 lessons
- importance 4: 9 lessons
- importance 5: 1 lessons

**Episodes**:
- importance 3: 6 episodes
- importance 4: 6 episodes

## Domain coverage

- cuda-gpu: 1 lessons
- other: 4 lessons
- refactor: 6 lessons
- subagents: 10 lessons
- testing: 5 lessons
```
