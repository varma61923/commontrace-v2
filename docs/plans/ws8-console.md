# WS8: Product experience (Learning Ledger console)

**Problem.** Operators need to see what memory is worth, what changed and what to do next, without a build step.

**Design.** The no-build console (`commontrace/ui/`) with overview, Needs Attention review, live activity, command center and the Learning Ledger: proven value at the lower bound with a lift-per-token frontier, release diffs, an experiment designer, occasion forensics and a weekly digest (`ledger_views.py`, `/v1/ledger/*`).

**Invariants touched.** Every dynamic value is rendered as text (no innerHTML). Review actions are revision-checked. A response fetched for another tab is never rendered as the current one.

**Tests.** `tests/test_ui_browser.py` (Playwright at phone and desktop widths), `tests/test_ledger_views.py`.

**Benchmark to move.** Time for an operator to find a harmful memory and act on it.

**Risks.** Large stores make some views slow; reports are memoized for 5-60 s.

**Rollback.** The console is read-mostly; actions need `--allow-approval`.
