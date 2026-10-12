# First reviewed project rule

`commontrace up --project-rule FILE` now offers a first lesson from an empty
project without making up experience or trusting an unattended import. It copies
an explicitly supplied UTF-8 rule into a canonical **review** candidate and prints
its path, content SHA-256, and the existing approval command. The description is
the first nonempty source line, not an inferred claim. The candidate cites the
source path and full original-byte hash; trace/episode evidence stays empty.

For example, with a project rule you want the agent to follow:

```bash
commontrace up --dest ./project --setup-only --agent codex --project-rule ./rule.md
# Open the printed candidate path and review the rule and its repository scope.
commontrace lesson approve PROJECT_RULE_SLUG --dest ./project --rationale 'Reviewed this project rule.'
commontrace up --dest ./project --setup-only --agent codex --project-rule ./rule.md
commontrace up --dest ./project --agent codex
```

Use the actual slug printed by the first command. The final command runs the local
hook broker and API; the generated native SessionStart hook delivers the approved
directive through the existing receipt transport. Setup-only configures hooks and
does not start the broker. A stopped broker retains the existing capture-only
fallback. Directive injection uses the always lane and needs no model or network;
the existing context budget still applies.

Staging never calls approval. It rejects empty, over-10,000-byte, non-UTF-8,
symlink/nonregular, and content-guard-blocked sources, reusing ingestion's bounded
file reader. Repeated identical content selects the same candidate without
overwriting human edits or approval. Changed source bytes create a new review
candidate and preserve the earlier one. These candidates have no `llm_draft`
marker, so the existing draft auto-approval scan does not select them. Simply
editing their status to active does not bridge them: an explicit project rule
needs an integrity-bound approval receipt, verified by the existing admission
code. Normal human/separation policies remain in `lesson approve`.

The implementation also fixes two bugs on the canonical bridge path. The bridge
must reread full frontmatter before admission and hashing because cached ranking
metadata omits the receipt. Both bridge and HookKit now compare canonical lesson
names rather than filenames with `lesson_` and `.md`; the bridge preserves the
reviewed `lesson_kind`. Injection still rechecks the full source hash and current
receipt. Editing approved content without reapproval withholds it.

## Validation

The tests were written before implementation and initially failed because `up`
had no project-rule option. `tests/test_first_project_rule.py` starts with an empty
temporary project, stages a real file, verifies no pre-review injection, refuses
forged active status, approves through the existing CLI handler, reruns setup,
and verifies both HookKit and the generated native Codex SessionStart subprocess
deliver the approved text offline. The native test starts and stops its own local
broker. It also verifies edited approved text is withheld, repeated imports retain
review edits, source changes create review candidates, and invalid input is
rejected. No retained memory store or model is used.

```bash
.venv/bin/python -m pytest tests/test_first_project_rule.py -q
```

On 2026-10-12 all nine cases passed as part of 99 bootstrap, hook transport,
approval and admission regression checks in 8.03 seconds, including the complete
temporary-project approval/injection workflow. This is a local functional check;
it does not measure human review time, a cold installation, retrieval quality,
or downstream task success.

## Competitor evidence

The supplied Mem0 snapshot (`b7ad69afda6b6ed030347c66d48a13e4de9dec08`),
`integrations/agent-plugin-core/python/hook_runner.py:first_prompt_memory_output`, makes first
prompt recall a concrete integration step. Hindsight
(`a9eeed77f9bce99d8f97d39a30d3281747300824`), its coding-agent
`hindsight-integrations/coding-agents/src/core/session-start.ts`, checks actual bank state for cold starts and separates
user-facing onboarding from model context. These flow patterns informed the
explicit first-value path; CommonTrace reuses its own ingestion, approval,
canonical lesson, and hook transport code.

The supplied Letta snapshot (`5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a`) only
redirects to `letta-code`; its onboarding implementation is absent. Zep's supplied
snapshot (`cfa2ab2bbc3c7085d636479b719bcf64608162cc`) documents cloud signup and
SDK integrations. Neither supplied checkout provides a local human-reviewed
first-lesson implementation to adapt. No competitor rule text was imported or
activated, and no claims of measured benefit were synthesized.
