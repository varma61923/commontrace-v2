# Project-scoped agent detection for `commontrace up`

`commontrace up --setup-only` can now find supported installed coding agents
without requiring one `--agent` option per host. Detection uses an exact,
ordered map of known CLI executable names on `PATH` and known project or user
configuration roots. It does not execute an agent, inspect conversation data,
or use agent-provided environment variables as host identity.

When `--agent` is omitted and the project has no explicit `agents` entries,
`up` writes only the repository-local hook targets for detected hosts. It
reports the detected hosts, the evidence for each detection, the selected
hosts, and hosts withheld pending opt-in. Existing named `--agent` selections
and explicit project configuration remain supported. Kimi is detected for
reporting but is withheld from automatic installation because its existing
adapter defaults to `~/.kimi-code/config.toml`; the user can name it with
`--agent kimi` or pass `--allow-global-agent-config` to allow a detected Kimi
host to receive that global hook edit. Kimi's `KIMI_CODE_HOME` override is
included in detection.

Automatic detection is installation discovery only. By default `up` keeps its
existing bounded scan of project exports under
`.commontrace/history/{claude,codex,cursor,gemini}`. Host-private Claude Code
and Codex transcripts are available only through the separate
`--import-agent-history` opt-in; see the [history onboarding note](next-generation-f4-agent-history-onboarding.md)
for attribution rules, resource bounds, and coverage reporting. Detection and
history import do not approve or activate lessons; the existing bridge only
sees lessons that already pass its admission checks.

## Competitor evidence

The supplied Hindsight coding-agent installer snapshot
(`a9eeed77f9bce99d8f97d39a30d3281747300824`) detects Cursor with either the
`cursor-agent` executable on `PATH` or an existing `~/.cursor` directory in
`hindsight-integrations/coding-agents/src/installer.ts`. Its installer tests
make `install all` an explicit target and assert that a bare install changes
nothing (`src/installer.test.ts`, “all vs named harnesses”). This uses the same
bounded signal shape, while CommonTrace writes repository hook files by
default and withholds its global-only Kimi target absent explicit opt-in.

The supplied Mem0 CLI snapshot (`b7ad69afda6b6ed030347c66d48a13e4de9dec08`)
uses a curated set of host environment variables in
`cli/python/src/mem0_cli/agent_detect.py` to decide whether the CLI is being
invoked from an agent context. The module explicitly treats this as a UX
trigger, not a verified agent identity. CommonTrace's installer needs to
discover which hook format to write, so it uses executable/configuration
evidence instead of Mem0's context-trigger pattern.

## Validation

`tests/test_up_agent_detection.py` supplies temporary PATH entries and fake
configuration roots. It verifies stable detection, project-only auto-wiring,
Kimi's withheld global target, the explicit flag route through the CLI, named
Kimi selection, the existing project-export import, and that host-private
history is not discovered. It also verifies that an oversized Kimi config is
rejected without modification. The focused bootstrap, first-rule, and
detection suites passed all 30 tests:

```bash
.venv/bin/python -m pytest tests/test_up_agent_detection.py tests/test_bootstrap_runtime.py tests/test_first_project_rule.py -q
```

Ruff, compileall, and `git diff --check` passed for the changed Python files.

The repository still cannot safely identify GUI-only installations that leave
no known config root and expose none of the mapped CLI binaries. Those hosts
remain available through explicit `--agent <name>` selection.
