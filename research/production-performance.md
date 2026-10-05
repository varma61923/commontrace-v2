# Production performance improvements

The changes extend published `8846feaccf94f6ad9dc37787c3c1f8fb63acdcca`.
They independently apply useful ideas from the six pinned competitor repositories
and papers, with source provenance, historical beliefs and bounded contexts
preserved. [algorithm-audit.md](algorithm-audit.md) records the code and research
review. No competitor source or official judge was changed.

## Measured hot paths

| Operation and workload | Before | After | Gain | Comparison |
|---|---:|---:|---:|---|
| Exact dense search: 200,000 passages, 768 dimensions, eight facets | 22,527.588 ms | 573.884 ms | 39.25× | Original `4b555b0`; conservative earlier measurement |
| Same search against the latest batched baseline | 3,104.567 ms | 573.884 ms | 5.41× | Published `8846fea` |
| Delete one session among 20,002 facts | 52.528 ms | 0.760 ms | 69.1× | Published `8846fea` |
| Resume extraction after 20,000 old messages, with five new messages | 164.121 ms | 2.387 ms | 68.8× | Published `8846fea`; includes added source-proof validation |
| Reuse a valid summary of 10,000 messages | 71.410 ms | 0.509 ms | 140.3× | Published `8846fea` |
| Construct the same model-summary prompt from that session | 84.198 ms | 1.350 ms | 62.4× | Published `8846fea`; inference excluded |
| Warm active-lesson page: 1,000 lessons, page 25 | 106.132 ms | 3.260 ms | 32.56× | Published `8846fea` |
| Warm review page: 1,000 active lessons + 25 drafts | 582.761 ms | 5.786 ms | 100.72× | Published `8846fea` |
| Hub queue and true total: 10,000 entries | 770.227 ms | 10.665 ms | 72.22× | Published `8846fea`; real PostgreSQL 16 |

These ratios describe the specified components and workloads. They cannot be
multiplied together or interpreted as 20× for every request, model, dataset or
competitor. The vector search lists, summary prompts and operator responses
match the baseline in the measured comparisons. Shared development workloads
affect timing; raw samples and independent avoided-work counts are retained.
Context budgets are unchanged; these gains do not come from larger LLM prompts.

Detailed results, limits and reusable helpers:

* [Disk-backed retrieval](retrieval-scale.md): exact scoring, immutable source
  generations, private atomic files, bounded disk use and fallback behavior.
* [Incremental ingestion](ingestion-performance.md): scoped chronological repair,
  checkpoint paging, retention-safe index reuse and source-content validation.
* [Summary work](summary-performance.md): indexed validity checks, lazy prompt
  construction, identical prefixes and revision-checked publication.
* [Console serving](serving-ui-performance.md): source-identity cache validation,
  scoped content diagnostics, immutable public outputs and credential freshness.
* [Hub queue](hub-queue-performance.md): SQL eligibility, priority, pagination and
  count from one database snapshot without full-corpus ORM hydration.
* [MCP progress](mcp-progress.md): native opt-in notifications, request correlation
  and worker-thread ingestion and summarization keep other requests responsive.
* [Roadmap assessment](roadmap-assessment.md): the supplied report's six proposals,
  corrected claims and criteria for accepting future architectural changes.

## Correctness found during independent review

SQLite can recycle a deleted message id. A model response obtained before deletion
must not attach to a replacement message merely because its id and external
reference match. Extraction now fingerprints each complete source message and
revalidates that proof inside the publication transaction before writing any
facts or advancing the checkpoint. Concurrent unrelated appends remain supported.
The final extraction timing above includes this validation cost.

The frontend previously could retain the old credential's capabilities after
introducing a freshness window. Retained route data and capability timestamps
now follow the credential, and credential replacement or authentication failure
clears prior data immediately. Older asynchronous responses cannot overwrite a
newer route or credential. Server authorization and approval gates remain live.

Disk snapshots are validated against authoritative source identity, revision and
passage count. A well-formed incomplete snapshot is rejected, and an older source
snapshot cannot overwrite a newer published generation. Corrupt files, disk
allocation failures and unsupported publication locking are cache misses that
fall back to source-backed streaming.

## Official evaluation limits

The frozen final code passed `python -m pytest tests/ e2e_tests/ -q`: **4,351
passed, 34 skipped**. The complete real-PostgreSQL Hub suite passed **2,451
passed, 1 skipped**. Ruff, Node syntax checking, whitespace validation and the
production Bandit scan passed. The existing local-tier performance gate passed
with a 29.0 ms median at 6,400 lessons and a fitted total-latency exponent of 1.10.
These test results accompany the component profiles; they do not measure answer
quality against competitors.

Official dataset retrieval and locally configured repository judges are evaluated
separately from component latency. Their source hashes, question selection,
model configuration, raw scores and errors must accompany any accuracy claim.
The local Qwen2.5-1.5B judge has shown semantic grading errors, so its diagnostic
scores cannot establish superiority over published Mem0 managed-platform scores.
Large-history evidence checks have exposed missed provenance despite the latency
improvements. Held-out retrieval experiments should determine which quality
changes to keep; no benchmark answer, dataset category oracle or arbitrary
confidence threshold is used to manufacture a higher score.

[Held-out quality experiments](retrieval-quality-experiments.md) cover 333 distinct
official questions. Larger primary quotas, forced session diversity and adaptive
advice/history quotas regress on other conversations, so none changes the default.
The existing optional local Arctic arm improves one 100K conversation's evidence
recall from 68.61% to 74.49%, with unchanged complete support and substantial CPU
preparation cost. This result does not establish an overall answer-accuracy gain.
