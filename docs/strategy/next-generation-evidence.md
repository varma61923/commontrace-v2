# Next-generation audit: 2026-10-10

All nine requested repositories were cloned into the execution workspace. The
Graphify source repository was also cloned and installed in an isolated Python
environment; its Codex skill was registered. Graphify 0.9.84 ran code-only AST
extraction, clustering, diagnostics and architecture queries over each code
repository. No semantic model calls were made and no competitor service was run.
This audit does not reproduce competitor accuracy claims.

Pinned commits and full diagnostic summaries are in
[measurements/graphify-audit.json](measurements/graphify-audit.json). Generated
JSON graphs, reports and query transcripts remain under the local
`/workspace/research` directory; they are large, rebuildable analysis artifacts
and are not vendored into CommonTrace.

| Repository | Commit | AST nodes | AST edges | Relevance to this phase |
| --- | --- | ---: | ---: | --- |
| CommonTrace | dcab4fd | 20,810 | 61,120 | `add_fact` → `mutate_facts` → whole-file load/save; `_StatementIndex` rebuilt on writes; evidence must stay fresh |
| Graphiti | a9ef13f | 5,336 | 17,709 | Candidate-first dedup and temporal preservation |
| cognee | 0ec7a9f | 43,268 | 119,538 | Safe distillation cursors; deferred to P2 |
| mem0 | b7ad69a | 15,920 | 34,310 | Local SQLite spool and persistent history; do not copy its ADD-only limitations |
| EverOS | fcd41b5 | 13,098 | 32,208 | Durable event processing and recoverable work |
| hindsight | 44d5340 | 65,487 | 158,936 | Consolidation and grounded retraction; preserve history |
| Zep | cfa2ab2 | 9,052 | 21,564 | Context assembly and framework integration; deferred to P1/P3 |
| letta-code | d31b879 | 19,881 | 65,436 | Versioned memory and reflection provenance; learner work deferred |
| supermemory | 552803c | 4,226 | 8,632 | Version envelope concepts; profiles deferred to P3 |

The final CommonTrace graph was regenerated after implementation: 20,916 nodes,
61,531 edges (before the final small transaction-removal fix). Source commits in
the manifest identify baseline graphs, not the subsequently edited working tree.

Graph diagnostics found no dangling or missing endpoints. All graphs include
self-loop edges (CommonTrace 88, competitors 5–145); these can represent recursion
or extraction artifacts. AST graphs, especially undirected graphs and code with
dynamic registration, cannot prove that code is dead. No module was deleted
solely because the graph lacked a production caller. P4's runtime reachability
and CI audit remains outstanding.

The implementation is original stdlib code. The ideas above and the attached
brief informed the design; no competitor implementation was copied. The existing
CommonTrace temporal guard, scope rules, evidence resolver, causal statistics and
retrieval score formulas remain the governing behavior.

## Acceptance decision

P0 is **partial; stopped at the gate**. The local migration, transactional ledger,
versioned fact envelopes and persistent statement indexes are implemented. The
30,000-fact run measured single-write p95 1.98 ms versus 5,906.58 ms for JSONL,
and 3,448 facts/s for a 1,000-fact SQLite batch. These are synthetic, selective
workloads on this machine, run alongside validation. The million-fact run failed
with `sqlite3.OperationalError: database or disk is full`; temporary benchmark
storage was then removed by the harness. There is no million-fact result.

Hybrid retrieval at scale, the Postgres fact engine and signed storage manifests
are unverified or unimplemented. Selective overlap search timings are not hybrid
search timings. Existing content-hash embedding caches were preserved, but no
new dense restart benchmark was run. No new reader/judge answer accuracy or
CodingLessonBench uplift is claimed.

Section 7 of the attached brief says: **"Stop and report when a gate is missed,
rather than lowering it."** The branch therefore stops before P1–P4. The next
storage step is to bound projection space (compact numeric fact/term identities,
shared signature blocks and posting storage), then rerun the unchanged million
fact gates. This is a recommendation, not an implemented or measured result.

CodeQL alert dismissal is blocked: `gh api
repos/varma61923/commontrace-v2/code-scanning/alerts` returned HTTP 403,
"Resource not accessible by integration". Source-based review context was added
to `.github/codeql/codeql-config.yml`; queries were not suppressed and no alert
was dismissed.

## Validation

- `python -m pytest tests/ e2e_tests/ -q`: **6,552 passed, 118 skipped**.
- `python -m pytest hub/tests -q` from the repository root, against PostgreSQL
  16: **2,492 passed, 2 skipped**. The disposable test database used fsync and
  synchronous commit disabled to accelerate fixture truncation; no server
  durability or server performance result is inferred from this test run.
- Focused migrated and JSONL fact/evidence/lifecycle tests: **192 passed**.
  A later focused store run additionally covers staged add/remove rollback.
- Existing fact-index branch-inclusive coverage gate: **96.12%**, above 95%.
  The new store's focused branch-inclusive coverage measured **87%**.
- Ruff, Bandit at medium/high severity, the CI strict-mypy list (35 files), and
  strict typing of the new record envelope passed. Core and pinned Hub
  dependency audits reported no known vulnerabilities.
- `python scripts/dev.py docs --check` and
  `python scripts/export_openapi.py --check` passed. Packaged schemas match the
  protocol sources. Original causal conformance vectors are unchanged.
- The pinned 154-question LoCoMo paired retrieval regression gate passed at
  1,500 and 4,000 tokens with **zero change in every compared retrieval metric**.
  This comparison used the current base revision and no reader/judge models;
  it is not answer accuracy. Details and commands:
  [measurements/p0-conversation-regression.json](measurements/p0-conversation-regression.json).
- The existing local retrieval-latency and 50K governed fact-search performance
  gates passed. These exercise the preserved JSONL path, not the new 1M gate.
- Original memory/fact contract benchmarks and `bash reproduce.sh` passed;
  independent processes reproduced identical functional causal metrics.

Optional-service/browser skips remain skips. These results describe local
validation, not a claim that GitHub's full multi-platform CI has completed.
