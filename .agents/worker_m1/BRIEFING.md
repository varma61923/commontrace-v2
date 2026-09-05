# BRIEFING — 2026-09-05T14:45:00Z

## Mission
Implement and verify all 14 assigned fixes across core CLI, trace/evidence IO, memory/attention, and write regression tests in tests/test_m1_regressions.py.

## 🔒 My Identity
- Archetype: teamwork_preview_worker
- Roles: implementer, qa, specialist
- Working directory: /root/commontrace-v2/.agents/worker_m1
- Original parent: 373e613d-5993-46fc-9aa3-f6e91cabe86a
- Milestone: M1 (Core CLI, Trace/Evidence IO & Memory/Attention)

## 🔒 Key Constraints
- Exclusively own and modify:
  - memory/attention/build_index.py
  - commontrace/reference/pilot_metrics.py
  - commontrace/trace_io.py
  - commontrace/evidence_io.py
  - commontrace/commands/taxonomy_cmd.py
  - commontrace/cli.py
  - commontrace/commands/capture_cmd.py
  - commontrace/commands/pilot_cmd.py
  - memory/attention/query.py
  - commontrace/overlap.py
  - commontrace/frontmatter.py
  - commontrace/commands/query_cmd.py
  - commontrace/commands/serve_cmd.py
  - commontrace/commands/init_cmd.py
  - tests/test_m1_regressions.py
- DO NOT modify files outside this set.
- DO NOT cheat: genuine logic, real state and real behavior.
- Backward compatibility and full test coverage.

## Current Parent
- Conversation ID: 373e613d-5993-46fc-9aa3-f6e91cabe86a
- Updated: not yet

## Task Summary
- **What to build**: Fix 14 specific functional bugs, performance bottlenecks, and dead/unwired code issues in core CLI, trace/evidence IO, memory/attention subsystems; create comprehensive regression tests in tests/test_m1_regressions.py.
- **Success criteria**: All 14 tasks implemented cleanly, tests/test_m1_regressions.py passing, full pytest suite passing with zero regressions, commontrace bench and doctor passing, handoff.md written.
- **Interface contracts**: /root/commontrace-v2/protocol/PROTOCOL.md
- **Code layout**: commontrace/, memory/, tests/

## Change Tracker
- **Files modified**: none yet
- **Build status**: untried
- **Pending issues**: none

## Quality Status
- **Build/test result**: untried
- **Lint status**: clean
- **Tests added/modified**: tests/test_m1_regressions.py (to be created)

## Key Decisions Made
- Follow minimal change principle and keep all public interfaces backward compatible.

## Artifact Index
- .agents/worker_m1/DISPATCH.md — Dispatch instructions
- .agents/worker_m1/progress.md — Liveness and progress tracking
- .agents/worker_m1/handoff.md — Final 5-component handoff report
