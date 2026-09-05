# BRIEFING — 2026-09-05T13:22:30Z

## Mission
Audit and remediate the entire commontrace-v2 codebase for bugs, performance bottlenecks, security vulnerabilities, and dead/unwired code with zero regressions.

## 🔒 My Identity
- Archetype: teamwork_preview_orchestrator
- Roles: orchestrator, user_liaison, human_reporter, successor
- Working directory: /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1
- Original parent: sentinel
- Original parent conversation ID: 6a8c6566-d0bb-4c10-945a-ad5c9b721af2

## 🔒 My Workflow
- **Pattern**: Project
- **Scope document**: /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/PROJECT.md
1. **Decompose**: Survey codebase with parallel explorers, build feature and defect inventory in PROJECT.md, partition into milestones (or execute milestones).
2. **Dispatch & Execute**:
   - **Direct (iteration loop)**: Explorer → Worker → Reviewer + Challenger + Auditor gate
3. **On failure**: Retry → Replace → Skip → Redistribute → Redesign → Escalate
4. **Succession**: At 16 spawns, write handoff.md, spawn successor
- **Work items**:
  1. Survey & Feature Inventory [in-progress]
  2. Decomposition & Milestones [pending]
  3. Execution & Verification [pending]
  4. Final Verification & Audit [pending]
- **Current phase**: 0 (Survey)
- **Current focus**: Survey codebase across all 6 areas

## 🔒 Key Constraints
- NEVER write, modify, or create source code files directly.
- NEVER run build/test commands yourself — require workers to do so.
- NEVER investigate or explore the problem at the code level — dispatch Explorers for technical investigation.
- Use file-editing tools ONLY for metadata/state files (.md) in your .agents/ folder.
- Never reuse a subagent after it has delivered its handoff — always spawn fresh.
- Binary veto on integrity violation from forensic auditor.

## Current Parent
- Conversation ID: 6a8c6566-d0bb-4c10-945a-ad5c9b721af2
- Updated: not yet

## Key Decisions Made
- Initiating Step 0 (Survey) with 3 parallel Explorers covering all modules in scope.
- Explorer 1 completed survey of CLI and Memory/Attention with 8 bugs, 6 performance bottlenecks, 4 unwired code items.

## Team Roster
| Agent | Type | Work Item | Status | Conv ID |
|-------|------|-----------|--------|---------|
| explorer_survey_1 | teamwork_preview_explorer | Survey CLI & Memory/Attention | completed | 18adc000-05af-4911-902b-a66a7ac8013f |
| explorer_survey_2 | teamwork_preview_explorer | Survey Protocol, Hub & Commons | in-progress | 38ffbd79-5622-4dad-92f2-a546cb443e5c |
| explorer_survey_3 | teamwork_preview_explorer | Survey Benchmark, Tests, Security & Dead Code | in-progress | 8bcf09f2-8d47-4988-b506-aad4c599baeb |

## Succession Status
- Succession required: no
- Spawn count: 3 / 16
- Pending subagents: 38ffbd79-5622-4dad-92f2-a546cb443e5c, 8bcf09f2-8d47-4988-b506-aad4c599baeb
- Predecessor: none
- Successor: not yet spawned

## Active Timers
- Heartbeat cron: task-26
- Safety timer: none
- On succession: kill all timers before spawning successor
- On context truncation: run manage_task(Action="list") — re-create if missing

## Artifact Index
- /root/commontrace-v2/.agents/ORIGINAL_REQUEST.md — User request
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/DISPATCH.md — Incoming message log
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/BRIEFING.md — Working memory
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/progress.md — Liveness & checkpoint
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/plan.md — Execution plan
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_1/PROJECT.md — Global project plan & inventory
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/handoff.md — Explorer 1 findings
