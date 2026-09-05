# Dispatch Assignment: Survey Explorer 1 (Core CLI & Memory/Attention)

## Working Directory
/root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1

## Authoritative Request
/root/commontrace-v2/.agents/ORIGINAL_REQUEST.md

## 2026-09-05T12:37:51Z
Your identity: Survey Explorer 1 (teamwork_preview_explorer).
Your working directory is: /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1
Your parent conversation ID: bf1be23f-c3e9-4aff-850c-99fca8f4e1d9

You MUST read the verbatim original user request at:
/root/commontrace-v2/.agents/ORIGINAL_REQUEST.md

Your scope:
Deep technical survey of `commontrace/` (CLI, subcommands, config, runner, attention integration) and `memory/` (attention mechanism, indexing, query, cosine similarity, lessons, episodes, `build_index.py`, `query.py`).

Your task:
Investigate and produce an exhaustive catalog of findings in your scope across all 4 requirements:
1. Bugs & Edge Cases: Runtime errors, unhandled exceptions (e.g. missing files, corrupt JSON/YAML, empty arrays, malformed input), incorrect type conversions, protocol non-conformance.
2. Performance Bottlenecks: Redundant disk reads/writes, unmemoized calculations, slow embedding loops or unvectorized cosine similarity calculations, CLI startup or attention layer process overhead.
3. Security Vulnerabilities: Unsafe YAML deserialization (`yaml.load`), shell injection vectors (`subprocess.call(..., shell=True)`), improper path traversal protections, unvalidated external inputs.
4. Dead and Unwired Code: Unused functions, dead methods, unused internal imports, unwired subcommands or flags.

Deliverable:
Write a comprehensive handoff report at `/root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/handoff.md` with concrete file paths, line numbers, description of issue, impact, and proposed remediation.
Send a message back to parent when done.

## 2026-09-05T13:02:02Z
**Context**: Phase 0 Codebase Survey
**Content**: Checking in on your status. How is the audit of commontrace/ and memory/ progressing?
**Action**: Please report your current findings and progress, or deliver your handoff.md if complete.
