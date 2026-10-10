# CommonTrace: Competitive Analysis and State-of-the-Art Master Prompt

Prepared 2026-10-08. Covers the CommonTrace v2 codebase (`f44bde0`) and the
default branches of eight competitor repositories cloned the same day:
Graphiti, Zep, mem0, Cognee, Hindsight, EverOS, Supermemory and Letta Code.
It also covers the 2025–2026 agent-memory literature.

The competitor benchmark numbers below are **self-reported** by each vendor,
with different readers, judges and budgets. They were not reproduced here.
Treat them as the market's perception, not as verified fact. Part G (the
master prompt) makes reproducing them under one harness the first piece of work.

Parts A–F are the analysis. **Part G is the single copy-paste prompt.**

---

## Part A — Verdict in one paragraph

CommonTrace is the only system in this set that can **prove** a memory helped.
It runs a per-memory randomized holdout with deterministic assignment and
anytime-valid confidence sequences. It withdraws memories proven to cause harm,
signs proof packages that anyone can re-derive, and bills on proven value. Around
that sits enterprise-grade governance and a conformance suite. None of the eight
competitors has any of this. CommonTrace is behind on what the market currently
buys: published answer accuracy (competitors claim 92–95% on LoCoMo and
LongMemEval; CommonTrace publishes retrieval-evidence metrics only, with no
judged accuracy), extraction intelligence, integration breadth, packaging
(not on PyPI, no hosted cloud) and scale backends. The path to a category-defining
product is to:

1. Reach accuracy parity, measured honestly under one harness for every system.
2. Turn the causal engine into the industry's neutral referee and its learning
   optimizer.
3. Add the things nobody else has: cross-level experience compression governed by
   causal evidence, origin-bound memory security, and a federated commons that
   creates network effects.

---

## Part B — Competitor teardown (what each actually ships)

| | Graphiti (getzep) | Zep Cloud | mem0 | Cognee |
|---|---|---|---|---|
| Licence / form | Apache-2.0 OSS lib, ~69k LOC Py | Proprietary cloud; repo holds examples, SDKs, eval | Apache-2.0 lib + self-host server + cloud, ~32k LOC Py + TS | Apache-2.0, ~51k LOC Py + frontend, Rust & TS SDKs |
| Core model | Temporal context graph: entities, bi-temporal fact edges, episodes (provenance), communities, sagas | Graphiti on a proprietary "Context Graph Engine"; users/threads | Flat memories + entity store; April-2026 "ADD-only" single-pass extraction; agent facts first-class | Graph + vector + chunks + code graph; `remember / recall / improve / forget` |
| Retrieval | cosine + BM25 + BFS per nodes/edges/episodes/communities; rerankers RRF, MMR, node-distance, episode-mentions, cross-encoder; ~17 named search recipes | sub-200 ms claim | semantic + sigmoid-normalised BM25 + entity boost (≤0.5) + temporal ranking, fused | 30+ retrievers: graph-completion CoT, decomposition, temporal, Cypher, NL→Cypher, coding-rules, triplet, summaries |
| Learning | Incremental graph updates, edge invalidation | Custom ontology/instructions | Additive accumulation | Session distillation (curator → writer/rejecter → persist, watermark); feedback weights on graph edges; proposals API |
| Distinctive | Pydantic ontologies (prescribed + learned); `LLMRuntime` routes each prompt to a model; GLiNER2 local extraction; Neo4j / FalkorDB(+lite) / Neptune | `zep-ingest` (Slack, docs, email, CSV, fact triples; fixes timestamp/alias/size pitfalls); eval harness with run manifests + config snapshots; 12 framework integrations; Py/TS/Go SDKs | 28 vector stores, 20 LLMs, 6 rerankers; **agent self-signup** (`mem0 init --agent` mints a key in ~5 s); 18 plugins (Claude Code, Codex, Cursor, n8n, Zapier…); "skills" for coding assistants | Works with no LLM (GLiNER + local embeddings); OWL ontologies; dataset permissions; distributed workers; Kuzu/Neo4j/Neptune/Turso/Postgres; LanceDB/pgvector |
| Claimed perf | — | Paper arXiv 2501.13956 | LoCoMo 92.5, LongMemEval 94.4, BEAM-1M 64.1, BEAM-10M 48.6, ~7K tokens, p50 ≈ 1 s (managed platform) | Paper arXiv 2505.24478 |

| | Hindsight (vectorize) | EverOS (EverMind) | Supermemory | Letta Code |
|---|---|---|---|---|
| Licence / form | MIT; huge (~170k LOC engine); Docker, pip, embedded pg0, Helm, cloud | Local-first lib/server, ~49k LOC Py | TS monorepo; cloud + "one binary" local | TS harness (~550k LOC incl. tests), CLI + desktop + web |
| Core model | World facts / experiences / **observations** (proof-counted) / **mental models** (standing answers auto-refreshed) / knowledge pages; banks with disposition traits; **directives** (hard rules) | Markdown is source of truth; SQLite + LanceDB derived and rebuildable; user episodes/profile/atomic facts/**foresight**; agent **cases → skills** (SKILL.md) | Facts, static + dynamic **user profiles** (~50 ms), automatic forgetting of temporary facts, contradiction handling | Memory blocks, **MemFS** (git-tracked, signed), skills, **dreaming/reflection** (sleep-time compute), shared memory repos across agents, **Memory Palace** dashboard |
| Retrieval | 4 parallel arms (semantic, BM25, graph entity/temporal/causal, time range) → RRF → cross-encoder; `reflect` agent: mental models → observations → raw facts | keyword / vector / hybrid; orthogonal scopes user/agent/app/project/session | Hybrid RAG + memory in one call; 95% Recall@15 at ~720 tokens | `/search` across agents; recall subagent |
| Distinctive | LLM wrapper in 2 lines (`wrap_openai`); 60+ integrations; coding-agent installer for 13 CLIs that ingests git history; MCP per bank; 45-pattern memory defense; multilingual (native script kept); Oracle 23ai; webhooks; Prometheus | Offline Memory Engine (cron strategies), weekly reflection merges episode clusters, foresight extraction, knowledge wiki, GitHub sync, `reproduce.sh` benchmarks | Connectors (Drive, Gmail, Notion, OneDrive, GitHub) with webhooks; OCR, video transcription, AST code chunking; SMFS filesystem (3× fewer tokens on xAFS); **MemoryBench** open benchmark framework + "benchmark your memory" skill; voice SDKs (LiveKit, Pipecat, Cartesia) | Self-configuring agents; channels (Slack, Telegram, Discord); crons/heartbeats; remote computers; secret obfuscation; skills install from GitHub/ClawHub/Hermes; MemGPT + sleep-time-compute lineage |
| Claimed perf | "Most accurate ever tested" on LongMemEval; independently reproduced (Virginia Tech, Washington Post); live leaderboard; paper arXiv 2512.12818 | LoCoMo 94.42, LongMemEval 94.00 | "#1" LongMemEval, LoCoMo, ConvoMem | — |

**Market signals.** mem0 raised about $24M (YC, Basis Set, Peak XV) and reports
large developer counts. Letta raised a $10M seed (Felicis). Zep moved from open
source to open core (its Community Edition is deprecated). Everyone ships
Claude Code, Codex and Cursor plugins, MCP, and a "run locally" story. Plain
recall is becoming a commodity: LoCoMo and LongMemEval are near saturation
(92–95), and the new benchmarks (MemoryArena, AMA-Bench, MemGym, DolphinBench)
show those systems falling apart on agentic tasks.

---

## Part C — CommonTrace today (honest position)

### Unique strengths (no competitor has these)

1. **Causal measurement.** Per-memory randomized holdout with deterministic
   BLAKE2b assignment (PROTOCOL §13.1), Benjamini–Hochberg correction,
   anytime-valid confidence sequences, power planning (`experiment --plan`),
   minimum detectable effect, survival analysis for delayed outcomes, and
   pre-registration.
2. **Acting on evidence.** `--on-harm withdraw` stops serving memories proven to
   hurt, without biasing the experiment. `working_set` graduates proven memories
   into a pinned, prefix-cacheable block.
3. **A neutral referee already exists.** `commontrace.measure.CausalMemory` wraps
   *any* store's retrieval call (mem0, Zep, a vector DB) and measures it without
   migrating anything. This is the wedge into every competitor's customer base.
4. **Proof and money.** Signed proof packages anyone can re-derive (`proof
   verify`), a hash-chained value ledger, and value-based billing with no default
   prices.
5. **Governance.** Review gate, two-person and require-human approval policies,
   revision journal, content-addressed releases, environments, a CI `gate` (JUnit
   and GitHub output), and content screens for secrets and injection.
6. **Hub.** Multi-tenant Postgres with RLS, SCIM, SSO, audit export, retention
   and legal hold, KMS encryption, quotas, outcome connectors (Zendesk, GitHub,
   Greenhouse, Intercom), and OTLP ingest.
7. **Protocol and conformance suite.** Language-neutral vectors, a Go example, and
   a gateway for robots and any language. Sim and real data are never pooled.
8. **Zero-LLM default and engineering rigour.** About 5,900 tests, source-bound
   measurement provenance, and an honesty culture in the docs.

### Gaps that cost deals today

| # | Gap | Evidence in repo | Competitor bar |
|---|---|---|---|
| 1 | No model-backed answer accuracy published | README: "Model-backed answer accuracy, matched competitor results and BEAM 1M/10M remain unverified" | 92–95 on LoCoMo/LongMemEval, published everywhere |
| 2 | Weak retrieval categories | LoCoMo multi-hop evidence 52% @1.5K; BEAM summarization 28%; BEAM turn Recall@5 30% | Supermemory 95% R@15 at ~720 tokens |
| 3 | Dense and rerank arms never evaluated in the scoreboard | `effective_embedders: []` in published runs | All competitors run hybrid + cross-encoder |
| 4 | Lesson extraction is heuristic | `distill` = word-overlap clustering; LLM drafting optional | LLM extraction, entity resolution, reflect agents, auto-refreshed mental models |
| 5 | Scale backends | Files + SQLite; exact linear dense scan; pgvector optional; no `store.py` abstraction (PROTOCOL §12 admits this) | 28 vector stores (mem0); Neo4j/FalkorDB/Neptune; sub-200 ms at scale (Zep) |
| 6 | Packaging and onboarding | Not on PyPI; no hosted cloud; no agent self-signup | `pip install`, `npx … local`, 5-second agent signup, one-binary local |
| 7 | Integration breadth | 3 framework integrations, 5 install targets, 4 outcome connectors, 2 ingest connectors | 60+ (Hindsight), 18 plugins (mem0), 12 frameworks (Zep), Drive/Gmail/Notion (Supermemory) |
| 8 | SDKs | Python + a thin TS Hub client | Py/TS/Go (Zep, Hindsight), Rust (Cognee) |
| 9 | Skills from experience | `skills.py` loads skills; `procedural.py` records workflows; no automatic, verified skill crystallization | EverOS cases→skills, Letta skill-creator, research (Skill-Pro, MSCE, ReMe) |
| 10 | Security model | Pattern-based screens (`defense.py`, `memory_guard.py`, `injection_guard.py`); commons signing is HMAC, so verifiers can also sign | Papers show content- and lineage-based defenses fail under laundering (arXiv 2606.24322) |
| 11 | Multilingual | English stemming in the conversation pipeline | Hindsight preserves native script end-to-end |
| 12 | Messaging | 2,967-line README; the product reads as a protocol + a CLI + a memory + a measurement tool | One-sentence positioning everywhere else |

---

## Part D — What to adopt from each competitor (ranked by ROI)

1. **mem0:** multi-signal fusion (dense + sigmoid-normalised BM25 + entity boost
   + temporal ranking), single-pass ADD-only extraction that never overwrites
   (fits CommonTrace's append-only, bi-temporal philosophy), agent
   self-signup, plugin-per-agent distribution, "skills" that teach coding
   assistants your SDK.
2. **Hindsight:** observations with proof counts (partly done in
   `observations.py`); mental models as standing questions refreshed in the
   background; directives as first-class hard rules; the reflect agent's
   hierarchy (curated → consolidated → raw); the 2-line LLM wrapper as the
   headline onboarding; a coding-agent installer that bootstraps memory from git
   history; a live, independently reproduced benchmark site.
3. **Supermemory:** a context-reduction target (high recall at a tiny token
   budget); static + dynamic profile in one ~50 ms call; automatic expiry of
   temporary facts; a one-binary local mode with the same API as cloud; an open
   benchmark framework that compares vendors (build CommonTrace's own, with
   causal metrics).
4. **Graphiti / Zep:** named search recipes; MMR, node-distance and
   episode-mentions rerankers; Pydantic/JSON-schema ontologies (prescribed and
   learned); per-prompt model routing (`LLMRuntime`); GLiNER local extraction;
   pluggable graph backends; `zep-ingest`'s guards (timestamps, aliases, size
   limits, batch API); eval run manifests with config snapshots.
5. **Cognee:** retriever registry (decomposition, CoT graph completion,
   NL→query); feedback-weighted graph edges; session distillation with a
   watermark that refuses to seal entries when an LLM call failed; proposals
   API; operate fully without an LLM using local models.
6. **EverOS:** Markdown as the source of truth with rebuildable indexes
   (CommonTrace already shares this philosophy, so say so loudly); a cron-driven
   offline memory engine; foresight (anticipatory notes); clustering agent cases
   into SKILL.md skills; orthogonal scopes (user, agent, app, project, session);
   a one-command `reproduce.sh` for benchmarks.
7. **Letta Code:** git-backed MemFS with signing (`memfs.py` exists, so extend
   it); sleep-time "dreaming" (`dream` exists, so budget and measure it); a
   Memory Palace style console (overview, needs attention, suggestions with
   action buttons); shared memory repos attachable to many agents; cron and
   heartbeat agents.

---

## Part E — Research digest (2025–2026) and what to take from each paper

| Theme | Paper (arXiv) | Take-away for CommonTrace |
|---|---|---|
| Causal utility of memory | **Causal Memory Policy** 2610.02070 | Utility is *unidentified* for never-retrieved memories (54–67% of required memories). Fix: reserve exploration slots sampled with known propensities, estimate with self-normalised IPW. Natural extension of the existing holdout. |
| Hindsight credit | Hindsight Memory-PRM 2608.29605; HiMPO 2606.16285; CHIME 2609.02074; TIDE 2609.37544 | Deletion-and-reanswer probes as causal credit; attribute the outcome to plan vs execution *before* memorizing; delayed, noisy business feedback (TIDE's Memory Evolution Gain metric). |
| Decision-aware context | CICL 2606.08151 | Rank context by expected effect on the next action, not similarity. Use causal utility estimates as a ranking prior. |
| Experience → skills | ReMe 2512.10696; Skill-Pro 2602.01869; MACLA 2512.18950; APEX-EM 2603.29093; BREW 2511.20297; MSCE 2607.16621; HyperSkill 2608.16114; LifeMem 2609.12655 | Contrastive success/failure distillation; Bayesian reliability per procedure; a PPO-style *gate* before a skill is admitted; structural signatures give 3.3× the transfer of semantic-only retrieval; hindsight relabelling of near-misses; skills that keep evidence links and applicability bounds. |
| Caution | **SkillEvolBench** 2605.24117 | Raw-trajectory reuse often *beats* distilled skills, so every abstraction level must be causally tested against the raw trace. CommonTrace is uniquely able to do this. |
| Unifying theory | **Experience Compression Spectrum** 2604.15877 | Memory (5–20×), skills (50–500×) and rules (1000×+) form one axis; *no system adapts across levels* (the "missing diagonal"). Build it, with promotion and demotion driven by causal verdicts. |
| Offline consolidation | Sleep-time Compute 2504.13171; Auto-Dreamer 2605.20616 | Anticipate queries offline (5× less test-time compute); learned consolidation shrinks the memory bank 6–12×. |
| Learned memory ops | Memory-R1 2508.19828; Mem-α 2509.25911; MemRL 2601.03192; Mem-T 2601.23014; MemPO 2603.00680; SelfMem 2607.03726 | Optional learned memory-manager policies. The holdout logs are exactly the reward data these need. |
| Security | TMA-NM 2606.24322; SMSR 2606.12703; MemAudit 2605.23723; Trojan Hippo Bench 2605.01970; MemPoison 2605.29960; Salami/MemCollusion 2608.01637; FARMA/SENTINEL 2607.05029; Sleeper poisoning 2605.15338 | Content- and lineage-based defenses are provably insufficient under laundering. Need write-time origin binding, non-malleable authority, Sybil-resistant corroboration, signed records, randomized-ablation voting for high-stakes actions, collusion detection and post-hoc causal forensics. |
| Multi-principal governance | GateMem 2606.18829; PiSAs 2607.05318; AIM 2609.12320; Governed Shared Memory 2606.24535; Governed Collaborative Memory 2605.04264 | Contextual-integrity access per principal, verified forgetting, provenance reconstruction, a pipeline-ordering bug class (dedup gate vs contradiction detector). |
| Federated learning across orgs | Federated Agent Optimization 2610.01195; DecentMem 2605.22721 | Share abstracted, protected experience across organizations. This is the network-effect business. |
| Type-conditioned decay | ScrubJay-MEM 2608.04746 | Per-memory perishability coefficient; extend `decay.py` and `ttl.py`. |
| Agentic benchmarks | MemoryAgentBench 2507.05257; MemoryArena 2602.16313; AMA-Bench 2602.22769; MemGym 2605.20833; DolphinBench 2609.24971; Evo-Memory 2511.20857; BEAM 2510.27246; WorldMemArena 2605.29341 | The next leaderboard is agentic and cost-aware. DolphinBench requires cost and latency next to accuracy; CommonTrace already has a harness stub in `benchmarks/dolphinbench/`. |

---

## Part F — Distinctive bets (creative, defensible, hard to copy)

1. **Category: "Agent Learning Assurance."** Others sell *remembering*.
   CommonTrace sells *proven learning*: a system of record for what a fleet has
   learned, with causal proof it pays. Tagline: **"Memory that proves it works."**
2. **The neutral referee (CausalMemory everywhere).** A drop-in wrapper and
   gateway that A/B-tests *any* vendor's memory in the customer's own
   production traffic and issues a signed lift certificate. Competitors become
   distribution: "Bring your mem0, Zep or Supermemory. We tell you, causally,
   whether it helps."
3. **Counterfactual CI for retrieval.** Every retrieval is logged with
   propensities, so a new ranker, embedding model or prompt can be scored by
   off-policy evaluation *before* it ships. `commontrace policy evaluate` blocks
   a release that would lower outcomes. Nobody else can do this because nobody
   else logs propensities.
4. **The missing diagonal.** One evidence-governed ladder: trace → episode →
   observation → lesson → verified skill → directive/rule → (optional) training
   data for weights. Items move *up* only when the holdout says HELPS against
   the level below. They move *down* on HURTS. This answers SkillEvolBench's
   finding with proof per item.
5. **Origin-bound memory authority.** Every record carries an unforgeable
   origin label at write time. Summarization, tool echo and self-corroboration
   cannot raise its authority. Action policies state which origins may influence
   which actions ("memory from an inbound email can never authorize a refund").
   This is a provable guarantee against laundering, sold to security buyers.
6. **Memory incident forensics.** "Which memory made the agent do that?" A
   deletion-and-replay counterfactual over the recorded receipts, ranked by
   causal influence, exportable as an incident report. This is the
   post-incident tool the CISO and the regulator ask for.
7. **Federated Commons with replicated-lift certificates.** Organizations
   exchange abstracted lessons under differential privacy. Per-fleet effect
   estimates are pooled by meta-analysis ("this lesson's lift replicated in 14
   fleets, pooled +9.1% [5.2, 13.0]"). Publishers earn revenue share. Each new
   fleet makes every lesson's evidence stronger, which is a network effect no
   pure memory vendor can copy.
8. **Outcome-priced memory.** Already started (`pricing.py`, `value_billing.py`).
   Make "pay only for proven lift" the default commercial model. It is credible
   only because the proof is re-derivable.
9. **Cost-aware learning.** Every injected token is a cost. Optimize lift per
   token, report the Pareto frontier (DolphinBench style), and graduate proven
   memories into prompt-cacheable working sets automatically.
10. **Embodied fleets.** The gateway and fleet adopt/merge (no sim/real pooling)
    is unique. Extend it to multimodal episodes and safety-protected memories
    for robotics, where proof of improvement is a regulatory need.

---

## Part G — The master prompt (copy everything inside the block)

```text
=====================================================================
COMMONTRACE STATE-OF-THE-ART PROGRAM: MASTER PROMPT FOR A CODING AGENT
=====================================================================

ROLE
You are the principal engineer and research lead for CommonTrace
(repo: commontrace-v2). Your mandate: make CommonTrace the category-defining,
state-of-the-art platform for agent memory and learning, measurably better
than Graphiti/Zep, mem0, Cognee, Hindsight, EverOS, Supermemory and Letta,
while keeping its unique causal-proof and governance core. Work in phases,
ship in small validated PRs, and never claim a result you have not measured.

------------------------------------------------------------------
0. READ FIRST (do not rebuild what exists)
------------------------------------------------------------------
Read, in order: AGENTS.md, protocol/PROTOCOL.md, README.md (sections 5-12,
"Working memory, facts, graph and ingestion", "Conversation memory",
"Measurement scoreboard"), docs/architecture.md, docs/performance.md,
CHANGELOG.md [Unreleased], tests/test_competitor_features.py.

Already implemented (extend, never duplicate):
- Lessons with review gate, approval policies (two-person, require_human),
  revision journal, releases/environments, `gate` for CI.
- Randomized holdout (BLAKE2b assignment, PROTOCOL 13.1), anytime-valid
  verdicts, BH correction, power planning, survival analysis, prereg,
  harm withdrawal (harm.py), working_set graduation, CausalMemory wrapper
  (measure.py) for third-party stores, proof packages + value ledger
  (proof.py, value.py), value billing (pricing.py, hub/value_billing.py).
- Retrieval: lexical BM25 fallback, optional dense (semantic_arm.py),
  cross-encoder rerank (rerank_arm.py), graph boost, reliability/recency
  weights, dosage/redundancy, receipts, exclude_shown.
- Facts (bi-temporal, evidence-bound), graph (bi-temporal edges, ontology,
  entities, communities), observations, sagas, knowledge pages, procedural
  memory, skills loading, memory blocks, conversation memory (FTS5 + optional
  vectors, temporal grounding, profiles, summaries, extraction),
  ingestion (multimodal, local dir, web crawler), jobs, daemon, dream,
  memfs (git), defense/memory_guard/injection_guard, PII redaction.
- MCP server (stdio, streamable-http, sse), gateway (HTTP/stdio) + console
  (no build step), Hub (Postgres RLS, SCIM, SSO, audit, retention, KMS,
  outcome connectors, OTLP ingest), TS hub client, conformance suite.
- Benchmarks: benchmarks/conversation_bench.py, compare.py, judges for
  LoCoMo/LongMemEval/BEAM/Dolphin, dolphinbench harness stub.

------------------------------------------------------------------
1. NON-NEGOTIABLES
------------------------------------------------------------------
1. Follow AGENTS.md: Python 3.10+, stdlib-first, argparse, os.path.join,
   no hardcoded platform paths, root auto-detection, COMMONTRACE_ROOT.
2. Core install stays dependency-light (PyYAML only). Every heavy capability
   (LLMs, embeddings, graph DBs, vector DBs, GLiNER) is an optional extra
   with a working fallback.
3. Zero-LLM default stays. LLM-powered paths are opt-in, budgeted, cached,
   and never activate a lesson. Only the review gate can do that.
4. Honesty: no number in README/docs without a reproducible command,
   pinned dataset hash, pinned product revision and run manifest. State
   limitations next to results. Competitor numbers are labelled
   "self-reported" unless re-run under our harness.
5. Causal-validity invariants are sacred: assignment is a pure function of
   (salt, memory, occasion); nothing new may change an arm after assignment;
   new estimators get conformance vectors; anytime-valid by default.
6. Security: every new write path goes through the existing guards; every
   new network surface gets auth, Host/Origin checks, admission limits,
   and tests.
7. Additive protocol changes bump the minor version; never break v2 readers.
8. Each PR: focused scope, tests (unit + e2e where relevant), ruff clean,
   full `python3 -m pytest tests/ -q` and `hub/tests` green, CHANGELOG
   entry, docs updated. Do not modify memory/ during a live run.

------------------------------------------------------------------
2. NORTH-STAR TARGETS (all measured under OUR harness, published with
   manifests; "competitors" = re-run through the same reader/judge)
------------------------------------------------------------------
A. Accuracy: >= best re-run competitor on LoCoMo (cat 1-4), LongMemEval-S,
   BEAM 100K/1M/10M, with identical reader + judge, reported with
   bootstrap CIs, tokens/query and p50/p95 latency.
B. Agentic: top-3 on at least two of MemoryArena, AMA-Bench, MemGym,
   DolphinBench, Evo-Memory, with cost and latency reported (Pareto plot).
C. Efficiency: >= 90% evidence recall within 1,000 context tokens on
   LongMemEval-S (Supermemory-class context reduction).
D. Latency: lexical recall p95 < 30 ms at 10k lessons (keep); hybrid
   recall p50 < 50 ms / p95 < 150 ms at 1M memories with ANN on a 4-core
   CPU, recall@10 >= 0.97 of exact.
E. Causal: detect a seeded +5pp lesson effect at 80% power with <= 2,000
   occasions (adaptive design), with anytime-valid coverage >= 95% in
   simulation (commons/eval/causal_harness extended).
F. Security: 0% attack success on direct and laundering memory-poisoning
   suites at >= 95% clean utility.
G. Adoption: `pip install commontrace`, `npm i @commontrace/sdk`,
   `docker run` one-liner and agent self-signup to first recall in < 60 s.

------------------------------------------------------------------
3. PHASE 0: TRUTH BASELINE (do this before any feature work)
------------------------------------------------------------------
0.1 Reproduce the scoreboard with attention extras installed
    (dense + cross-encoder ON) and record effective_embedders.
0.2 Add model-backed answer accuracy to benchmarks/conversation_bench.py
    using the existing judges, with a pinned reader and judge (configurable
    provider via commontrace/llm.py). Report per-category accuracy, CI,
    tokens and latency. Include BEAM 1M and 10M.
0.3 Build benchmarks/adapters for mem0 (OSS), Graphiti, Cognee, Hindsight
    (embedded), EverOS, Supermemory local and Letta (where runnable
    offline), and run them through the SAME harness and reader/judge.
    Where a system cannot run locally, mark it "not reproduced".
0.4 Add benchmarks/agentic/: adapters for MemoryArena, AMA-Bench,
    MemoryAgentBench, Evo-Memory and DolphinBench (complete the stub).
0.5 Produce docs/benchmarks/ with a generated leaderboard (static HTML +
    JSON) from signed run manifests; add `scripts/reproduce.sh`.
Exit gate: a failing-category list ranked by gap size drives Phase 1.

------------------------------------------------------------------
4. WORKSTREAMS (run 1-3 first; 4-10 in parallel after Phase 0)
------------------------------------------------------------------

WS1. RETRIEVAL TO STATE OF THE ART
- Multi-signal fusion: dense + sigmoid-normalised BM25 + entity-link boost
  + temporal relevance + graph proximity + causal-utility prior (from the
  holdout; zero for unproven) with learned or recipe-configured weights.
  Expose named "recipes" (like Graphiti) in RetrievalConfig.
- Rerankers: add MMR, node-distance, episode-mention and a
  decision-aware reranker (expected effect on next action, CICL).
- Query planning: decomposition for multi-hop and aggregation, iterative
  2-hop entity bridging (already partial), time-window parsing for more
  languages, abstention detection with calibrated "insufficient evidence".
- Summarization and event-ordering questions: hierarchical session/topic
  summaries (sagas, communities) as retrievable units with source links.
- Context compression: evidence-sentence extraction to hit target C.
- Entity resolution: alias merge with provenance, optional GLiNER local
  NER extra, optional LLM resolution; never lose source identity.
- Optional extraction mode "additive": single-pass, ADD-only, bi-temporal,
  agent-generated facts first-class (mem0's April-2026 design) as an extra.
- Multilingual: language detection, native-script preservation, per-
  language stemming/tokenizers with a Unicode fallback.
- Scale: implement the missing store abstraction (PROTOCOL 12):
  commontrace/store.py protocol with backends: files (default), SQLite,
  Postgres+pgvector (HNSW, iterative scan), optional LanceDB, optional
  Neo4j/FalkorDB for the graph. Keep exact fallbacks and source-identity
  checks. Benchmark target D.
Acceptance: targets A, C and D met or the gap is documented with cause.

WS2. CAUSAL ENGINE 2.0 (the moat)
- Retrieval-level exploration (Causal Memory Policy, arXiv 2610.02070):
  reserve k context slots filled by propensity-sampled memories, including
  never-retrieved ones; log propensities; SNIPW and doubly-robust
  estimators; conformance vectors for the sampler.
- Off-policy evaluation: `commontrace policy evaluate --candidate CONFIG`
  scores a new ranker/embedder/prompt/dosage from logged propensities
  (IPS, SNIPS, DR) with CIs; wire it into `commontrace gate` so a release
  that lowers expected outcomes is blocked ("counterfactual CI").
- Adaptive allocation: optional Thompson/top-two sampling that shrinks the
  holdout for proven memories and explores uncertain ones, using
  confidence sequences that stay valid under adaptive designs; preserve
  the deterministic-assignment property by hashing on a published
  allocation schedule version.
- Heterogeneous effects: estimate where a lesson helps (by tag, scope,
  agent_type, context cluster) and auto-draft a tightened applies_when
  (status=review) from the evidence; interaction effects for lessons that
  co-fire (factorial reading of independent per-lesson arms).
- Attribute-before-memorize (CHIME): classify an outcome as plan vs
  execution vs environment failure before it updates any lesson's evidence.
- Hindsight credit: deletion-and-replay probes (Memory-PRM) on sampled
  occasions where a replay harness exists (agent_loop.py), feeding a
  presence-credit estimate shown beside the randomized one.
- Memory forensics: `commontrace forensics <occasion_id>` ranks the
  memories that influenced an outcome via receipts + counterfactual
  replay; exports a signed incident report.
- Neutral referee: harden measure.CausalMemory into adapters for mem0,
  Zep, Supermemory, Hindsight, Cognee and LangGraph stores plus a gateway
  endpoint, so any vendor's memory can be A/B-tested in production and
  receive a signed lift certificate (proof.py).
Acceptance: target E; OPE matches on-policy truth within CI on seeded sims.

WS3. EXPERIENCE COMPRESSION LADDER ("the missing diagonal")
- Tiers: trace -> episode -> observation -> lesson -> skill (executable
  SKILL.md with activation, execution, termination, verification checks,
  evidence links, reliability) -> directive (hard rule) -> optional export
  as SFT/DPO data.
- Crystallization: cluster successful and failed traces by structural
  signature (operation sequence, tools, error class; APEX-EM) and
  semantics; contrastive success/failure refinement (MACLA, ReMe);
  hindsight relabelling of near misses (BREW). Drafts land at
  status=review; LLM drafting optional.
- Governance by evidence: a candidate is promoted only if its holdout
  verdict beats the level below it (raw trace replay is the control, per
  SkillEvolBench); demoted or withdrawn on HURTS; every move journaled.
- Directives: first-class always-injected rules with priority, scope and
  the core:true semantics; conflicts surfaced by `reliability`.
- Skills publish to Claude Code/Codex/Cursor skill folders via
  `commontrace install`.
Acceptance: measurable gains on Evo-Memory or SkillEvolBench vs
raw-trajectory and no-memory controls, with causal verdicts per skill.

WS4. MEMORY SECURITY YOU CAN PROVE
- Origin-bound authority (TMA-NM, arXiv 2606.24322): an immutable origin
  label at write time (user, operator, tool:<name>, external:<channel>,
  agent-derived(from=...)); derived memories inherit the LEAST trusted
  origin; summarization/echo cannot elevate; elevation only via
  corroboration from distinct, independently authenticated origins.
- Action policies: `memory/authority-policy.yaml` mapping action classes
  to the minimum origin trust allowed to influence them; enforced in
  retrieval output (filter + receipt reason).
- Signed records: per-store Ed25519 signing of memory records and commons
  exports (replace HMAC trust groups, where verifiers can also sign, with
  asymmetric keys; keep HMAC compatibility).
- High-stakes retrieval smoothing (SMSR): optional randomized-ablation
  verdict voting with a certificate.
- Collusion detection: flag memory sets that are individually benign but
  jointly steer toward a sensitive action (salami attacks).
- Red-team suite: tests/security/ reproducing MINJA, Trojan Hippo,
  MemPoison, FARMA, sleeper and laundering attacks; report ASR + utility.
- Multi-principal contextual integrity (GateMem, PiSAs): per-principal
  read scopes, verified forgetting with deletion certificates proving a
  deleted item can no longer be retrieved from any index or cache.
Acceptance: target F; GateMem-style utility/access/forgetting report.

WS5. FEDERATED COMMONS AND NETWORK EFFECTS
- Cross-org exchange of abstracted lessons (never raw traces) with
  differential privacy on shared statistics; MinHash overlap (exists).
- Meta-analysis: pool per-fleet effect estimates (random-effects with
  heterogeneity) into "replicated lift" certificates; display I^2.
- Marketplace in the Hub: publish proven lessons/skills/packs with
  signed lift certificates; licence terms; publisher revenue share wired
  into value billing; operator review stays mandatory.
Acceptance: simulation shows pooled estimates with correct coverage and a
privacy budget accountant; no org-identifying data leaves an org.

WS6. FORESIGHT AND SLEEP-TIME COMPUTE
- Extend `dream`/`daemon` into a budgeted offline engine: predict likely
  next queries per scope (from receipts), precompute briefs and answers,
  refresh mental models (standing questions) and observations, merge
  episode clusters, expire perishable facts (type-conditioned decay,
  ScrubJay).
- Treat each offline artifact as an experimental arm so its value is
  measured causally, not assumed. Budget tokens and cost per run.
Acceptance: test-time tokens or latency reduced at equal or better
accuracy on a multi-query benchmark, with causal verdicts.

WS7. ADOPTION SURFACE
- Packaging: publish to PyPI and npm; Docker image; `commontrace up`
  one-liner (local gateway + console + MCP); Helm chart (exists) for Hub.
- Agent self-signup on a Hub (`commontrace init --agent`) issuing a
  scoped, rate-limited key a human can later claim.
- LLM wrappers: `from commontrace import wrap_openai, wrap_anthropic,
  wrap_litellm` (promote litellm_wrapper.py), recall-before/capture-after
  with occasion ids so every wrapped call joins the experiment.
- Auto-capture plugins (hooks) for Claude Code, Codex, Cursor, Copilot
  CLI, Gemini CLI, OpenCode, Windsurf: session start -> working_set +
  retrieve; session end -> capture trace with outcome detection; git
  history bootstrap. Live-test each install target and mark it verified.
- Framework integrations: CrewAI, AutoGen/AG2, LlamaIndex, Mastra, Vercel
  AI SDK, OpenAI Agents, Google ADK, Strands, Pydantic AI, LangGraph
  (some exist; complete and test).
- Connectors: Slack, Jira, Linear, ServiceNow, Salesforce, Notion, Google
  Drive, Gmail, Confluence, GitHub (knowledge in), plus outcome connectors
  (exist for Zendesk/GitHub/Greenhouse/Intercom).
- SDKs generated from an OpenAPI spec of the gateway: TypeScript (full),
  Go, Rust, Java/Kotlin; conformance-tested.
- Remote MCP with OAuth 2.1 for hosted Hubs.
Acceptance: target G; every integration has a smoke test in CI.

WS8. PRODUCT EXPERIENCE ("Learning Ledger" console)
- Keep the no-build-step console. Add: an executive view (proven value
  with CIs, lift per token, harm prevented), a Needs Attention queue
  (drafts, contradictions, harm verdicts, underpowered lessons) with
  one-click actions routed through the existing revision-checked review
  flow, a graph and saga explorer, release diffs, an experiment designer,
  forensics reports and a weekly digest export.
- Messaging: cut README to a 1-page pitch + quick start; move reference
  material to docs/. Positioning line: "Memory that proves it works."
Acceptance: accessibility checks pass; phone-width layout; light/dark.

WS9. BENCHMARK LEADERSHIP
- Publish the leaderboard from Phase 0 continuously (CI job, signed
  manifests, dataset hashes); invite independent reproduction.
- Create CausalMemBench: seeded fleets with known helpful, neutral and
  harmful memories, delayed and noisy outcomes, and poisoning; metrics:
  harm detection time, false-withdrawal rate, lift estimation error,
  cost. Run every competitor through it via CausalMemory adapters.
Acceptance: benchmark + paper draft in docs/research/.

WS10. EMBODIED AND MULTIMODAL FLEETS
- Multimodal episodes (image, audio, video, sensor summaries) through the
  gateway with protected safety memories; sim/real separation (exists);
  fleet pooling refusal rules; latency budget for on-robot recall.
Acceptance: an end-to-end robot fleet demo with a causal report.

------------------------------------------------------------------
5. PROCESS AND OUTPUT FORMAT
------------------------------------------------------------------
For each workstream:
1. Write docs/plans/<ws>.md: problem, design, invariants touched, tests,
   benchmark to move, risks, rollback.
2. Implement in small PRs (<= ~800 lines diff each), each green.
3. Report after each PR: what changed, measured before/after with the
   reproduce command, what is NOT yet proven.
4. Keep a running docs/strategy/scorecard.md with targets A-G, current
   values, and the source manifest for each number.
Stop and ask the human before: breaking a protocol field, changing
assignment semantics, adding a mandatory dependency, or publishing any
external claim.

BEGIN WITH PHASE 0. Do not start WS1+ until the truth baseline exists.
=====================================================================
```

---

## Sources

- Competitor repositories, cloned 2026-10-08: github.com/getzep/graphiti,
  getzep/zep, mem0ai/mem0, topoteretes/cognee, vectorize-io/hindsight,
  EverMind-AI/EverOS, supermemoryai/supermemory, letta-ai/letta-code.
- Papers: the arXiv identifiers cited in Part E.
- Funding context: [TechCrunch on mem0's raise](https://techcrunch.com/2025/10/28/mem0-raises-24m-from-yc-peak-xv-and-basis-set-to-build-the-memory-layer-for-ai-apps),
  [Sacra: mem0](https://sacra.com/c/mem0),
  [respan.ai market map: Letta](https://www.respan.ai/market-map/letta/alternatives),
  [AgenticWire comparison](https://www.agenticwire.news/article/mem0-zep-letta-agent-memory).
