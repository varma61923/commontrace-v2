# F4 history citation verification

## Scope and current behavior

The F4 learner paths in `session_learning.py` already separate case extraction,
curation, and independent writer acceptance. They keep prior lessons and up to
nine source cases in bounded prompts, retain source-case and trace lineage,
support the typed lifecycle operations, and leave captured work retryable when
a model call or persistence step fails. The existing tests cover rejection,
retry, causal promotion and retirement, split groups, and the incremental
reflection boundary. `experience_skills.py` consolidates explicit trace cases
by procedure shape and scope, pins their source digests, and keeps raw traces
available until a paired causal review admits a skill.

The history importer records the author's role, a source digest and message
index citation, a code digest, and a coverage ledger. A code-grounded actionable
claim can seed only a quarantined candidate; review rechecks the cited code
before moving that candidate into shadow evaluation.

## Gap and change

`verify_code()` previously treated every backticked string as a code symbol,
including a source path. Thus a valid history claim such as ``Always run
`parse` in `parser.py` before changing its boundary behavior`` failed
verification unless the literal text `parser.py` also appeared inside the
source file. That prevented a common citation style from supplying eligible
evidence to cold-start import.

The parser now identifies recognized source paths first and excludes those
paths from the quoted-symbol set. It still requires every remaining quoted
symbol to occur in the cited source tree, and it retains the existing path
boundary and file-size checks. The import regression verifies the evidence and
that the resulting candidate remains quarantined; it does not claim the
candidate is causally useful.

## Competitor navigation

Graphify queries of the local Cognee and EverOS graphs surfaced the relevant
distillation and skill-consolidation paths. Inspection of Cognee's
`cognee/modules/session_distillation/distill.py` confirmed that failed curator
or writer calls must not advance its watermark. EverOS's
`src/everos/memory/strategies/extract_agent_skill.py` bounds prompts to ten
skills and nine supporting cases and carries skills through case lineage.
CommonTrace already has the corresponding failure-safe journal cursor, bounded
prior/case inputs, and source-case lineage, so this change did not create a
parallel learner path. The local Letta Graphify graph contains only README
nodes, so it did not expose the reflection implementation for source-level
comparison.

## Verification and limits

Test-first evidence: the new backticked-path regression failed before the fix
because `verify_code()` returned no evidence. After the fix:

- `/tmp/commontrace-goal-env/bin/python -m pytest tests/test_history_import.py tests/test_session_learning.py -q -p no:cacheprovider` — **36 passed**.
- `/tmp/commontrace-goal-env/bin/ruff check commontrace/history_import.py tests/test_history_import.py` — passed.
- `git diff --check -- commontrace/history_import.py tests/test_history_import.py` — passed.

These checks use deterministic local fixtures. They verify import and learner
contracts, not model quality, source attribution beyond the recorded role, or
CodingLessonBench uplift.
