# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Adaptive recall budgets.** `Options(adaptive_budget=True)`, `conversation recall --budget auto`, MCP `adaptive_budget` and the gateway's `adaptive_budget` size the context by question shape (summaries, orderings and counts 3x, lists and multi-facet questions 2x, capped at 12,000 tokens); `explain.budget` records the decision. Measured on full datasets with the keyword arm: BEAM 100K evidence 70.87% -> 79.37% at a 1,500 floor (2,804 mean tokens), LongMemEval-S 79.60% -> 83.02% (2,447), LoCoMo 78.69% -> 80.29% (1,697). The benchmark harness gains `--adaptive-budget`.
- **Subgroup effects.** `commontrace experiment --by agent_type|agent_id` (or `--covariates FILE`) reports each lesson's randomized effect per pre-treatment subgroup, Benjamini-Hochberg corrected, with Cochran's Q for heterogeneity; a lesson that helps one subgroup and hurts another is flagged `CROSSING`.
- **CausalMemBench and graduation.** `python -m benchmarks.causalmembench` scores memory policies against seeded ground truth in a confounded fleet with late and missing outcomes. `CausalMemory(graduate=True)` stops randomizing a memory once its anytime-valid verdict is HELPS.
- **Readiness probes.** Gateway `/v1/health/live` and `/v1/health/ready` (store, schemas, free disk; 503 when not ready).
- **Offline mode.** `commontrace --offline` / `COMMONTRACE_OFFLINE=1` refuses non-loopback network calls and loads models from the local cache only.
- **Named rerankers.** `providers.reranker("mmr" | "cross-encoder" | "cross-encoder-fast")` plus `register_reranker` for custom second stages.
- **Markdown editing.** `commontrace lesson edit SLUG` validates, screens and journals a hand edit; an edited active lesson returns to review.
- **One-command agent signup.** `commontrace init --agent-caller NAME`.
- **Release workflow.** Tag-triggered PyPI and npm publishing through OIDC trusted publishing.
- **Threat model.** `SECURITY.md` documents assets, threats, controls, trust assumptions and known limits.

- **Standard OpenAPI and generated SDKs.** Typed memory requests, responses and bearer authentication, schema-enforced gateway admission, a pinned Swagger UI, and checksum-pinned upstream OpenAPI Generator tooling for TypeScript, Go, Rust, Java and Kotlin. CI compiles each generated client; generated output remains a build artifact.
- **Learning assurance runtime.** Signed joint-policy assignment/outcome logs, preregistered fixed-horizon IPS/SNIPS/DR release gates, recorded recall and usage receipts, failure attribution, bounded action-ablation voting and signed replay incident reports. Adjacent compression levels require an independent review and a randomized comparison against both the parent and raw evidence. Store-level Ed25519 receipts, recursive forgetting certificates, protected aggregate releases and authenticated replicated-lift pooling retain explicit limits.
- **Optional engines and adoption adapters.** Source-bound Neo4j/FalkorDB graph snapshots, scoped LanceDB indexing, append-only Markdown/SQLite record adapters, native Google ADK/Strands/AG2/Vercel/Mastra tools and owner-scoped knowledge-provider connectors. CI exercises actual optional engines and framework SDKs. The README is now a short start page; detailed reference content moves to `docs/REFERENCE.md`.

- **Bounded real-data reproduction runner.** Pinned LoCoMo/LongMemEval development selections, matched original/current product harness, dense/reranker ablations, local vendor attempts, per-case failures and cluster comparisons; exact context-text counts report estimated-budget exceedances separately.

- **Executable local vendor benchmark profiles.** Mem0 raw-memory/Qdrant and Graphiti episodic/FalkorDBLite paths share source-bound evidence and bounded reader/judge accounting; their limited configurations are explicit. LoCoMo development limits select exact seeded question counts.

- **Bounded benchmark inference.** HTTP reader and multi-call judge requests reserve cost before dispatch, send output caps and avoid hidden retries. Missing or overbound usage invalidates the run; cache history is reported separately from current spend. Optional tokenizer counts and source-bound generation settings expose the measurement contract.
- **Configuration-bound benchmark caching.** Completion reuse binds opaque provider/account routing, generation settings and the completion implementation; legacy unbound rows cannot satisfy judged harness lookups. Endpoint and credential values are excluded from persisted identities.
- **Memory evolution interfaces.** Append-only extraction, governed multi-signal search recipes, scoped agent SDK distribution, standing-question refresh jobs, hard directives, a Memory Palace console, signed shared MemFS snapshots and budgeted offline consolidation. Experimental causal exploration, structural skill proposals and approved abstract experience exports retain evidence and explicit limitations. The Linux binary builder and independently reproduced synthetic benchmark are documented in `docs/MEMORY_EVOLUTION.md`.
- **Benchmark measurement fidelity and source-bound comparison.** New `benchmarks.compare` measures raw-turn/session Recall@5/10 and NDCG@10 with question-weighted conversation-cluster 95% intervals, exact case/provenance checks and a quality regression gate. Both public recall budgets retain temporal and eligibility fences. Parsed caches bind dataset/adapter content; canonical source edits refuse reuse. `full-context` retains entire history; `budgeted-history` names the earlier truncated reference. A pinned 154-question stratified LoCoMo CI sample compares the same corrected harness against the pinned product source. No retrieval API or default changed.
- **Corrected evidence scoreboard, not answer accuracy.** Full keyword-only LoCoMo (1,540), LongMemEval-S (500) and BEAM 100K (400) now include ranking metrics and independent budget latency. At 1,500 / 4,000 estimated tokens, evidence is 78.69% / 86.31%, 79.60% / 85.92%, and 70.87% / 80.67%. LoCoMo previously reported 78.98% / 86.61% after silently excluding unresolved annotations; retaining those misses corrects the denominator. LongMemEval's complete second-budget recall corrects 85.85% to 85.92%; BEAM evidence is unchanged. `--limit 500` now evaluates 500 rather than 400 cases. Default `auto/auto` was verified only in lexical fallback; no model calls or official judged accuracy are claimed.
- **Benchmark judge compatibility profiles.** LongMemEval task prompts match upstream; LoCoMo's legacy downstream binary profile differs from the primary evaluator and current competitor judge. BEAM's compatibility profile still needs official semantic alignment and aggregate scoring. Parser tests with supplied responses are not measured agreement of live judges. Existing response cache and preflight cost estimates need endpoint/settings binding and multi-call/full-history accounting before a paid evaluation.
- **Whole-history episodic chaining and multi-session round-robin interleaving (`Store.timeline()`, `assemble()`).** `Store.timeline()` generates a chronological sequence of conversation sessions with turn bounds and narrative anchor timestamps. `assemble()` performs round-robin session interleaving across top candidates (`session_to_tids` with user-turn priority), preventing single-session budget starvation and lifting BEAM summarization evidence recall from 14.1% to 27.9% and multi-session reasoning from 73.7% to 83.7%.
- **Temporal grounding and multi-month range window resolution (`timeparse.py`, `search.py`).** Extended `question_window` parses multi-month and multi-year ranges ("between May 2023 and August 2023", "from 2021 to 2023"). Date-sensitive retrieval prioritizes source turns with grounded absolute dates. These are retrieval features; no model-judged temporal accuracy has been verified.
- **Compound query decomposition and entity seek (`search.py`, `store.py`).** Deep query frame stripping (`_QUERY_FRAME`), coordinate noun phrase splitting (`A and B`, `A or B`), and standalone aspect extraction from multi-part queries. `Store.entity_turns()` links turns directly to mentioned speakers and entities via SQL union, resolving low-overhead lexical matches without model calls.
- **Calibrated abstention detection and confidence scoring (`confidence()`, `answer.py`).** Salient non-attribute keywords are checked across candidates; when unanswerable, confidence reports 0.0 with `explain["abstain"] = True`, directing the answering model to state the absence of information.

- **Conversation memory, by ability.** Standing instructions to the assistant are recognised as they are written and pinned into every recall; preferences are matched by stem and surfaced for any request for help; over-long turns are excerpted to the passages that bear on the question; list-style questions search each aspect; self-introductions ("I'm Craig, a colour technologist") are identity facts; questions about a whole topic (summaries, order of events, across sessions) are answered breadth-first from what the user raised; the best hits are placed before their neighbours; "current/latest" questions favour later statements; neighbours are only pulled when said close in time; recall reports `explain.confidence` and the cross-encoder's `rerank_top`; the answer prompt covers instructions, preferences, updates, contradictions, abstention, dates and order. Keyword-only evidence recall at 1,500 / 4,000 tokens: BEAM (100K, 400 probing questions, ten abilities) 44.9% / 50.2% → 62.1% / 70.4%, and 67.4% / 73.3% with `minilm` and the cross-encoder (instruction following 24% → 85%, preference following 33% → 67%, event ordering 15% → 34%, multi-session 44% → 55%, summarization 6% → 18%); the cross-encoder now reads the question-focused excerpt of a long turn, halving recall time on long chats; LongMemEval (120 questions) 70.7% / 80.6% → 77.4% / 84.4%; LoCoMo (1,540) 78.7% / 86.0% → 79.6% / 86.5%.
- **BEAM and DolphinBench in the conversation benchmark** (`--dataset beam|dolphin`), and a DolphinBench harness (`benchmarks/dolphinbench/commontrace_harness.py`) whose agent remembers through CommonTrace: ingestion writes each message with no model work on the memory side, `freeze` seals and hashes the store, tests read it read-only with a `search_memory` tool, and costs come from a durable usage ledger.
- **Frozen stores.** `Store(root, space, read_only=True)` recalls but cannot write.
- **The cross-encoder votes beside the fused rank instead of replacing it** (`Options.rerank_blend`, default 1.0). On DolphinBench task requests it had cost evidence (48.0% at 1,500 tokens against 51.2% keyword-only); blended it gives 54.1% / 71.3% / 77.7% at 1,500 / 4,000 / 7,000 tokens, LoCoMo with `minilm` rises from 79.8% to 80.6% at 1,500 tokens, and BEAM is unchanged.
- **A declarative ontology and a bi-temporal knowledge graph.** `memory/ontology.yaml` (or JSON, or RDF/OWL with `rdflib`) declares entity types with parents, relations with domain, range, inverse and exclusivity, and entity aliases; `strict: true` refuses anything undeclared (`graph ontology show|init|check`). Exclusive relations resolve contradictions by the latest valid time, keeping late-arriving older facts as history; edges record when the store learned of each change, so `graph query --known-at` reads past beliefs beside `--as-of`; `graph between` answers interval queries and `graph timeline` lists an entity's changes with reasons. MCP `graph_timeline`, `graph_query(known_at)`. Graph schemas accept any snake_case type or relation.
- **Entity extraction and linking (`graph extract|link|entities|duplicates|merge`).** Patterns for services, tools, errors, files, symbols, environment variables, people and organisations, plus spaCy NER when installed; one canonical node per entity through ontology and built-in aliases; lessons linked to the entities they name so queries naming an entity boost those lessons; duplicate detection and history-preserving merge.
- **Document ingestion pipeline (`ingest --type docs`).** Loader, transforms and submitter as separate stages: chunking, prompt-injection screening, deduplication, cached context headers (heuristic or model-written), alias canonicalisation and a size guard; facts attributed to their file, or a conversation space with `--space`; a ledger that skips unchanged files, with sampled fingerprints for files over 256 MB that are hashed in full only on a size collision.
- **Multi-channel recall (`commontrace recall`).** Lessons, facts, graph relations and conversation spaces fused by weighted reciprocal rank, de-duplicated across channels, and packed into one token budget with per-channel floors and sentence-boundary truncation; one `--as-of` applies to every channel; per-agent budgets and weights in `memory/budgets.json`; MCP `memory_recall`.
- **Versioned memory (`commontrace memory`).** A store as its own git repository with a validating pre-commit hook (limits in `memory/memfs.json`, parseable JSONL and frontmatter, no conflict markers, no credentials), `status/log/diff/commit/restore`, merge-conflict repair (union of append-only JSONL, the other side parked for anything else), and HMAC-signed handoff tokens that name a commit and a digest of its files, with audience, expiry and drift checks.
- **Durable job queue (`commontrace jobs`).** Ingestion, extraction, summaries, index rebuilds, entity linking and commits queued in SQLite with leases, exponential-backoff retries, dead-lettering, dedupe keys and priorities; the daemon processes jobs on each pass; a batch helper falls back to one call per item when a batch call fails.
- **Observability.** CLI commands, MCP tools, gateway requests, recall, retrieval, ingestion and jobs are timed into in-process counters and latency histograms, served by the gateway at `/v1/metrics` (Prometheus text or JSON) with an `X-Request-Id` on every response; OpenTelemetry spans over OTLP when enabled; JSON logs with request context and credential redaction (`COMMONTRACE_LOG_FORMAT=json`); `doctor` reports the telemetry state.
- **Conversation memory, continued.** Newer single-valued statements (job, home, favourites) replace older ones (`profile --history`); session summaries, extractive or model-written (`summarize`); model-distilled dated memories (`extract`); model answers with multi-round follow-up search (`answer --rounds`); recall filtered by session, speaker and date; per-message `expires` and `forget --expired|--before`; `export`/`import` as JSONL; an entity index that lifts turns naming what a question names; `promote` into atomic facts; MCP `conversation_profile`, `conversation_summarize` and `conversation_forget`. Stores written by the first version are upgraded in place.
- **Conversation memory (`commontrace conversation`, MCP `conversation_add` / `conversation_recall`, gateway `/v1/conversation/*`).** Messages are stored per space in SQLite with their relative dates resolved as they are written, and recalled into a dated, token-budgeted context: keyword (FTS5 BM25) and semantic search fused, reranked by the cross-encoder, lifted by a time window the question names, with neighbouring turns and the user's self-descriptions for advice questions. No model runs at write time. On LoCoMo it puts 84.3% of the answering evidence in a 1,500-token context and 95.7% in 7,000 tokens (`benchmarks/conversation_bench.py`).
- **Working memory, facts and a knowledge graph beside lessons.** `commontrace block` (bounded blocks with an audited revision chain), `commontrace fact` (atomic facts with confidence, scopes, a validity window, supersede, delete and reversible forget, `--as-of` views) and `commontrace graph` (typed nodes, dated edges, multi-hop queries, version chains, provenance, Mermaid/JSON/HTML views). Graph proximity boosts lesson retrieval (`retrieval --graph-weight`). All of it is exposed over MCP, written atomically under a lock, and screened for credentials and prompt injection.
- **Ingestion and connectors.** `commontrace ingest` maps code into the graph and turns docs, documents (PDF including compressed streams, DOCX, HTML, image/audio/subtitle metadata), logs and agent transcripts into facts and traces; `sync --connector local_dir|web_crawler` re-syncs changed sources and retires the facts they no longer state. Ingestion never writes lessons directly.
- **Agent loop (`commontrace agent run`)** against the configured model, with relevant lessons, memory and project skills in context (MCP `list_skills` / `load_skill`); runs are recorded as traces.
- **Lexical scorers `adaptive-v1` (new default for new stores) and `bm25-v1`**, CJK-aware tokenization, a persisted corpus index, and TTL (`expires`) on lessons and facts.
- **Consolidation:** `commontrace dream` links traces into the graph and writes `memory/profile.md`; `daemon --once` and `watch --once` for cron; `init --git` versions a store and records fact forget/restore as commits.
- **Hub:** traces carry `scopes` and a validity window (validated), searchable by `scope` and `as_of` over MCP and REST; `sync` forwards a lesson's scopes.
- **Scoped, bi-temporal local lessons.** Optional project/team `scopes` and inclusive `valid_from` / exclusive `valid_until` fields flow through the schema, CLI and MCP retrieval; `query --as-of` can reproduce the valid-time view while the revision journal preserves recorded-time history.
- **A redesigned customer console.** One shell for every `/app` page: grouped sidebar navigation with the organisation and plan, a command palette (Ctrl/⌘ K) and `g` + letter shortcuts, and a light, dark or system theme remembered per browser.
- **The warm worker runs `commontrace query` whole.** It already held the models; the CLI process still imported the package, parsed the lesson cache and built the lexical index on every call.
- **`commontrace query` with the attention extra in about 0.55 s, from 7-8.5 s** (default fusion and reranking, 1000 lessons, 4-core CPU; semantic alone 0.25 s from 6.5 s).
- **An index rebuild reads each lesson once.** `build_index.py` parsed every lesson's frontmatter for its staleness check and again for the build; the build now reuses the first pass.
- **`commontrace gate`: fail a build on memory that should not ship.** Exit 1 on a COMPROMISED experiment, an active lesson measured to hurt (a warning if harm withdrawal already keeps it out), an active lesson tripping the content screen, or unedited scaffolding; contradictions and no-verdicts-yet…
- **Revoke every Proof share link at once.** Links carry the org's share generation; an admin's "Revoke all share links" (audited) ends every earlier link.
- **External memories are screened for prompt injection.** `MeasuredMemory` (third-party memory stores) quarantines a memory that trips the injection screen before arm assignment, and reports it in `Recall.quarantined` by pattern name.
- **Image vulnerability scan in CI.** The built Hub image is scanned with Trivy (pinned by digest): a fixable CRITICAL fails the job, HIGH is reported.
- **Billing on proven value (`commontrace bill`).** An invoice is built from a price schedule the owner supplies (every term required, no defaults) and proof packages that verify.
- **The Hub as a Helm chart and Terraform** (Postgres on AWS and GCP: customer-managed keys, TLS, high availability, point-in-time recovery), with SLO burn-rate alerts over the metrics the Hub exports.
- **A conformance suite (`commontrace conformance`)** and PROTOCOL.md section 13: vectors for assignment, the value ledger, the raw-assignments digest and a lesson revision, runnable against any language over stdio, plus checks of a store and a gateway.
- **The lesson workbench in the console** (review queue, gate results, edit, approve, reject), a shareable proof page, `commontrace proof wizard` (with a rehearsal on a simulated fleet), a scheduled `commontrace dream` pass, `distill --failed` / `--signal`, Bedrock and…
- **Fixed:** a model draft left `TODO` in two sections, so `lesson approve` refused every drafted lesson.
- **A door for any agent, including robots (`commontrace gateway`) and a local console.** Plain HTTP + JSON or stdio JSON lines, memory-agnostic, token-authenticated, with protected (safety) memories that are never withheld, one environment per store so simulation is never pooled with reality, relaxed durability for…
- **The Agent Learning Proof (`commontrace proof`).** `start` forecasts (and refuses a run that cannot answer in time), starts the holdout and registers the design before any data; `status` reports progress, validity and each memory's verdict; `report` writes a package (`report.md`…
- **Automatic harm withdrawal for memory held anywhere.** `CausalMemory`, `MeasuredMemory` (third-party memory stores) and file sources now follow the store's harm policy: with `commontrace retrieval --on-harm withdraw`, a memory whose anytime-valid verdict is…
- **Function kits.** `commontrace function` and `init --function|--kit`: the occasion, outcome model (real detectors, how they combine, how long to wait), planning defaults and demo data that make the holdout work for a function, as a validated spec.
- **Outcome connectors (Hub).** `POST /connectors/{id}/events` turns a system of record's signed webhooks into recorded outcomes: Zendesk (solved, reopened, bad CSAT) and GitHub (merged, closed unmerged, reverted).

### Changed

- **Lexical retrieval now defaults to `adaptive-v1`.** Stemmed, length-normalized IDF relevance uses a query-relative tail gate: the cross-field fixture keeps 100% recall@3 and precision@1 while reducing worst-field pollution from 2.33× to 1.06×. Historical scorers remain selectable and experiment logs pin old treatments.
- **The verified-key cache is safe across replicas.** Revoking or rotating a key, deleting an org or changing its region sends a transactional Postgres NOTIFY; every replica's listener clears its cache on commit, and a replica caches only while that listener is connected.
- **The shared rate limiter decides in one statement.** `HUB_RATE_LIMIT_BACKEND=postgres` refilled and decremented in two statements inside a transaction (four round trips, row lock held across them) for each of the two limiter checks on every authenticated request.
- Recording an outcome no longer re-parses the whole outcomes log (O(n^2) over a run: 2.7 s for 800 occasions, and every report for a fleet with 100k).
- A bare `commontrace init`, a store with no declared type, and Hub traces with no `agent_type` are `general`, not `code`.

### Fixed

- **Conversation recall no longer lets a boost outvote the answering turn.** Measured keyword-only at 1,500 / 4,000 tokens against this branch before the fix (fresh ingestion, with the instruction fix below): LongMemEval (120) 72.1% / 80.0% → 78.0% / 82.8% under the same adapter, BEAM 100K 62.9% / 72.3% → 67.5% / 73.7%, LoCoMo 77.9% / 86.0% → 79.0% / 86.6%. "Current/latest" questions reorder only the top `Options.recency_pool` matches (default 4) by time, instead of every scored turn; the date boost for "when" questions is proportional to a turn's own score instead of a flat 40% of the best one; and question words are no longer treated as entities because an entity of the same name exists (in one LongMemEval history "use" had 42 entity mentions).
- **Question shapes are read more carefully.** Date-gap questions ("how many days passed between A and B", "how many days after A did B", "how long had I been A when B") search each event on its own and gather breadth-first; counts and aggregates ("how many times", "in total", "altogether") are whole-history questions, but durations of a habit ("how many hours do I sleep") are not; questions that point back to one earlier answer ("remind me", "you said", "our previous chat") are not; "X or Y" choices still split, "X and Y" subjects no longer do; the "when" boost is for dates, not durations ("how much time do I spend").
- **Filtered keyword search is about 15x faster.** A filter that excludes nothing (e.g. `now` after every turn) is dropped instead of being sent with every full-text query, and a real filter over-fetches the ranking and keeps eligible units, falling back to the exact filtered query only when too few survive (same results, checked by test). A cold LongMemEval recall fell from 1,161 ms to 52–91 ms; the benchmark's mean recall time from 606 ms to 78 ms.
- **Multi-session questions search each named aspect and count what the user raised.** "Considering A, B, C and D, how …" searches each aspect on its own, and "how many different X did I mention" lists the user's own turns like an ordering question. BEAM multi-session reasoning 60.2% / 71.7% → 66.1% / 78.4% (BEAM overall 70.9% / 80.7%); LongMemEval and LoCoMo unchanged.
- **Summary questions read each ask together with its answer.** Every hit of a summary question now brings its reply (not only the first 5–10), and each passage is cut to `budget // 60` tokens (at least 40) so more exchanges fit. BEAM summarization 25.4% / 42.1% → 28.5% / 49.7% after pairing (21.2% / 37.5% before either change); BEAM overall 70.2% / 79.9%; LongMemEval and LoCoMo unchanged. Answer quality from the shorter passages is not yet measured.
- **"In what order did I bring up X" lists every turn the user raised.** The answer is the user's own turns in order, and similarity ranking alone missed the later aspects; ordering questions about what the user brought up now take every user turn (after the ranked ones), each cut to its passage nearest the question so the sequence fits. BEAM event ordering 42% / 50% → 60% / 96% at 1,500 / 4,000 tokens (BEAM overall 67.5% / 73.7% → 69.5% / 78.7%); LongMemEval and LoCoMo unchanged.
- **BM25 fact search scores only the query's terms.** Each candidate fact looked its counts up by decoding every stored term, and each term's IDF was recomputed per fact; a direct lookup and one IDF per query term keep scores bit-identical (same result hashes in `benchmarks/fact_search.py`) and cut warm search at 20,000 facts from 375 ms to 231 ms.
- **A user's own plans are no longer standing instructions.** "I'll make sure to prune the basil" and "I need to remember to call my mom" were pinned into every recall as rules for the assistant; a self-commitment (I'll, I will, I need to, I'm going to …) before the directive now rules it out. BEAM instruction following 85% → 90% and preference following 68% → 73% at 1,500 tokens.
- **LongMemEval's adapter asks at the end of the question's day.** 43 of its 500 questions carry a clock time earlier than an evidence session on the same day, and recall's point-in-time cutoff hid that history.
- **Timestamps keep the speaker's wall-clock time.** A UTC offset was converted to UTC, moving evening messages onto the next day and skewing "this morning" / "yesterday" grounding.
- **Long pastes are stored and split instead of refused** (the per-message limit is now 1,000,000 characters).
- **Frontmatter parses on libyaml where it is installed.** The strict loader subclassed PyYAML's pure-Python SafeLoader, so even installs whose PyYAML ships the C parser (the wheels for every mainstream platform) parsed every lesson in Python: 1.20 ms per lesson against 0.16 ms on…
- **Unchanged lessons are not re-parsed, and the semantic arm's slugs are de-duplicated in linear time.** A long-lived process (MCP server, warm worker) re-read the same lessons' YAML on every retrieval, about 5 ms each with PyYAML's pure-Python loader; `frontmatter.read` now memoises the parse by the exact frontmatter text (a copy…
- **One directory pass per retrieval.** A retrieval listed the lessons directory up to five times (the index staleness check before and after a refresh, the lesson cache, and the semantic arm's two newer-than-the-index checks), each a stat of every lesson.
- **No progress bars from the MCP server.** The semantic arm's query encoding drew a sentence-transformers progress bar onto stderr on every `retrieve`, because the server's logging runs at INFO.
- **Semantic-only retrieval honours the injection budget.** Its script appends every active lesson at or above the importance floor to the top-k, and this path, unlike the lexical and fused ones, never applied the store's budget or admitted core lessons: on a 10,000-lesson store one query…
- **A semantic-only experiment log pinned the wrong settings.** Semantic-only assignments carry no floor, so the pinning read them as pre-upgrade rows: an unconfigured store was pinned to the historical `count-v1` scorer, and an index rebuilt from nothing took the default embedder instead of…
- **Brute-force limits multiplied by replica count.** Console sign-in, signup, share-link views, the operator console, connector/OTLP/REST auth and `/readyz` built process-local limiters, so under `HUB_RATE_LIMIT_BACKEND=postgres` each replica kept its own budget.
- **SSO lockout after an IdP key rotation.** A token with a newly rotated `kid` failed until the JWKS cache expired (up to an hour).
- **Rate limits bypassable through `X-Forwarded-For`.** Only the header's first line was read, and a proxy that writes `IP:port` gave every connection a fresh bucket.
- **A link could make either console announce any message.** After an action, both consoles showed the `?done=` text from the URL as their confirmation banner, so `/admin/kb?done=Call+…` or `/app/kb?done=…` put arbitrary words in the console's voice (escaped, but trusted-looking).
- **A freshly minted share link went into browser history, and the console would present any link as its own.** Generating a link redirected to `/app/proof?share_url=<link>`, so the live credential landed in history and sync; and a signed-in user sent `/app/proof?share_url=https://anything` saw that URL under "Shareable link generated".
- **The Hub's logs held share-link tokens and customer search text.** The request log recorded `/app/proof/shared/<token>` verbatim -- the token is the credential for that org's live report for 14 days -- and uvicorn's own access log, which reached the same JSON handler, added every query string…
- **An unauthenticated request with a huge body could exhaust the Hub's memory.** `HUB_MAX_REQUEST_BODY_BYTES` (1 MiB) was enforced only by the MCP transport; the signup and sign-in forms, the REST API, SCIM and the Stripe webhook buffered bodies of any size, several before authenticating.
- **One misclick revoked a production API key.** Revoke and rotate (keys and webhook secrets), disabling a webhook, deleting an alert rule, purging traces and applying retention acted on the first click, with no way back.
- **Every CLI command paid for every other command's imports** -- numpy, asyncio and ssl among them, about 140ms.
- **The Hub image and compose stack are built from base images pinned by digest** (`python:3.12-slim@sha256:…`, `postgres:16@sha256:…`), so a re-pushed tag cannot change what is built.
- **Hub HTML pages now send `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin` and a Permissions-Policy that turns off camera, microphone, location, USB and payment APIs.** A window another site opened no longer keeps a handle to the console, no other origin can load a console page as a subresource, and a script that got past the CSP gets none of those device APIs.
- **Rate limits keyed on a client's address did nothing against an IPv6 client.** One host is typically assigned a whole /64 and can send from any address in it, so each request could land in a fresh bucket: the console's five-attempt sign-in limit stopped no one from guessing API keys, and the same was true…
- **A webhook could target Alibaba Cloud's instance metadata service** (100.100.100.200).
- **The Hub URL check let the cloud-metadata address through in other spellings.** `https://2852039166/`, `https://0xa9fea9fe/`, `https://169.254.169.254./` and `https://[::ffff:169.254.169.254]/` all reach 169.254.169.254, but the Python client only recognised the dotted form and the TypeScript SDK missed the…
- **Any site could sign a user out of the console** with an `<img>` tag pointing at `/app/signout`, which acted on a GET.
- **A page on a sibling subdomain could post console forms as the signed-in user.** The session cookie is `SameSite=Strict`, but a sibling subdomain is the same site, so the browser still attached it.
- **Hub console and admin pages were unreadable in dark mode.** Every primary button drew white text on a near-white background (contrast 1.2:1), and the red/amber/green verdict boxes and pills kept light-mode colours under light text.
- **Every console page scrolled sideways on a phone.** The nav bar did not wrap (665px of content on a 390px screen), and wide tables and the memory search box overflowed.
- **`commontrace query` on a store with no lessons took 12–19 seconds** to return nothing: it loaded the cross-encoder and rebuilt the semantic index first.
- **The console session cookie lost `Secure` behind a TLS proxy in another container or an ingress** , because the Hub only sees `https` from a proxy on 127.0.0.1.
- **A console left open in a background tab kept reloading** every 15-45 seconds, re-running its queries (up to 1.4s of statistics on the overview) for as long as it stayed open.
- The CLI's HTML reports (`taxonomy`, `impact`, `pilot`, `bench --html`) had no viewport tag, so phones rendered them zoomed out; they now fit the screen, scroll wide tables, and follow the system dark mode.
- **An agent's first `retrieve` waited ~9s for models to load.** The MCP server now loads the cross-encoder and embedder its store will use on a background thread as it starts (and refreshes a stale index), so the first retrieval took 0.28s instead of 8.7s in an end-to-end stdio session.
- **Every `commontrace query` on a populated store took ~15.5s; now ~6s.** Two causes, same results either way.
- Every query printed two "Loading weights" progress bars.

### Security

- **Credentials in captured or imported traces were stored verbatim.** A key that leaked into a log line (AWS, GitHub, Slack, Stripe, Google or Anthropic keys, PEM private keys, JWTs) went into the local trace file -- its filename too, when it was in the title -- where every later agent could read…
- **`commontrace redact`** cleans a store that predates that: it rewrites each affected trace through the atomic writer, renames a file whose name was built from a key, and `--dry-run` previews it.
- **Every Hub HTML page sends a Content-Security-Policy** that allows only its own inline scripts, by SHA-256 hash: no inline event handlers, no plugins, no framing, and forms may post only to the Hub or to Stripe.
- **The TypeScript SDK sent the API key to any URL** , including plaintext `http://` to a remote host and cloud-metadata addresses.
- The Hub image's code is now owned by root and only readable by the `hub` user that serves the network, so that process cannot rewrite its own code even where the filesystem is not mounted read-only.
- **Optional bearer token for `/metrics` (`HUB_METRICS_TOKEN`).** Unset, nothing changes; set, a scrape without that token gets a 401 (compared in constant time), for deployments that cannot keep `/metrics` off a reachable network.

### Accessibility

- **Live pages can be paused.** The console overview and the operator pages reload themselves every 15-45 seconds; a "Pause live updates" button in the header now stops that (WCAG 2.2.1), and the choice is kept across pages in that browser.
- The console nav marks the current page (`aria-current`), pages have a skip-to-content link, every focusable control shows a focus ring, and table columns of row buttons have a header a screen reader can announce.

### Added

- **`commontrace lesson list --json`** prints one JSON array (name, agent_type, importance, status, description, domain, path) for scripts, instead of the aligned table; `--status` and `--agent-type` filter it the same way.
- **`commontrace` suggests the command you meant** (`unknown command 'captur'.
- **A Copy button beside every shown-once secret in the Hub** -- a new API key (signup and console), a webhook signing secret, a share link.
- **`commontrace doctor` says whether the store's models are cached.** For each model the store's retrieval loads (the index's embedder when fusion is on, the reranker when reranking is on) it reports `[OK]` when the local cache holds it, or explains that the first query will download it and how to…
- **The accurate reranker is the default, over a shallower pool.** With the arctic-embed semantic arm, the answer is almost always near the top of one arm or the other, so gated fusion now hands the reranker each arm's top 10 (accurate model) or top 15 (fast model) instead of 30…
- **A stronger semantic arm: new indexes embed with `Snowflake/snowflake-arctic-embed-m-v1.5`.** Same size and width as the original `multi-qa-mpnet-base-dot-v1` (109M parameters, 768 dims), and it finds more: on LoCoMo its exact cosine search puts an answering turn in the top 10 for 70.6% of questions, against 56.1% for…
- **Gated fusion (`commontrace retrieval --fusion gated`), the new default where the attention extra is installed.** Both arms feed the reranker, but a lesson that did not clear the lexical relevance floor reaches the page only when the cross-encoder scores it at least -4 (`rerank_arm.GATE_THRESHOLDS`).
- **Lambda's verdicts are kept, and measured.** Episodes now record `lambda_decisions`: Lambda's verdict on every Omega proposal, rejected and sent-back ones included.
- **Cross-encoder reranking, opt-in** (`commontrace retrieval --rerank cross-encoder|cross-encoder-fast`, `commontrace/rerank_arm.py`).
- **The semantic index keeps itself current.** A lesson approved or edited after the last `commontrace index` used to make the index stale.
- **Agents now get fused retrieval.** When a store has set `commontrace retrieval --fusion rrf`, the MCP `retrieve` tool ranks by keyword and meaning together, as `commontrace query` does.
- **`idf-v3`, an opt-in stemmed lexical scorer.** It is `idf-v2` with every term Porter-stemmed, so "resets", "reset" and "resetting" are one term.
- **A lesson proven to make outcomes worse can now be withdrawn automatically.** `commontrace retrieval --on-harm withdraw` stops `query` and the MCP `retrieve` tool from injecting any lesson whose verdict is HURTS.
- **Search results now carry each lesson's measured causal evidence.** Once an org has holdout data, every `search_traces` result includes `evidence`: its `verdict` (HELPS, HURTS, NO_MEASURABLE_EFFECT, UNDERPOWERED or NOT_MEASURED), `effect`, `ci_95` and arm sizes.
- **Fixed before release: the evidence cache recomputed on every search during an experiment.** Its first key counted holdout assignments, and a search with an `occasion_id` writes one, so an org running an experiment rebuilt the whole analysis on every search.
- **Knowledge Base queries no longer re-read the corpus on every call.** `commons_search` at 20,000 entries went from ~1.6s to ~29ms, and a 50-failure `commons_overlap` from ~1.9s to ~210ms (A/B on identical data; the speedup grows with the corpus).
- **`commontrace.measure.CausalMemory`** — wraps any retrieval callable so memories held by another system (a vector database, a memory SDK, a LangGraph store) get the same per-memory causal verdict from `commontrace experiment` as this store's own lessons, without…
- **`holdout_io.record_outcome`** — report a task outcome by occasion id alone, for applications with no episode or trace file to carry it.

### Changed

- **Fast reranking is on by default where the attention extra is installed.** A store that has not chosen a reranker and has no experiment history reranks the lexical arm's floor-cleared candidates with `cross-encoder-fast` (~30 ms).
- **Local retrieval is 6.9× faster at 6,400 lessons (~300ms → ~44ms), with identical rankings.** Measured with `commontrace/reference/measure_local_latency.py`.
- **`commontrace experiment`, its `--strict` gate and `commontrace pilot` now read a running experiment the way the MCP tools and the Hub do.** They tested against a fixed 5% threshold, so one store could fail its CI gate on a HURTS that `retrieve` reported as UNDERPOWERED, and the pilot report could disagree with both.
- **Trimmed `hub/server.py`'s `search_traces` MCP tool docstring** — every registered tool's docstring is sent to every connected agent's system context on every session, so a near-verbatim restatement of the holdout mechanic (occasion_id, `withhold`, `pinned`, why using a withheld trace…

### Fixed

- **Every fused experiment was declared invalid.** The marginal-eligibility check compared each assignment's relevance with the retrieval floor.
- **A perfect split could never be declared.** The anytime-valid interval returned "no claim" (−100% to +100%) whenever neither arm had seen both outcomes, however many occasions had accrued.
- **The local evidence surfaces now read a running experiment the way the Hub does.** The evidence on `retrieve` results and `experiment_status` used a fixed 5% threshold.
- **A torn line in the holdout or outcome log no longer takes the next record with it.** A writer that died mid-line left the file without a trailing newline, so the next append was glued onto the fragment and the reader dropped both.
- **The Knowledge Base catalogue (`browse_commons`) is no longer ordered by query traffic** , which any caller can inflate.
- **`hub/console.py`'s `alerts_create` could 500 on a malformed request instead of returning its intended validation error.** `form.get(...)` can return an `UploadFile` when a field arrives as a multipart file part rather than plain text; `float()`/`int()` raise `TypeError` on that, not `ValueError`, which the handler's `except ValueError` alone did not…
- **`hub/config.py`'s `HubConfig.extra` field was dead code** — declared, never populated by `from_env()`, never read anywhere.

### Fixed

- **A doctor test went red on the most complete install possible.** `test_info_conditions_do_not_fail_the_run` asserted that `commontrace doctor` prints at least one `[INFO]` line — but every INFO branch in `doctor_cmd` reports an *optional* extra being **absent**, so on a machine with all extras…

### Added

- **`commons fetch` + `commons_export`: hold the corpus, and stop asking.** Every other Knowledge Base call describes a failure — as a MinHash signature, but the Hub still learns that a fleet is asking and roughly about what.
- **`commons report --corpus`: a coverage number computed with no Hub at all.** The Knowledge Base is operator-curated public content and a fleet's failure text is already on its own machine, so both halves of the comparison can be local.
- **`commons report --corpus --semantic`: embedding similarity instead of word overlap.** Measured on the held-out probe set, 32.6% recall at a 0% false-positive bar against the lexical matcher's 8.7% (`commons/eval/semantic.py`).
- **`commons report --candidates`: the report no longer reads as an empty Knowledge Base.** The evaluation recorded this as *"the single highest-value thing to fix in this codebase"*: a prospect's nine-failure export, seven of which the corpus provably contained, came back **"0 of 9. 0%."** The number was…
- **A corrected Knowledge Base entry now says it was corrected.** Amendment resets an entry's votes (they judged text that no longer exists), which left a freshly corrected entry indistinguishable from one nobody has ever tried: both `unproven`, both zero votes.
- **Correcting a Knowledge Base entry no longer deletes it.** `commons_visible()` excludes superseded rows and `amend_trace` INSERTs a new row rather than mutating the original, so amending a Knowledge Base entry removed it from the Knowledge Base: the original went invisible the moment it…
- **The Knowledge Base catalogue now shows the grounds for a verdict, not just the verdict.** `standing` says the field rejected an entry; it does not say whether the entry is stale, wrong, or dangerous, which are three very different decisions for someone about to apply the fix.
- **Sockpuppet resistance for Knowledge Base voting** — the open repository now follows the wiki model all the way down, not just in who may contribute.
- **Per-org opt-in to contributing back** (`Organization .commons_auto_contribute`, migration `a1c7e4f93d2b`).
- **Customers can vote on Knowledge Base entries from the console** (`hub/console.py`).
- **The Knowledge Base is now browsable and contributable from the customer console** (`crud.browse_commons`, `hub/console.py`).
- **A `/api/v1/*` REST surface, so the CommonTrace Claude Code plugin can talk to this Hub** (`hub/rest.py`, opt-in via `HUB_REST_API_ENABLED`).
- **Amending a trace from the `/admin` console, plus a new `hub.manage amend-trace` CLI command** (`hub/manage.py:amend_trace`, `hub/admin.py`): the operator counterpart to the `amend_trace` MCP tool an org's own agents already use — for a support-ticket-driven correction where the org itself cannot or has not amended its own…
- **A double-submit guard on every form in both consoles** (`hub/admin.py`, `hub/console.py`): every mutation here is POST-then-redirect, so a double click or an impatient second click while the first request is still in flight could fire the same mutation twice before either response…
- **An in-process webhook-delivery scheduler** (`hub/scheduler.py`'s `run_webhook_delivery`, opt-in via `HUB_WEBHOOK_SCHEDULER_ENABLED`): the same pattern the existing alert scheduler already used for `hub.manage check-alerts`, now applied to `hub.manage webhook-deliver` — a…
- **Auto-refreshing pages in both consoles.** The customer console's Overview and audit log pages, and the operator console's Overview, organization, and knowledge-base pages, now reload themselves every 15–45 seconds (vanilla JS, no new dependency) so a page left open in a…
- **Key issuance and a confirmed danger zone in the `/admin` console** (`hub/admin.py`): issue/rotate/revoke-key buttons on an organization's own page (needed for onboarding — a brand-new org has no key yet, so it cannot sign into its own console to issue one itself), plus a "danger zone" exposing…
- **User management, SSO linking, and subject-rights lookup in the `/admin` console** (`hub/admin.py`): an organization's own page now lists its individual `User` rows (distinct from its shared API key) with buttons to create a user, change a role, disable/enable, and link/unlink an SSO identity, plus a "find…
- **Organization creation and plan changes in the `/admin` console** (`hub/admin.py`): a "Create organization" form on the Overview page and a "Change plan" form on each organization's own page, previously `hub.manage create-org`/`set-plan` only.
- **Reversible operator actions in the `/admin` console** (`hub/admin.py`): releasing a quarantined trace, placing and releasing a legal hold, and setting and clearing a retention policy are now buttons on an organization's own page, previously reachable only via `hub.manage…
- **Self-service webhook management, an audit log page, and randomized holdout control in the customer console** (`hub/console.py`).
- **A Stripe webhook event-id idempotency ledger** (`processed_webhook_events`, migration `37d2580be8db`).
- **Pluggable secrets provider** (`hub/secrets_provider.py`): every genuinely secret `HUB_*` setting (`HUB_DATABASE_URL`, `HUB_API_KEY_PEPPER`, `HUB_ADMIN_TOKEN`, `HUB_CONSOLE_SECRET`, the Stripe keys, `HUB_LEDGER_SIGNING_KEY`, `HUB_ENCRYPTION_KEY`/`_PREVIOUS`)…
- **Encryption at rest for `WebhookEndpoint.url`** (`hub/encryption.py`), opt-in via `HUB_ENCRYPTION_KEY` (AES-256-GCM, with `HUB_ENCRYPTION_KEY_PREVIOUS` for rotation) — `python -m hub.manage generate-encryption-key` prints a fresh one.
- **Kubernetes deployment manifests** (`deploy/k8s/`) for operators whose platform is Kubernetes rather than a single Docker host: ConfigMap, Secret shape (a template, not real values), a one-shot migration Job mirroring `docker-compose.yml`'s `migrate` service, a…
- **`commontrace export`** : the missing counterpart to `commontrace/import_data.py`'s bulk importer.
- **Near-duplicate lesson detection (`commontrace/redundancy.py`) and its three consumers** : near-duplicates are caught on write and kept out of the same result set.
- **Reliability- and recency-weighted retrieval ranking** , closing the standing gap that `commontrace/retrieval.py`'s `rank_lessons` never consulted `commontrace/reliability.py`'s HELPS/HURTS verdicts or a lesson's `last_hit` freshness — a lesson flagged HARMFUL ranked exactly like…
- **`retrieval.apply_reranker`** : a pluggable second-stage scorer seam.
- **`commontrace lesson suggest-revision <slug>`** : for a MISCALIBRATED lesson (fires often, rarely helps), drafts a new `status: review` lesson from its own retrieval evidence — which occasions it fired on, split into "fired and helped" vs "fired but did not help", with a short…
- **`commontrace query --exclude-shown`/MCP `retrieve(exclude_shown=...)`** : a session/occasion-scoped retrieval filter, so `occasion_id` is usable as a retrieval filter as well as for holdout assignment.
- **Point-in-time lesson reconstruction** : `commontrace/lesson_io.py`'s revision journal recorded a before/after HASH on every content change — enough to detect a lesson changed mid-experiment (`integrity.check_treatment_stability`), never enough to answer what it…
- **`commontrace reliability` reports uncaptured retrievals** , closing a narrower and safer version of the "continuous capture" gap than the first design attempted.
- **Two more fields in the cross-field retrieval corpus** — `commontrace/fixtures/fields/{clinical,finance}.json`, taking the gate from six fields to eight (48 lessons, 144 labelled queries).

### Fixed

- **A stale scorer claim in `commontrace/reference/measure_retrieval.py`.** Its module docstring said the current scorer pollutes at 1.00×–1.28×, which is the superseded single-corpus tuning at `floor=0.10`, not the shipped `floor=0.04` default (1.72×–2.33×) that the benchmark methodology records the…

### Added

- **SSRF protection for webhook endpoints** (`hub/events.py`, audit): `add_endpoint` previously validated only that a URL was `https://`, never where it actually pointed.
- **`GET /disclosure`** (`hub/disclosure.py`, audit,): an always-mounted, unauthenticated endpoint reporting an operator's own configured data region, legal name, and support contact (`HUB_DATA_REGION`/`HUB_OPERATOR_LEGAL_NAME`/…
- **A measured backup/restore drill and a continuous deletion drill** (audit).
- **Accessibility pass on the console and operator console** (audit): every visible form control across `hub/console.py` and `hub/admin.py` now has a programmatically-associated name (`<label for>`/`aria-labelledby`, replacing several placeholder-only inputs), and the API-key scope…
- **Users & roles and API keys are now manageable from the customer console** (`hub/console.py`, `/app/users`, `/app/keys`), not only the CLI — the one deliberate exception to that console's otherwise read-only design.
- **SCIM Groups (`/scim/v2/Groups`, `hub/models.py:ScimGroup`/ `ScimGroupMembership`).** Audit named "SCIM Groups" as a declined gap: a real Groups API needs many-to-many membership, which this Hub's one-role-per-user model (`hub/rbac.py`) has no room for.
- **Automatic webhook alert on a privileged role grant (`user.privileged_role_granted`, `hub/events.py`).** Audit named "an automatic alert on use" as a missing piece of break-glass recovery.
- **Structured subject tagging for exact-match erasure (`Trace.subject_ids`, `tag_trace_subjects`/`find_traces_by_subject`/`purge_traces_by_subject`).** Audit's remaining line: `search_trace_content` could locate candidates via free text but could never honestly certify "this subject has no data here" (a match proves presence, never absence).
- **Application-level IP allowlisting (`hub/server.py:IpAllowlistMiddleware`, `HUB_IP_ALLOWLIST`).** Audit named "no IP allowlisting / private networking" as a single gap; it's really two things, and only one of them is code.
- **SCIM 2.0 user provisioning (`hub/scim.py`, `/scim/v2/Users`).** Audit named "SCIM auto-provisioning" as still open.
- **An opt-in in-process scheduler for alert checks (`hub/scheduler.py`, `HUB_ALERT_SCHEDULER_ENABLED`).** Audit named "no in-process scheduler" as the remaining gap once alerting and reports existed: `check-alerts` and `generate-report` were pure operator-CLI commands meant for external cron.
- **A live OpenTelemetry span exporter (`commontrace/otel_exporter.py`, `commontrace[otel]`).** Audit: "Not done: auto-instrumentation, runtime wrappers, or a collector — CommonTrace consumes OTel, it does not emit it." `CommonTraceSpanExporter` is a real `opentelemetry.sdk.trace.export.SpanExporter`: attach it to a…
- **Named environments and scheduled release promotion (`commontrace/environments.py`, `commontrace release promote|current| pending`).** dev/stage/prod each track which release (`commontrace release cut`) they are running.
- **Locating traces for a subject-erasure request (`search_trace_content`, `hub.manage search-content`).** Audit: "a customer who needs subject-level erasure over trace content must locate the traces themselves; there is no field this system could search on to do it for them." This is that tool: a literal (default) or POSIX-regex…
- **A TypeScript client SDK (`sdk/typescript`, `@commontrace/hub-client`)** — audit's first named non-Python client.
- **Threshold alerts and scheduled reports (`hub/alerts.py`).** Webhooks (6.3) already tell a receiver *when* something happened; this adds *whether* a number an operator cares about has crossed a line, and a periodic usage summary — both delivered through the same signed, at-least-once…
- **Collaboration on traces: comments, assignment, and a notification inbox (`hub/collab.py`).** `hub/manage.py`'s Knowledge Base review queue is an operator surface across every tenant; this is the missing piece for a customer's *own* team working on their own traces.
- **Human user identity, RBAC, and OIDC SSO for the Hub.** An API key authenticates a workload (a CI job, an agent fleet); until now there was no way to authenticate a *person*, or to tell two people who share an org's key apart.
- **Evidence decay: a measured effect stops being billed when nobody has re-measured it.** This product's argument is that a memory earns its place by measured effect.
- **Hybrid retrieval: both arms, fused, instead of picking one.** `commontrace query` chose ONE retriever — semantic when the attention extra was installed and the index was fresh, lexical otherwise — and discarded the other arm's signal entirely.
- **`commontrace retrieval` can set the context budget** (`--max-lessons`, `--max-chars`) and shows `fusion`, the budget, and the exact label assignments will be logged under.
- **`commons/eval/sequential_error_rates.py`.** The sequential-testing unit tests pin the mechanics and cannot answer the question a buyer asks — *how often does this call a useless memory a winner?* That needs simulation, which is too slow for the test suite and too important…
- **Import adapters for LangSmith, Langfuse, Braintrust and OpenTelemetry.** `commontrace import` could read JSONL and CSV with `--title-field` and friends, which works for a spreadsheet and works for none of the four systems a customer is most likely to be coming from: those export *nested* rows, where…
- **Webhook event export from the Hub.** Everything the Hub knew was readable only by polling it, so reacting to a quarantine or an experiment verdict meant a cron job diffing `hub/manage.py` output against last time.
- **Retention policies, legal holds, and a purge you read before it runs.** The Hub could delete one trace or one whole organization, both by hand and both immediately.
- **Dosage, always-on lessons, and retrieval receipts.** Retrieval answered "which lessons match, best first" and stopped there, which left two questions that actually decide what an agent receives unanswered.
- **Immutable releases: what the fleet was running, as one named thing.** This product had two of the three identities a deployable change needs -- a lesson (a mutable slug) and a revision (content identity for one lesson's text) -- and was missing the third.
- **Pre-registration, and a raw export the customer can re-run the arithmetic from.** `experiment.plan` already worked out what it takes to answer the question before a run starts; nothing recorded that plan, and a plan nobody wrote down is a recollection formed after the result is known, by the party the result…
- **A verdict read from a running experiment now survives having been watched.** Every surface here reads a LIVE holdout -- `experiment_status`, the console Proof page, `causal_effects` on every call, `working_set` promoting a trace the moment it clears significance -- which is repeated significance testing…
- **The value aggregate no longer double-attributes occasions, no longer sums interval endpoints, and reports what its own selection is worth.** Three separate defects sat in the arithmetic that turned per-memory effects into the figure an invoice is computed from, and each one made a number that looked like a measurement.
- **A store can require that the approver of a lesson is not its author.** Activating a lesson is the one action with fleet-wide blast radius -- an `active` lesson is injected into every later retrieval verbatim -- and the gate enforced everything about WHAT was being activated (no scaffolding, valid…
- **API keys carry scopes, so a credential minted for one job cannot do every job.** There was one identity per org and one privilege level: hold a key and you could search the corpus, contribute to it, delete a trace outright, and schedule the organization's own deletion.
- **The Hub now refuses to start when row-level security is installed but cannot bite, and the shipped stack serves as a role that cannot bypass it.** Postgres skips every RLS policy for a superuser or a `BYPASSRLS` role silently -- no error, no log line -- so "installed but inert" is a worse state than "not installed": a guarantee an operator believes in and does not have.
- **Content-safety screening for lesson/trace text, closing the OWASP ASI06 (Memory & Context Poisoning) gap: nothing previously inspected what a captured trace or an activated lesson actually said before storing or injecting it verbatim.** New `commontrace/memory_guard.py` scans free text for three independent things: HIGH-confidence secrets (AWS/GitHub/Slack/Stripe/Google/Anthropic tokens, PEM private key blocks, JWTs -- structured shapes vanishingly unlikely to…
- **Issuer signatures for the value ledger, so a hash chain nobody can forge a replacement for is now also a hash chain nobody can fabricate in the first place.** `verify_ledger` proves a ledger is internally consistent -- each entry follows from the one before it back to a fixed genesis -- but that genesis and the hashing algorithm are both deliberately public (the whole point is that a…
- **A survival-analysis censoring check, so a lesson that works faster no longer looks like it is losing data.** `check_differential_attrition` compared terminal outcome-recording rates with no notion of time, and a lesson that helps concludes its occasions SOONER — so mid-run, the treated arm always has more outcomes on the books purely…
- **A rate card and a hash-chained audit ledger for `value_delivered`.** A flat per-occasion rate prices a password reset and an averted SLA breach identically, which is the first thing a finance function rejects.
- **A trivial-prompt gate, so an acknowledgement never pays for a retrieval.** A meaningful share of an agent's turns — "ok", "thanks", "lgtm", "go ahead" — carry no content to match a lesson against; ranking the corpus against them returns noise, and injecting that noise on an occasion it had nothing to do…
- **`working_set` — memory that costs its tokens once per session instead of once per query, and earns its contents.** Retrieval is not free: measured on a live Hub, one `search_traces` page costs ~231 tokens and is paid again on every call.
- **Graduation into `working_set` now expires, so a pinned lesson cannot outlive the evidence for it.** The promotion rule above creates its own blind spot: a pinned trace is injected on every occasion, so it is never withheld, so it stops accumulating the withheld arm its effect was computed from.

### Fixed

- **`commontrace retrieval` no longer erases settings it was not asked to change.** `configure()` wrote only the fields it was given, so a store that had set a context budget lost it the next time anyone touched the floor — the budget silently reverted to the default, meaning an operator tightening precision by…
- **A lesson the budget crowds out is no longer logged as treated.** Holdout arms are now assigned AFTER the dose is computed and only over lessons that will actually be administered.
- **`core` is carried in the lesson cache projection** (`FORMAT_VERSION` 2).
- **One Reciprocal Rank Fusion implementation, not two.** `commons/eval/hybrid_fusion.py` had its own copy of the formula while sweeping for the `k` and arm weights the product should run with, so it could have tuned one implementation and shipped another.
- **A pinned `working_set` trace could be drawn into its own control arm, silently biasing the effect it was promoted for.** `working_set`'s contract says a trace is "either being randomized or it has graduated, never both", and nothing enforced it.
- **`working_set` kept pinning a promoted trace's text after that trace was corrected or deleted.** The block is pasted into a system prompt once and never re-fetched mid-session, so unlike `search_traces` nothing ever re-reads it to notice staleness — which made this the one surface where the bi-temporal supersession gap below…
- **A relevance tie returned a fleet's own re-tellings of a lesson ahead of the lesson itself, and the near-duplicate clustering could not hold a stable randomization unit because of it.** Exact `ts_rank` ties are the signature of near-duplicate text, and a fleet generates those constantly: it resolves an occasion using a lesson, then contributes a trace describing what happened in the same words.
- **The Hub's own randomized holdout fragmented a fleet's statistical power across near-duplicate traces.** Every occasion a fleet resolved and then contributed a trace of -- the exact pattern `commontrace capture` encourages locally -- became a new, independent randomization unit in `holdout_assign`, so instead of one lesson's…
- **A trace's own recorded failure could outrank a working solution for the same query.** `search_traces` ranked purely on text relevance, so an agent's unresolved, escalated occasion log and a hand-written, working lesson describing the same failure ranked on equal footing -- text relevance cannot tell them apart…
- **`search_traces(brief=True)` shipped ~14 always-present metadata fields even at their empty/default value** , so the docstring's own recommended "browse many with brief, then `get_trace` the one you want" pattern measured *worse* than a single non-brief call, not better, on a real corpus.
- **`value_delivered` returning $0.00 read as "this doesn't work" rather than "not enough data yet"** when every memory was UNDERPOWERED -- `reason` stayed empty in exactly that case, with no top-level signal distinguishing a correct measurement from a null result.
- **`pytest`'s dev-extra range still permitted a known-vulnerable version, and fixing it broke `pytest-asyncio` collection.** `pyproject.toml`'s `[dev]` extra capped `pytest` at `<9.0`, whose newest release (8.4.2) carries PYSEC-2026-1845, fixed in 9.0.3.
- **A non-finite `rank` in a holdout log line crashed the entire read.** `json.loads` accepts the bare `Infinity`/`-Infinity`/`NaN` tokens by default, and `holdout_io._opt_int` converted with `int(value)`, which raises `OverflowError` on an infinite float — uncaught, since the append that used it sits…
- **`COMMONTRACE_ALLOW_STORE_SCRIPTS=1` never actually did anything.** `find_reference_script` checked the packaged copy of a reference script first and returned on the first match — but every real reference script (`query.py`, `build_index.py`, `measure_performance.py`, `pilot_metrics.py`) ships…
- **A malformed `tags` argument to the `capture`/`draft_lesson` MCP tools was silently dropped.** `_coerce_tags` returned `None` for anything that wasn't a string, list, or tuple, and both call sites treated that the same as "no tags were given" — the write succeeded, `ok` was `true`, and nothing in the response said the tags…
- **`draft_lesson` had no size guard on the text it writes.** The CLI write paths (`capture`, `lesson new`, ...) all refuse an oversized write via `_validators.check_text_size`, but `draft_lesson` — the primary way an agent puts free-text content into a lesson over MCP — called…
- **A cache entry with a corrupt `terms` field never self-healed on disk.** `lesson_cache`'s write-decision (`_stamps_differ`) compares only `mtime_ns`/`size`/`fm`, deliberately not `terms` (a pure function of `fm`).
- **`failure_import.read_failures`'s fallback size cap counted characters, not bytes.** If `os.path.getsize` failed while `open()` still succeeded, the fallback opened the file in text mode and capped `len(raw)` — decoded characters — letting up to ~4x `MAX_IMPORT_BYTES` of real data through on multi-byte UTF-8…
- **`commontrace import`'s size cap and implicit-store warning had gaps.** `import_data.py` never received the total-file-size cap `failure_import.py` got (only the per-field CSV limit was duplicated); `import_cmd.py` now checks the file size up front.
- **Checkout could mint a second Stripe subscription on an already-subscribed org.** The Overview page hid the "Upgrade" button once an org had a live subscription, but the `billing_checkout` route itself never checked — a stale page, a browser back-button resubmit, or a direct POST reached…
- **Deleting an org with a live Stripe subscription never cancelled it.** Both `confirm_org_deletion` (self-service, reachable by any org's own API key) and `hub.manage purge_org` (operator CLI) deleted the org row — and with it, `stripe_subscription_id` — without telling Stripe.
- **Self-serve billing could be partially configured into a paid-and-never- upgraded state.** `StripeSettings.checkout_configured` checked for a secret key and a price, but not `HUB_STRIPE_WEBHOOK_SECRET` — a deployment missing only that variable would show a working "Upgrade" button, take a customer's real payment via…
- **`retrieve()` (the local MCP tool) shipped a withheld lesson's full body over the wire.** The control arm of the tool's own randomized holdout is the half an agent is explicitly told never to act on — but every withheld lesson's complete instructional text was still being sent on every `retrieve()` call made while an…
- **`experiment_status` (the MCP tool) reintroduced a bug this file already recorded as fixed once.** The CLI's `commontrace experiment` scopes its report to the store's *current* randomization (salt) — changing the holdout rate rotates the salt on purpose, and pooling assignments from two randomizations lets one occasion sit in…
- **`amendment_chain()` walked a trace's amendment lineage with one database round trip per link.** `hub/crud.py`'s BFS issued one query per level of the chain, and nothing capped how deep a chain gets — repeated `amend_trace` calls on the same trace is this codebase's own documented curation pattern ("each attaching whatever…
- **A working, user-facing local flag had its value silently dropped at the Hub sync boundary.** `commontrace capture --profile <name>` has populated `Trace.profile` on disk since the schema was written, and the Hub already modeled the column, read it back on every `get_trace`/ `search_traces`, and correctly carried it…
- **A customer could never learn why an operator declined their Knowledge Base proposal.** `hub/console.py`'s "Note" column read `s.get('reviewer_note')`, a key `crud._submission_to_wire` never produces — the field is named `rejection_reason` there.
- **The admin overview's fleet-wide tiles silently undercounted past 200 organizations.** `traces_by_org`/`quarantined_by_org`/`keys_by_org` in `hub/admin.py:_overview` are already unrestricted, fleet-wide `GROUP BY` aggregates — but `total_traces`/`total_quarantined`/`total_keys` were summed from `rows`, the per-org…
- **The customer-facing memory-search page had no way to reach a result past the 50th.** `search_traces` has returned `offset`/`has_more` since the fix for this product's own core retrieval defect (its own docstring), but `hub/console.py`'s `/memory` route called it with no offset and never read `has_more` back — a…
- **`GET /metrics` had no cardinality bound on the HTTP method label.** `path` was already bucketed to the routes this app actually serves ("the classic way a metrics endpoint becomes the outage," per that code's own comment) — `method` was not, and an HTTP method is constrained only by RFC 7230's…
- **`kb_stats`'s "needs review" hint covered two of `kb-review`'s four buckets.** It was computed as `standings[disputed] + standings[stale]` — `standing_of()` has no "urgent" (security-flagged) or "never_hit" value at all, so an entry in either of those two buckets was invisible to this summary while…
- **`approve-submission` printed the credit it was asked for, not the credit it actually wrote.** `review_kb_submission` clamps `--credit` to `[0, 2**63-1]` before storing it (so `-50` writes `0`), but the CLI's success message printed its own unclamped local variable — an operator-facing report that misdescribed what the…
- **A destructive-operation confirmation prompt crashed on Ctrl-D.** `purge-trace`/`purge-org`'s interactive "type 'yes' to continue" prompt let an ordinary EOF (Ctrl-D) raise `EOFError` uncaught, reaching the operator as a raw traceback instead of the same clean "aborted" message every other way…
- **`commontrace import --dry-run` could report a row as importable that a real run would reject.** Schema validation ran only in the non-dry-run branch, so a row that parsed fine but failed `trace.schema.json` (e.g. a negative `tokens_used`, which the schema floors at 0) was counted by `--dry-run` as one of the traces "would…

### Changed

- **`revision.py:same_treatment()`** had zero callers anywhere in the repository, including tests.
- **`HubConfig.api_key_header`** was defined but never read from the environment and never consulted by the auth middleware, which hardcodes `"Authorization"` everywhere — a config field that silently did nothing no matter what it was set to.

### Added

- **Shareable, read-only Proof links.** Until now the Proof page — the one place this product's central claim (a causal, honestly-caveated measurement of whether the memory changed outcomes) is actually shown — only ever rendered behind an authenticated console session.
- **Self-serve org signup (`HUB_SIGNUP_ENABLED`).** Every account before this was sales- or support-assisted by construction: the only way to get an org and a first API key was an operator running `hub.manage create-org`.
- **Self-serve billing (`hub/billing.py`).** A signed-in customer can now upgrade to a paid plan via Stripe Checkout, and `Organization.plan` stays in sync with what Stripe actually charged via a signed webhook — no operator, no manual row edit.
- **`python -m hub.manage value <org_id> [value_per_occasion]`.** `crud.value_delivered` (occasions improved, causally, priced only when a rate is supplied and never stored) was reachable from a customer's own console session and from an authenticated agent's MCP tool call, but an operator…
- **`search_traces(brief=True)`.** `context_text`/`solution_text` are each allowed up to 20,000 characters, and a search page can hold up to 200 results — a full page can legitimately run to millions of characters, enough to blow a calling agent's own context…
- **CI now proves self-serve signup and console sign-in over real HTTP.** The `compose-stack` job builds and runs the actual container image — the only CI job that catches what only shows up when "the pieces compose" — but never enabled or exercised either surface above; a route-registration mistake or…
- **`SECURITY.md`.** This Hub is multi-tenant, stores customer trace data, and holds an Argon2-hashed API key per organization — and had no vulnerability disclosure policy anywhere.
- **`hub/requirements-lock.txt`: a real answer to the "known follow-up" `hub/requirements.txt` already named** ("this still isn't full transitive-dependency reproducibility... pinning every indirect dependency too").
- **`.github/dependabot.yml`.** Pairs with the lock file above — a lock nobody is prompted to revisit rots exactly the way its own header warns against.
- **The causal instrument and the commercial number were never connected — and the commercial one was the confounded one.** The product strategy names this product's pricing hypothesis (price against measured effect per fleet, not seats or trace volume) and says "the mechanism ships".
- **The Hub can size an experiment before an operator starts one.** `python -m hub.manage plan-experiment <org_id>` reads that org's own retrieval volume and its own resolution rate and says what holdout rate a 10-point effect needs — or says plainly that no rate answers it in this window.
- **A fleet can now set its own holdout rate, and both retrievers read it.** `commontrace experiment --configure --rate 0.5` writes the store's experiment settings; `commontrace query --experiment` and the MCP `retrieve` tool both read them.
- **`commontrace experiment --plan` designs the experiment before you run it.** The failure it prevents is expensive and silent: a fleet runs a 30-day pilot at the default rate and the report on the last day says "not enough data yet".
- **Customers had no interface.** `hub/admin.py` is the *operator* console — one vendor employee, cross-tenant, moderating the Knowledge Base — and until now it was the only HTML the Hub served.
- **An effect size was attached to a mutable name, and the treatment could change underneath it.** The validity audit shipped alongside this checks whether the *sample* can support an estimate.
- **The causal number is now audited, and it could be confidently wrong before.** `commontrace/experiment.py` estimates each lesson's effect correctly — two-proportion tests, a 95% interval, Benjamini-Hochberg across lessons, underpowered comparisons kept out of the correction, an explicit `UNDERPOWERED`…
- **A power projection, because "underpowered" on day 30 is a spent pilot.** `experiment` already said a lesson was underpowered and how many observations each arm needed.
- **The local tier now speaks MCP, so an agent no longer needs a terminal to use its own memory.** `commontrace serve` exposes the local store over MCP stdio: `retrieve`, `capture`, `propose_lessons`, `list_lessons`, `get_lesson`, `draft_lesson`, `approve_lesson`, `reject_lesson`, `store_status`.
- **The Knowledge Base is now operable from the console** — the review queue that decides what goes into the one surface where anything crosses an org boundary.
- **A read-only operator console at `/admin`** , served by the Hub itself and off unless `HUB_ADMIN_TOKEN` is set.

### Changed

- **A proposed lesson now carries the evidence needed to write it.** This is the throughput limit on the whole product, and `distill` was making it as expensive as possible.
- **The arm-balance check flagged one sound experiment in ten.** It shared the attrition check's alpha (0.10), and a two-sided test at alpha=0.10 flags a *correct* randomizer about 10% of the time — at every n; that is what an alpha is.
- **`commontrace experiment --strict` now also fails a compromised run.** The flag means "stop the build if the memory is making things worse", and a biased comparison cannot answer that in either direction.

### Fixed

- **Every hub-tests CI job failed at collection, on all three Python versions, and the local suite passed the whole time.** `install_cmd` grew a module-level `from commontrace import mcp_server` to read a tuple of tool NAMES; `mcp_server` pulls in the retrieval stack (`evidence_io` → `frontmatter` → `yaml`).
- **The local causal report pooled every randomization the store had ever run.** The Hub has always scoped its analysis to the current salt, in SQL.
- **A 240-occasion pilot with a real +25pp effect reported `NO_MEASURABLE_EFFECT`.** Found by running the customer journey to the end — import 36 tickets, curate three lessons through the MCP tools, run 240 occasions with the holdout, read the report.
- **Hub integrity findings used the local tier's vocabulary.** The two tiers randomize different objects — the local tier withholds *lessons*, the Hub withholds *traces* — and the shared checks reported both as "lesson".
- **`commontrace doctor` could be killed by its own optional-dependency probes.** `importlib.util.find_spec` walks `sys.meta_path`, so any import hook installed in that interpreter gets to raise inside it — and an unhandled exception from the `numpy` or `sentence_transformers` probe took down the whole report…
- **`commontrace sync --push-traces` could not complete against a default-configured Hub, and reported the failure as a network outage.** Measured on a 46-trace store: **0 of 46 traces pushed**, 62 of 100 requests refused with HTTP 429, and every one reported as `could not reach the Hub ... unhandled errors in a TaskGroup (1 sub-exception)`.
- **Unedited scaffolding could become an active, injected, published lesson.** Reproduced end to end on a real store: `commontrace distill` writes a candidate whose Rule, How-to-apply, Counter-examples, `applies_when` and `do_not_apply_when` are all `TODO: ...`; `lesson approve` activated it; `lesson…
- **`sync --pull` followed by `sync --push-traces` pushed the Hub's own traces back to it.** Pulled records are written into the local traces directory as `hub_<slug>_<id>.md` with `hub_trace_id` already set, so the push path saw each as "on the Hub, no recorded fingerprint" and amended the Hub's trace with a…
- **The anti-brute-force limiter throttled legitimate clients hardest.** The auth-attempt limiter exists to bound the Argon2 CPU an unauthenticated source can force, but it charged every request -- successful ones included.
- **`hub/bench_scaling.py` crashed while printing its own results.** `growth_factor` is `None` whenever the smallest corpus measured 0ms -- the ordinary case for a fast read path -- and formatting `None` with `:>6.1f` raises `TypeError`, after every measurement had been taken and thrown away.
- **`commontrace doctor` printed affirmative labels for negative results** , e.g.

### Added

- **`--threshold-lexical`, `--threshold-freshness` and `--threshold-composite` are implemented.** They were parsed, forwarded by `commontrace bench`, and read by nothing: a fleet could set a quality gate, watch it never fire, and conclude quality was fine.
- **`GET /metrics`** (Prometheus text format): requests by method/route/ status, summed duration per route, and refusals per limiter.
- **Every numeric Hub setting is range-checked at startup.** `HUB_PORT=99999` died in uvicorn's bind, `HUB_DB_POOL_SIZE=-1` in SQLAlchemy on first query, and `HUB_MAX_TITLE_CHARS=-5` rejected every `contribute_trace` with nothing anywhere saying why.
- **`Retry-After` on every rate-limit refusal** , HTTP and tool-level alike, plus a one-time notice from `sync` explaining that a large push is pacing itself -- a correct slow push read as a hang.

### Changed

- **`HUB_RATE_LIMIT_PER_MINUTE` default raised from 20 to 120** (burst 5 to 30).
- **The Hub's rate limiter caps how many keys it tracks.** The idle sweep evicts nothing for an hour, and the client-address-keyed limiters are keyed on something the peer chooses (any address out of an IPv6 /64), so an unauthenticated flood could grow process memory without bound via…
- **An MCP session-teardown `DELETE` is no longer charged to the read limiter.** It runs no tool and reads no row, and refusing it made an otherwise successful command print `Session termination failed: 429`.

### Added

- **The measurement loop is now reachable from where agents actually are** : an optional `occasion_id` on `search_traces`, the Hub's full tool surface in the generated MCP config, and holdout instructions in **both** skills `commontrace install` can write -- the repo's own `SKILL.md` (used whenever…
- **`commontrace prove`** , the client path to the Hub's measurement tools (`prove outcomes` / `prove assign` / `prove record`, plus `hub_client.fleet_outcomes` / `holdout_assign` / `record_occasion_outcome`).
- **The randomized holdout, in the Hub** (`holdout_assign` / `record_occasion_outcome` MCP tools, `hub/manage.py start-experiment` / `experiment` / `stop-experiment`, `HoldoutObservation`).
- **A measured answer to the product strategy's weakest link** (`hub/bench_scaling.py`, results in the scaling analysis). lists five links the business case rests on and marks exactly one "unmeasured, and the weakest link nobody has looked at": *value compounds within a customer faster…
- **Fleet outcome measurement in the Hub** (`fleet_outcomes` MCP tool, `python -m hub.manage outcomes [org_id]`, `hub/outcomes.py`).
- **Knowledge Base entry standing, and the operator queue that acts on it.** Seeding and community submissions both answer how content gets into the Knowledge Base; nothing answered what happens when an entry stops being true, and a curated corpus that only grows is one that decays.
- **Self-service deletion.** An org's own API key can now delete its own data without operator/DB-access trust: `delete_trace` removes one trace (and its full amendment chain) immediately, and `request_account_deletion` / `confirm_account_deletion` /…
- **A reviewed community-submission channel for the Knowledge Base** (`submit_kb_entry` / `list_my_kb_submissions` MCP tools, `commontrace commons submit` / `commons submissions` CLI, `hub/manage.py list-submissions` / `approve-submission` / `reject-submission` operator commands).

### Changed

- **The commons is no longer org-to-org. It is a single, optional, operator-curated Knowledge Base.** The previous design let one org opt a trace into a shared corpus other orgs' queries could match against (`share_trace`/`unshare_trace`, `commontrace commons contribute`).

### Removed

- **`share_trace` / `unshare_trace` MCP tools** , and the `commontrace commons contribute` / `commons unshare` CLI subcommands built on them.
- **The "earn query allowance by contributing" mechanic** (`plans.QUERY_CREDIT_PER_HIT`, `entitlements()["commons_queries"]["earned"]`, `entitlements()["delivered_hits"]`).
- **`hub/manage.py commons-value` and `commons-stats`** — the per-org contribution ledger and the "how many distinct orgs contribute" adoption metric.

### Fixed

- **`fleet_outcomes` was superlinear in an org's corpus (exponent 1.12), three commits after being added.** It selected every matching trace's `outcome` JSONB and counted in Python -- tens of thousands of blobs crossing the wire and a Python dict per row, to produce six integers.
- **The benchmark's first run blamed the wrong thing, and the fixture was the reason.** It reported `search_traces` as linear-or-worse (0.88).
- **`hub/DEPLOYMENT.md`'s scaling claim rested on one data point.** The existing "~113 ms sequential scan -> ~9 ms index scan at 50k traces" shows the index works and says nothing about growth; it now points at the measured exponents and carries the two caveats above.
- **CI was red: two new test files crashed pytest collection with no numpy installed.** `tests/test_empirical_storage_and_index_resilience.py` and `tests/test_storage_remediations.py` did `import numpy as np` unconditionally at module scope, so the core-install and dev-extra CI jobs (which don't install the `attention` extra)…
- **`load_schema`'s path-traversal guard missed backslash/colon separators.** `os.path.basename` only treats `/` as a separator on POSIX, so a name like `C:\trace.schema.json` passed the "is this a bare filename" check unchanged on Linux, then 404'd instead of raising the intended `ValueError` -- not…
- **`sync`'s Hub URL had no explicit scheme guard** , despite a commit message claiming one. httpx already refuses to open a `file://`/`ftp://` "connection" so nothing was exploitable, but that safety was incidental to the HTTP client, not a guarantee this module made -- and a bad…
- **`query --experiment` silently did nothing on the semantic path.** Only the lexical branch honoured the holdout flags; the semantic branch forwarded just the query and `--top-k` to the reference script.
- **`amend_trace` bypassed every write guard.** It skipped schema validation, size limits, the rate limiter, and quarantine - all of which `contribute_trace` enforces - making it the way around all of them: unbounded writes, and a title past the column width returning a hard…
- **`import` wrote schema-invalid traces that `capture` refuses.** A bulk import is the likeliest source of malformed records - it is someone else's export - so accepting what `capture` rejects made the importer the one hole in the store's invariants, and a bad row was averaged into `bench…
- **Holdout assignments were not de-duplicated on retry.** The log is append-only, so a retried task rewrote the same `(lesson, occasion)` pair; counting it twice inflated the arm and deflated the p-value, meaning a retry storm could manufacture significance.
- **`minimum_detectable_effect(power=...)` was accepted and ignored** - both branches of a ternary were the 80% constant, so asking for 95% power silently returned the 80% answer and understated the sample size an experiment needs.
- **`release-quarantine` audited an empty reason.** It read `quarantine_reason` after the UPDATE had already synchronized it to `""`, so every audit row recorded `was=` - losing precisely the fact the row exists to preserve.
- **Alert text was interpolated into the HTML report unescaped** , bypassing the `html.escape` the same file applies to all other frontmatter text.
- **`commontrace bench` crashed on an episode with no `name`.** A malformed file must not take down a report about the whole corpus.
- **`install.sh` aborted on its own success path on bash < 4.4** (stock macOS): `${#ARR[@]}` on an empty array under `set -u` is an unbound variable.
- **`--dest` no longer loses to an exported `$COMMONTRACE_ROOT`.** `run_script` used `env.setdefault`, so a child script inherited the *old* env value and silently discarded an explicit `--dest`, inverting the precedence `paths.py` documents.
- **`protocol/PROTOCOL.md` contradicted itself three ways** — the heading said five stages, the table listed seven, and the diagram showed a different five with **Validate** missing and Inject renamed.
- **`lesson list` and `trace list` crashed on a present-but-empty field.** `.get(k, default)` returns the default only when the key is *absent*; `status:` with no value parses to `None`, which has no `__format__` for a width spec.
- **Mistyped paths raised raw tracebacks.** `lesson validate /nope/x.md` and `trace validate <a directory>` reached `open()` unguarded.
- **`capture` wrote traces that violate the shipped schema.** `--tokens-used -5` landed on disk and was only caught by a later `trace validate`, while `pilot_metrics` averaged the negative number into a customer-facing cost figure in the meantime.
- **The YAML fallback parser disagreed with PyYAML on four numeric forms.** Its docstring claimed `7.0e3` resolved as a float "confirmed against yaml.safe_dump/safe_load"; PyYAML's YAML-1.1 resolver requires a *signed* exponent, so it is the string `'7.0e3'`.
- **`split_baseline` was O(n²).** `t not in baseline` is a full dict comparison per trace, correct only because `load_traces` happens to set `_path` on every dict — a load-bearing side effect of an unrelated line.
- **Removed the committed `.devin/skills/commontrace/SKILL.md`.** It was the only install output checked into the tree, and `install --target devin` overwrites that exact path with the root `SKILL.md` — so the committed stub was whatever a Devin user saw until they ran install, at which point…
- **Deleted four stale pre-rename assets** (`justdoit_overall.{dot,png}`, `agent_orchestrateur.{dot,png}`) still carrying French labels and the string `/justdoit v2.3`.
- **`SKILL.md`'s description was 1013 of 1024 permitted characters.** Eleven characters from silently failing to load.

### Added

- **`commons_search` / `commontrace commons ask` — the commons becomes a knowledge base you can query, not just a meter that scores you.** The positioning has always described a searchable, ranked corpus where "each distinct problem need only be solved once".
- **Agents under management: the expansion meter, and the per-agent pricing it makes enforceable.** The product strategy concludes the variable to run this business on is "agents under management, not logos", and asserted it was already measurable.
- **`maxLength` in `commontrace/validate.py`.** Required before the trace schema could bound `agent_id` to the Hub's column width: the validator enforces a deliberate subset and *raises* on an unknown keyword rather than ignoring it, so an unimplemented constraint cannot ship…
- **`commontrace taxonomy` / `commontrace impact` / `commontrace pilot` — the 30-day pilot's three leave-behinds, as real commands rather than a slide.** `taxonomy` groups recurring traces into a structured map of failure patterns (reusing `distill`'s clustering, but read-only and showing coverage status rather than writing candidate lessons).
- **`python -m hub.smoke` — post-deploy verification against a live server.** CI proves the code and the compose stack work; it cannot prove *your* deployment works — your TLS terminator, your managed Postgres, your ingress — and that gap is where deployments actually fail.
- **`compose-stack` CI job — the deployment path, end to end.** Brings up the documented stack, waits on `/readyz`, asserts migrations created every table, provisions two orgs through the operator CLI, drives the running server over real HTTP (all six tools, 401 without a key, cross-tenant…
- **Readiness healthcheck on the `hub` compose service.** It probes `/readyz` rather than `/healthz` — readiness checks the database, which is what "can this container serve a request" actually depends on; the liveness endpoint would report healthy while every call failed.
- `validate.assert_supported_schema()` — this validator implements a deliberate subset of JSON Schema, and an unsupported keyword was previously ignored in silence.
- A test asserting `protocol/schemas/` and `commontrace/schemas/` stay byte-identical.
- Coverage for `lesson list` / `trace list`, which had none at all.
- **`commontrace bench` and `bench --pilot` now work from a plain `pip install`.** Both reference scripts lived only in the repo checkout, so a customer could install the product and still be unable to compute their own pilot metrics — the one number they most need, and the whole point of the before/after story.
- **`capture` and `lesson new` now inherit the store's `agent_type`.** `init --agent-type support` stamps the type into `memory/INDEX.md`, but both commands hard-defaulted to `code`, so a support/sales/ops fleet silently mislabeled every record unless the operator repeated `--agent-type` on every…
- **The generated Hub MCP config could not connect.** `commontrace install` wrote the stdio shape (`command`/`args`/`env`) for a server that speaks streamable-HTTP — there was nowhere to put the endpoint or the bearer token, so anyone pasting the template simply failed to attach.
- **`import` accepts the protocol's own field names.** `context_text` / `solution_text` are what `sync --pull` writes and `search_traces` returns, yet the importer required `--context-field` flags to rename them into the names we ourselves emit — so the product could not round-trip…
- **`hub.manage` reports operator mistakes as errors, not tracebacks.** A bad day count or an unknown org id raised a raw `ValueError` traceback from the production operator CLI; these now print `error: ...` and exit 2.

### Added

- **Causal effect measurement via randomized holdout** (`commontrace experiment`, `commontrace query --experiment`).
- **`commontrace reliability` now states that `lift` is correlational** and points at `commontrace experiment`.
- **Production deployment artifacts.** `Dockerfile` (multi-stage, non-root, no build toolchain in the runtime layer), `docker-compose.yml` (with migrations as a one-shot service the app waits on, so replicas can't race the same DDL), `.dockerignore`, and…
- **Observability** (`hub/observability.py`): JSON logs on stdout, a request-correlation id (honoring an inbound `X-Request-ID`, echoed back in the response) on every log line, and one structured line per request with method/path/status/duration …
- **`/readyz`, split from `/healthz`.** `/healthz` (liveness) answers "is this process alive" and does **not** touch the database on purpose; `/readyz` (readiness) runs `SELECT 1` and returns 503 when Postgres is unreachable.
- **Audit log** (`hub/audit.py`, `audit_log` table): every MCP write and every `hub/manage.py` admin command is recorded.
- **API-key expiry.** `issue-key <org_id> [days]`; expired keys are rejected at verification with no revocation job needed, and are indistinguishable from invalid ones.
- **Search pagination.** `search_traces` takes `limit`/`offset` (clamped to `MAX_SEARCH_LIMIT`) and returns `has_more`.
- **Client resilience.** `commontrace/hub_client.py` now sets a finite request timeout (it had none, so a stalled Hub hung `sync` forever) and retries transport failures with exponential backoff — never retrying auth or validation failures, which cannot…
- Connection-pool sizing, graceful shutdown (the engine is now disposed on exit rather than dropping pooled connections), and a CI step that applies every migration to an empty database plus `alembic check` — migrations were…
- **Hub admin/monitoring commands** (`hub/manage.py`): `stats` (org/key/ trace/vote counts, mean trust), `list-quarantined [org_id]` (the abuse-control review queue), `release-quarantine <trace_id>`, and — closing a gap the retention policy previously flagged as…
- **`commontrace import`** (`commontrace/import_data.py` + `commands/import_cmd.py`): bulk-import an existing JSONL or CSV export into `memory/traces/`, so a fleet can start from its historical traces with no infrastructure replacement.
- **A generic Curator/Validator loop for any `agent_type`** (`commontrace distill`, `commontrace lesson approve|reject`).
- **A dependency-free retrieval fallback** (`commontrace/retrieval.py`).
- protocol/PROTOCOL.md's Roles table gained a "Generic CLI reference" column pointing Curator/Validator/Retriever at the commands above.
- **The CommonTrace Hub server (`hub/`).** Previously `protocol/PROTOCOL.md` described a Hub as already in production while `commontrace sync` made no network call at all — this closes that gap with a real implementation: an MCP server (`search_traces`…
- `commontrace/hub_client.py` + `commontrace sync --push`/`--pull`: the client half of the bridge, now a real implementation instead of printed instructions — pushes local `active` lessons to the Hub via `contribute_trace`…
- Benchmark credibility (`commontrace/reference/measure_performance.py`, `memory/attention/query.py`): every run now persists to `memory/benchmark_reports/*.json` (`schema_version`-tagged) with new…
- CI (`.github/workflows/ci.yml`) running the test suite and a `ruff` lint pass on Python 3.10, 3.11, and 3.12, both for the core install (`pip install -e .`) and the `dev` extra (`pip install -e ".[dev]"`).
- `LICENSE` file (MIT) at the repo root, matching the license already declared in `pyproject.toml`.
- A support matrix in `README.md` for `commontrace install --target <...>` documenting, per target, what file(s) are written and how their format was verified (template-vs-published-spec, not live-tested against the running…
- `[INFO]` severity in `commontrace doctor`, for conditions that are expected and not actionable in a normal client install (e.g. the optional `attention` extra not being installed, or reference scripts only present in a source…

### Changed

- **`search_traces` matching is full-text, not substring — a visible behavior change, not a transparent optimization.** The old `ILIKE '%query%'` could not use any index (a leading wildcard defeats B-tree prefix matching), so every search sequentially scanned the org's traces.
- **`search_traces` returns a dict** (`{"traces", "limit", "offset", "has_more"}`) rather than a bare list, to carry pagination state.
- **Fixed an N+1 in trace hydration.** Votes and relations were fetched per trace, so a 50-result search issued 101 queries; they are now batch-loaded in 2 queries regardless of result count.
- `hub/tests/conftest.py` drops and recreates the schema per session: `create_all` never ALTERs existing tables, so a test database left on an older revision silently kept stale columns.
- `ruff` added to the `dev` optional-dependency group, with an explicit `[tool.ruff.lint] select = ["E", "F", "W", "I"]` policy rather than whatever a given `ruff` release's default rule set happens to include — needed because this…
- README install-target quick-reference and file-layout table point at the new support matrix instead of asserting untested platform behavior.
- Documentation no longer implies `pip install commontrace` (bare, from PyPI) works today; `pip install -e .` from a repo checkout is the only currently verified install path, and PyPI publication is called out as a future step…
- `README.md` no longer describes the Hub as "production" infrastructure external to this repo; it now points at `hub/` as the (self-hosted, not hosted-by-this-project) server implementation.
- The `[attention]` optional extra's `sentence-transformers` floor bumped from `<5.0` to `>=6.0,<7.0`, with an explicit `transformers>=5.5.0` floor (mirrored in `requirements.txt`) — see Fixed.
- CI gained a `test-hub` job (Postgres 16 service container, `hub/tests/` including `test_tenant_isolation.py`) alongside the existing core/dev jobs.

### Fixed

- `commontrace install --target cursor|generic-mcp` generated `commontrace.hub.mcp.json.example` was **not valid JSON**: the Hub tool list was interpolated into a JSON string field with an f-string template, leaking unescaped…
- `tests/test_attention_query.py` imported `numpy` unconditionally at module scope, so the whole test module (and therefore `pytest tests/`) failed to *collect* — not just skip — when the optional `attention` extra wasn't installed.
- `[attention]`'s previous `sentence-transformers<5.0` cap transitively resolved a `transformers` version with 5 known RCE-class CVEs (PYSEC-2025-217, PYSEC-2026-2288/2289/2290) in checkpoint/config deserialization (found via…

## [2.0.0] - 2026-08-18

### Added

- `protocol/PROTOCOL.md` — the canonical, implementation-independent CommonTrace Protocol spec: the `Trace` / `Lesson` object model (§3), the Local/Hub store conformance tiers (§5), generalized roles (§6), the open taxonomy…
- `protocol/schemas/trace.schema.json` and `protocol/schemas/lesson.schema.json` — universal, agent-agnostic JSON Schemas for `Trace` and `Lesson`, aligned 1:1 with the production CommonTrace Hub's live trace object…
- `commontrace` CLI package (`commontrace/`) — a client-installable, agent-agnostic CLI (`pip install -e .`) with `init`, `install`, `capture`, `trace`, `lesson`, `query`, `index`, `bench`, `sync`, and `doctor` subcommands, and…
- `Trace.extensions` (namespaced under `Trace.profile`) as the mechanism for profile-specific fields that don't generalize across agent types (e.g. a git commit SHA for the code-review profile) — see PROTOCOL.md.
- `Trace.outcome` and the five pilot metrics (repeated-error rate, resolution rate, escalation rate, frustration rate, token/LLM-call cost) — see PROTOCOL.md and `commontrace/reference/pilot_metrics.py`.
- Support for five new agent types beyond `code`: `support`, `sales`, `hr`, `marketing`, `ops`, `custom`.

### Changed

- **`domain` went from a closed 7-value enum to an open vocabulary.** The code-review profile's original 7 values (`git-safety`, `cuda-gpu`, `refactor`, `testing`, `subagents`, `performance`, `other`) remain valid starter domains for `agent_type: code`; they are no longer the only values the…
- **Profile-specific fields moved into `extensions`, namespaced under `profile`.** Fields like a commit SHA or a review verdict that only make sense for the code-review profile are no longer implied to belong on the universal `Trace`/`Lesson` core; a profile declares itself via `Trace.profile` and puts…
- **Version unification.** Package version and protocol version were previously two different numbers (package `1.0.0`, protocol `1.1.0`).
- `SKILL.md`'s double-review pipeline (Alpha → A → B → Omega → Lambda) is now documented as *one* conformant profile — the "code-review profile," versioned independently at v2.3 — rather than the only shape the protocol supports…
- `README.md` restructured around two entry points: the CLI (any agent type) and the code-review reference profile (`SKILL.md`), rather than only the latter.

### Breaking changes

- None at the schema level for existing data.
- If a prior deployment's tooling relied on `domain` being restricted to exactly the 7 historical values (e.g. rejecting anything else), that external validation behavior is no longer enforced by the protocol itself now that the…

### Fixed

- `commontrace query`/`commontrace index` no longer crash when the optional `[attention]` extra isn't installed.
- Path-traversal and other input-validation issues in lesson/trace writing.
- A business-critical metrics computation bug in `commontrace/reference/measure_performance.py`.
- Frontmatter `---` delimiter parsing edge cases.
- Install-target file-overwrite and Hub-credential-in-git safety warnings (`commontrace install`).

### Removed

- The requirement that `domain` be one of exactly 7 fixed values — superseded by the open taxonomy in PROTOCOL.md (see "Changed" above; not a schema removal, since no schema file in this repo ever encoded that enum).
