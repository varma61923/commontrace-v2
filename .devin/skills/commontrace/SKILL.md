---
name: commontrace
description: "Provider-agnostic double-review agent pipeline with long-term memory. Delegates a task to Implementer sub-agent A (immediate commit), then to independent Reviewer sub-agent B; iterates on gaps (max 3 rounds, then orchestrator arbitration). Memory: Alpha retrieves past lessons before coding; Omega + Lambda extract and validate new lessons after. Use for architectural code, heavy refactors, GPU ports, and critical fixes. Invocation: /commontrace <task description + success criteria>."
triggers:
  - "/commontrace"
  - "commontrace"
  - "/justdoit"
  - "double review"
  - "commontrace this task"
  - "launch A+B on"
---

# commontrace — Double-Review Pipeline with Long-Term Memory

This Devin skill wraps the full `commontrace` pipeline. For the canonical
spec (phases, agent briefs, memory format), read `SKILL.md` at the project root.

## Setup

1. Run `install.sh` (or `install.sh --dest <path>`) from the project root.
2. Scripts auto-detect their root from their own file location.
3. Only set `COMMONTRACE_ROOT` if you need to override the auto-detected path. The legacy `JUSTDOIT_ROOT` variable still works for backward compatibility.
4. Invoke: `/commontrace <task>`

## Reference files

| File | Purpose |
|---|---|
| `../../SKILL.md` | Full pipeline specification |
| `../../AGENTS.md` | Agent-facing guidance |
| `../../README.md` | Quick-start and config reference |
| `../../requirements.txt` | Python dependencies |
| `../../install.sh` | Setup script |
