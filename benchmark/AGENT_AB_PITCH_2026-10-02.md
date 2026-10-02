# With and without CommonTrace: a controlled, end-to-end measurement

Date: 2026-10-02. Real agent runs, hidden-test grading, exact billed token usage from the run transcripts.
Plans were registered and hashed before any evaluation run (hashes at the end). Raw data: `agent_ab_data/`.

## The one-paragraph version
Where an agent's work depends on **rules that are written down in the code**, memory changes nothing and costs
nothing (Bench A: 48/48 pass in both arms, 252,934 vs 252,833 billed tokens). Where it depends on **rules that live
only in a team's past incidents and reviews** (rounding policy, deadline convention, PII logging, vendor limits,
key formats), agents with the relevant lesson passed CI on the first try **94% of the time versus 0%**, needed 1.1
attempts instead of 2.3, and spent **59% fewer billed tokens and 72% less agent time per green build**. The
production value is that effect multiplied by how often your fleet meets such rules, which this experiment cannot
measure for you.

## Setup (both benches)
- Agent: one fixed model and tool set, real runs. Task: implement one function in a small payments library; a
  strict hidden CI suite grades it. Visible tests are thin and pass for a naive solution; every task was validated
  (reference solution passes, naive fails hidden) before any agent ran.
- With/without: CommonTrace's own randomized holdout decides, per lesson per occasion, whether the relevant lesson
  is injected. Contrast: relevant lesson **injected (with)** vs **withheld (without)**. The agent prompt is identical;
  only the work directory's `TASK.md` differs. The store also held unrelated "distractor" lessons.
- Tokens are the **billed usage per API call from each run's transcript** (fresh input, cache writes, cache reads,
  output), not the harness's single summary number, which turned out to be cumulative and includes idle time.

## Bench B: rules that exist only as team memory (n = 40; 17 with, 23 without)
Each task needs a rule that appears in neither the spec nor the code. Lessons were written from the failing CI output
of baseline runs (what a reviewer would say). After a CI failure the agent is sent the failing output and may retry,
up to 3 attempts, identically in both arms. The hidden tests are never shown.

| | With CommonTrace | Without | Difference (95% CI) |
|---|---|---|---|
| First-attempt CI pass | **16/17 (94%)** | 0/23 (0%) | +94 pts (+69 to +99), p = 2.7e-10 |
| Green within 3 attempts | 17/17 (100%) | 18/23 (78%) | +22 pts (0 to +42), p = 0.061 |
| Attempts used, mean | 1.1 | 2.3 | median -1 (-2 to -1) |
| Billed tokens to green or give-up, median | 203,113 | 411,483 | -208,370 (-217,984 to -154,006) |
| Billed tokens per green build | 212,644 | 517,129 | **-59%** |
| Cost units per green build (assumed price ratios) | 53,986 | 81,876 | **-34%** |
| Agent wall time per green build | 7.6 s | 27.0 s | **-72%** |
| Tool calls, median | 4 | 8 | -4 (-5 to -3) |
| Raw email written to a log (PII), first attempt | **0/3** | **5/5** | |
| Never reached green in 3 attempts | 0/17 | 5/23 | all 5 in one rule class |

Per rule class, first-attempt pass with vs without: rounding 3/3 vs 0/5, deadline 3/3 vs 0/5, PII masking 3/3 vs 0/5,
vendor batch limit 4/4 vs 0/4, idempotency key 3/4 vs 0/4.

Notes on reading this:
- **Cost units** weight each token category relative to fresh input (cache write 1.25, cache read 0.1, output 5).
  Those ratios are an assumption; plug in your own rates. Raw token counts are dominated by cached context, so the
  percentage saving in cost is smaller than the saving in raw tokens, and the median-cost interval includes zero.
  The per-green-build figure is larger because 5 of 23 unaided runs never reached green at all.
- **PII:** with retries CI eventually caught the leak in the unaided arm too. With memory the defect was never
  written. If no CI test exists for a rule, the unaided leak ships.
- **Wall time** is the agent's compute only. It omits the real cost of a failed CI cycle (queue time, a human
  re-reading the failure), which is typically minutes to hours, not seconds.
- The one first-attempt failure with memory (a key-format task) was the agent adding its own de-duplication, a
  misreading of the spec that is unrelated to the lesson. It reached green on retry. The same misreading cost
  unaided agents extra attempts.
- The five unaided runs that never reached green were all the deadline rule (17:00 New York, daylight saving). The
  failing assertion gives no way to infer it, and several agents correctly declined to guess.

## Bench A: rules documented in the code (n = 48; 31 with, 17 without)
| | With | Without |
|---|---|---|
| Hidden CI pass | 31/31 | 17/17 |
| Billed tokens per run, median | 252,934 | 252,833 |
| Cost units, median / mean | 43,335 / 49,984 | 42,778 / 47,590 |
| Wall time, median | 8.4 s | 8.6 s |
| Tool calls, median | 5 | 5 |

A strong model already reads the docstrings, so there is nothing for memory to add, and injecting lessons costs
about 1% (median) in cost units. CommonTrace's own experiment report called this run uninformative (all outcomes
succeeded) rather than reporting an effect.

## System measurements (idle 4-core machine, this session)
- Retrieval at the agent: median 0.36 s, p95 0.50 s per query (first call after start: 16.5 s while models load).
  Cold CLI query at 1,000 lessons: p50 352 ms, p95 395 ms.
- Time from empty store to first injected lesson: 0.66 s. Install into 6 agent platforms: 6/6.
- Hub request floor (auth plus rate limit): 516 req/s in memory and 241 req/s on Postgres at 8 concurrent clients;
  p95 20 ms and 44 ms. This is not search latency.
- Injected context: median about 405 tokens (1,620 characters), at most about 1,000.
- **Injection precision: 16% in Bench B (17 of 106 injected lessons relevant), 24% in Bench A.** Most of what an
  agent receives is irrelevant, because the experiment path admits top-ranked lessons regardless of absolute
  relevance. It did not hurt here (the cost is a few hundred tokens), but it is the first thing to fix.
- Security: static scan (bandit) of all 88 produced solutions found 0 issues; the repository's own Hub suite
  (2,439 tests) and root suite (3,349 tests) pass; SQL-injection and path-traversal hidden checks passed in both arms.

## What this supports claiming, and what it does not
Supports: when an agent meets an undocumented organisational rule, a retrieved lesson turns a failed first attempt
into a pass, and does so with a measurable cut in tokens, attempts and defects shipped; and it does so at about
400 tokens and under half a second of overhead, with no measured penalty when the knowledge is not needed.

Does not support:
- **How often that happens.** Every Bench B task was built to need such a rule. Value = (share of your fleet's work
  that hits one) x (the per-task saving above) - overhead. That share is a customer property; the first pilot
  should measure it, using this protocol on the customer's own tasks.
- Production numbers. The tasks, rules and lessons were written by the experimenter; one model; one codebase.
  Lessons came from CI failures through a manual step, not an automatic extraction agent.
- Rate cards. Dollar figures need your rates; the cost-unit weights are an assumption.

## Illustration of the arithmetic (per 1,000 tasks; not a forecast)
Per affected task, from the table: 304,000 fewer billed tokens, 27,900 fewer cost units and about 1.2 fewer CI
attempts per green build. If a fraction p of a fleet's tasks hit an undocumented rule:

| p | Fewer CI attempts | Fewer cost units | Fewer billed tokens |
|---|---|---|---|
| 2% | 24 | 0.56 M | 6.1 M |
| 5% | 60 | 1.4 M | 15.2 M |
| 10% | 120 | 2.8 M | 30.4 M |
| 20% | 240 | 5.6 M | 60.9 M |

Add the human time of each avoided CI failure at your own loaded cost.

## Deviations from the registrations (all made before the relevant outcomes were seen)
Bench A: replications 2 to 4 per task; contrast changed to relevant-lesson injected vs withheld (the holdout is per
lesson); a planted prompt-injection arm was dropped (the environment blocked writing it); 16 runs failed on an API
rate limit and were re-run from the stored assignment. Bench B: none to the plan; token and time accounting uses
transcripts instead of the harness's summary, decided before analysis because the summary was found to be cumulative.
Several agents also stopped without changing code on a retry (declining to guess); those attempts are counted as failures.

Registration hashes (sha256): Bench A `ea03bc82...`, amendments `e62241f1...` and `acb49f83...`; Bench B `1dbd7e76...`.
The task bank and graders are withheld so future runs are not contaminated; the per-run data is in `agent_ab_data/`.
