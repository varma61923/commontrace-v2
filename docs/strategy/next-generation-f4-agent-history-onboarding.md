# Explicit Claude Code and Codex history onboarding

`commontrace up --import-agent-history` adds an explicit cold-start path for
host-local Claude Code and Codex history. Without this flag, `up` continues to
read only project exports under `.commontrace/history/` and configured history
sources. It does not inspect a user's host-private transcript directories as a
side effect of agent detection or hook installation.

The import reuses `history_import.import_history`: each message remains a
quarantined claim with its author role, source digest and message-index
citation, and verified code paths carry the current file digest as evidence.
When the source is a discovered host transcript, the citation and per-file
coverage record also hold its path relative to the home directory. Only
user- and assistant-authored text may seed a lesson candidate; system and tool
messages remain attributable claims. A seeded candidate stays quarantined and
still requires the existing review and outcome path.

The scanner recognizes Claude's encoded project-directory names and reads
top-level `.jsonl` files in matching project directories; it does not recurse
into nested Claude transcript directories. Codex rollout files are found
recursively under its date-partitioned sessions root. These layouts only
narrow the candidates: the `cwd` recorded inside each transcript must resolve
to the selected project or one of its descendants. It does not infer
ownership from a directory name. Symlinks are not followed. Each opt-in run
writes a per-agent discovery ledger
with examined entries, candidate and attributed files, imported message counts,
skips, limits, and per-file coverage IDs. Per agent, the bounded scan examines
at most 20,000 directory entries, imports at most 128 files and 100 MiB total,
and reads no file larger than 20 MiB. Attribution reads at most a 1 MiB prefix
per candidate, with a 32 MiB aggregate probe budget. The scan stops and marks
coverage partial when that budget is exhausted, reporting candidates left
unprobed. Ledger status also reports skipped or unattributed files; it does
not claim to have imported all history on the machine.

## Competitor navigation

The supplied Hindsight coding-agent importer at
`/root/Test/hindsight/hindsight-integrations/coding-agents/src/core/history.ts`
provides the local path precedent: Claude candidates are selected from the
encoded project path and confirmed using transcript `cwd`; Codex candidates
are read from `~/.codex/sessions` and attributed from `session_meta.payload.cwd`.
Its adjacent `history.test.ts` fixtures cover sibling-project rejection,
Codex cwd matching, malformed headers, and bounded header reading. The
transcript parser cases in `transcript-codex.test.ts` also distinguish user
text from developer instructions and injected context.

The supplied `/root/Test/letta` checkout is the retired API repository; its
README directs active Letta Code source to a separate repository, and its
Graphify index contains README-only code context. The CommonTrace importer
already had the relevant authorship, evidence, citation, and per-file coverage
records, so this addition keeps that record model and adds the missing
opt-in discovery and scan-coverage layer rather than claiming to port source
that is absent from the supplied checkout.

## Validation

`tests/test_up_agent_history_import.py` runs the real `up --setup-only` CLI
against isolated Claude and Codex layouts. It proves that the default run does
not import private histories, the opt-in imports only transcripts whose
recorded cwd belongs to the project, author/evidence/citation fields survive,
and scan coverage is persisted. It also checks that an entry/file cap is
reported as partial. `tests/test_history_import.py` verifies that system text
cannot seed a lesson candidate even when its quote happens to match current
code. These tests do not claim support for other agents' history stores or
measure downstream lesson utility.
