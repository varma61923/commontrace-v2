# BRIEFING — 2026-09-05T13:38:00Z

## Mission
Audit the entire `commontrace-v2` codebase to identify and resolve all functional bugs, performance bottlenecks, security vulnerabilities, and dead/unreferenced code with full test coverage and schema conformance.

## 🔒 My Identity
- Archetype: teamwork_preview_orchestrator
- Roles: orchestrator, user_liaison, human_reporter, successor
- Working directory: /root/commontrace-v2/.agents/teamwork_preview_orchestrator_gen2
- Original parent: parent
- Original parent conversation ID: 6a8c6566-d0bb-4c10-945a-ad5c9b721af2

## 🔒 My Workflow
- **Pattern**: Project Pattern (Orchestrator Gen 2)
- **Scope document**: /root/commontrace-v2/PROJECT.md
1. **Decompose**: Survey findings merged into PROJECT.md feature/defect inventory; decomposed into cohesive module milestones.
2. **Dispatch & Execute**:
   - Step 0: Complete Survey (Explorer 2 completion on Hub/Commons/Protocol)
   - Step 1: Synthesize into PROJECT.md and decompose into milestones
   - Step 2: Milestone Iteration Loops: Explorer → Worker → Reviewer(s) → Challenger(s) → Forensic Auditor → Gate
   - Step 3: Final Verification: full pytest, bench, doctor, schema conformance
3. **On failure**: Retry → Replace → Skip (non-critical) → Redistribute → Redesign → Escalate
4. **Succession**: Spawn successor at 16 spawns after active subagents complete.
- **Work items**:
  1. Survey Step 0 (complete Explorer 2 on Hub, Commons, Protocol/Schemas) [in-progress]
  2. Synthesize findings into PROJECT.md and decompose milestones [pending]
  3. Milestone Iteration Loops [pending]
  4. Final E2E verification & Forensic Audit [pending]
- **Current phase**: Phase 0 / Survey completion
- **Current focus**: Awaiting Explorer 2 survey handoff

## 🔒 Key Constraints
- DISPATCH-ONLY orchestrator: NEVER write source code directly, NEVER run build/test commands directly.
- All investigation at code level delegated to subagents.
- File-editing tools only for metadata/state files (.md) in .agents/.
- Forensic Auditor INTEGRITY VIOLATION is a binary non-negotiable veto.
- Include ORIGINAL_REQUEST.md path in every dispatch.
- Mandatory integrity warning in all Worker prompts.
- Never reuse subagents after handoff.
- Self-succeed at 16 spawns.

## Current Parent
- Conversation ID: 6a8c6566-d0bb-4c10-945a-ad5c9b721af2
- Updated: 2026-09-05T13:35:00Z

## Key Decisions Made
- Inherited Explorer 1 handoff (8 bugs, 6 perf bottlenecks, 4 unwired code items).
- Inherited Explorer 3 handoff (benchmark collision, TOCTOU, naive datetimes, test coverage gaps).
- Spawned fresh Explorer 2 (`ed750d72-16d2-4b12-8dc8-2901f32f7d90`) to complete Hub, Commons, and Protocol/Schemas audit.

## Team Roster
| Agent | Type | Work Item | Status | Conv ID |
|-------|------|-----------|--------|---------|
| explorer_survey_2 | teamwork_preview_explorer | Hub/Commons/Protocol survey | in-progress | ed750d72-16d2-4b12-8dc8-2901f32f7d90 |

## Succession Status
- Succession required: no
- Spawn count: 1 / 16
- Pending subagents: ed750d72-16d2-4b12-8dc8-2901f32f7d90
- Predecessor: teamwork_preview_orchestrator_1 (bf1be23f-c3e9-4aff-850c-99fca8f4e1d9)
- Successor: not yet spawned

## Active Timers
- Heartbeat cron: 5f20407e-a93e-4af7-ba44-ecf1d3bff56b/task-36
- Safety timer: none

## Artifact Index
- /root/commontrace-v2/.agents/ORIGINAL_REQUEST.md — Authoritative user requirements
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_1/handoff.md — Survey report 1 (Core CLI & Memory)
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_3/handoff.md — Survey report 3 (Benchmark, Tests, Security, Dead code)
- /root/commontrace-v2/.agents/teamwork_preview_explorer_survey_2_gen2/handoff.md — Survey report 2 (Hub, Commons, Protocol)
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_gen2/plan.md — Orchestrator plan
- /root/commontrace-v2/.agents/teamwork_preview_orchestrator_gen2/progress.md — Progress log & heartbeat
