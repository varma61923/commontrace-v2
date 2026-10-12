# F4 reflection cursor audit

## Finding

CommonTrace already persisted a per-conversation reflection cursor, but its
legacy SQLite `cursors` row was keyed only by session while capture reads were
filtered by both session and scope. The public `evolve runtime learn` command
accepts a caller-supplied session ID and a separate agent/context scope, so the
same ID could be reused across scopes: sealing scope A through sequence N could
then skip an interleaved scope B capture at a lower sequence.

New watermarks are keyed by session and canonical scope JSON. `Journal.buffer()`
resumes after that scoped sequence, while `closed_boundary()` only offers a
terminal event beyond it. Existing session-only watermarks migrate to the scope
of the capture at the exact sealed sequence; other scopes replay from zero, so
ambiguous legacy history can cause duplicate work but cannot be skipped. The
legacy `seal(session, through)` call remains supported and attributes its
watermark from that capture row when available.

`session_learning.learn()` filters the buffer to a closed boundary and seals
only after a successful write, an explicit no-op/rejection, or a thin
trajectory that needs no extraction. A provider, validation, or persistence
exception exits before sealing, so the same captured work remains available for
retry. Episode and lesson identities are deterministic from the session and
source sequence, making a retry after a partial write idempotent.

This meets the F4 reflected-through requirement without storing a second
message-id cursor: journal message IDs are stable identities, and the durable
sequence maps directly to the final captured message. It also preserves later
unfinished work because only entries at or before a closed boundary enter the
reflection pass.

## Letta Code comparison

The supplied `/root/Test/letta` checkout is the archived `letta-ai/letta`
repository at `5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a`; it contains policy
and repository metadata but not the cited Letta Code implementation. I checked
the cited client source at Letta Code revision `d31b879` instead. Its
`reflection-transcript.ts` defines `ReflectionTranscriptState` with
`reflected_through_message_id`, puts each state file under an agent/conversation
directory, selects entries after that message-id anchor, and advances the
cursor only when finalization reports success. It also locks state operations
per agent/conversation. CommonTrace has the same durable per-conversation and
failure-safe boundary, using SQLite sequence watermarks and deterministic
record IDs rather than a JSON state file and message-id anchor.

Source evidence: [Letta Code reflection transcript at revision d31b879](https://github.com/letta-ai/letta-code/blob/d31b879/src/cli/helpers/reflection-transcript.ts), especially the state shape (lines 28–36), per-conversation paths (850–864), anchor selection (991–1029), and success-gated finalization (1746–1777). Its append logic skips duplicate message IDs under the same conversation lock (935–982).

## Local evidence

- `tests/test_session_learning.py::test_failed_writer_does_not_seal_and_retry_is_idempotent` verifies provider failure leaves captures pending, a successful retry creates one lesson, and another run is a no-op.
- `tests/test_session_learning.py::test_reflection_leaves_new_task_unsealed` verifies a later prompt remains pending after the preceding conversation is reflected.
- `tests/test_session_learning.py::test_reflection_cursor_is_scoped_when_session_ids_interleave` verifies a scope A no-op cannot seal an interleaved scope B prompt.
- `tests/test_session_learning.py::test_legacy_session_cursor_migrates_to_its_capture_scope` verifies an existing session-only watermark migrates without skipping another scope's older capture.

Validation: the full focused bundle `tests/test_session_learning.py
tests/test_memory_journal.py` passed (**59 passed**); after the final scope-match
guard, the cursor/retry selection passed (**4 passed, 31 deselected**). Ruff,
compileall, and `git diff --check` passed.

These deterministic tests cover cursor and retry semantics; they do not measure
the quality of model-generated reflections.
