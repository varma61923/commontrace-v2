# WS6: Foresight and sleep-time compute

**Problem.** Useful consolidation (mental models, foresight notes) should happen between tasks, not on the request path.

**Design.** Standing questions and mental models refreshed by background jobs (`memory_control.py`, `jobs.py`), `dream` offline passes, background ingestion with graceful drain.

**Invariants touched.** Foresight notes are drafts until reviewed; background jobs never write active lessons.

**Tests.** `tests/test_jobs*.py`, `tests/test_background_ingest.py`, `tests/test_memory_evolution.py`.

**Benchmark to move.** Recall quality on questions answered by refreshed models.

**Risks.** Background compute costs; refresh intervals are bounded (1 s to 1 year).

**Rollback.** Jobs are idempotent and can be stopped; nothing they produce is active without review.
