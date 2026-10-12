# F4 Cursor and Gemini history scope

`commontrace up --import-agent-history` now emits a durable coverage record for
Cursor and Gemini alongside the Claude Code and Codex scans. The records are
explicitly unsupported and do not enumerate host-private paths: the supplied
competitor sources do not establish an authoritative per-session project cwd
for either host. Their candidate count is stored as unknown (`null`), with
`candidate_count_known: false`, so a skipped scan cannot be mistaken for an
empty history store. Project exports under `.commontrace/history/` remain
available through the existing import path.

Cursor has direct negative evidence in the supplied Hindsight checkout:
`/root/Test/hindsight/hindsight-integrations/coding-agents/src/core/history.ts`
describes Cursor history as SQLite-backed with an unversioned internal schema,
and dispatches `cursor-cli` to the unsupported result. Its adjacent
`history.test.ts` asserts that the importer reports SQLite-backed harnesses as
unsupported. That source gives no stable session-file layout or verified
project-to-store mapping. CommonTrace therefore does not probe candidate
databases or infer ownership from a workspace identifier.

For Gemini CLI, the supplied checkouts provide hook support but no cold-history
reader that records an authoritative `cwd`. Hindsight's installer notes that
Gemini CLI was renamed to Antigravity's `agy` and keeps an internal
`antigravity-cli` identity; its history importer has no Gemini or Antigravity
reader. The local Graphify implementation at
`/root/Test/graphify/graphify/install.py` installs a project-scoped
`BeforeTool` hook in `.gemini/settings.json`; it does not describe a past
transcript store or an attribution field. Hindsight's Qwen transcript reader
uses a Gemini-shaped `message.parts[]` body, but explicitly documents a Qwen
envelope and Qwen storage path, so that body shape is not evidence for a Gemini
CLI history layout.

The coverage blocker strings are intentionally actionable: Cursor needs a
supported file/export format with a recorded cwd or a documented,
version-stable store mapping; Gemini needs a competitor or upstream format
source showing where history lives and a per-session cwd field. Until then,
the importer fails closed and preserves the unsupported status in the same
coverage ledger used by bounded Claude/Codex discovery.

## Validation

`tests/test_up_agent_history_import.py::test_up_keeps_private_history_opt_in_and_imports_only_cwd_attributed_sessions`
checks the CLI's four coverage records and verifies that Cursor/Gemini's
unsupported status, blocker, and unknown candidate count persist in the
journal. `tests/test_up_agent_history_import.py::test_cursor_and_gemini_history_discovery_fail_closed_without_project_cwd`
checks that discovery returns no sources and writes the exact blocker to the
coverage ledger without claiming a zero-candidate scan.
