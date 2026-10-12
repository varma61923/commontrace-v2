# Codex post-tool failure injection

Codex exposes `PostToolUse`, not a separate `PostToolUseFailure` hook. Its
post-tool payload includes `tool_response`; local Mem0 adapter code classifies
failures from `exit_code`, `isError`, or `success` in that result. CommonTrace
previously registered the unsupported canonical event name and treated every
Codex post-tool event as successful, so tool-error lessons could not match.

The Codex serializer emits one supported `PostToolUse` entry for post-tool
processing and omits the unsupported canonical failure key. At the adapter
boundary, an explicit failed result becomes the canonical
`PostToolUseFailure` event and is passed to the shared injector. Successful or
unstructured outputs remain `PostToolUse`; an unknown outcome is not guessed
to be a failure. The native reply keeps the host event name `PostToolUse` so
Codex can accept its additional context.

Evidence and behavior references:

- The local Mem0 Codex event list uses `PostToolUse` and its adapter checks
  `tool_response.exit_code`, `tool_response.isError`, and
  `tool_response.success` (`/root/Test/mem0/integrations/codex-plugin/hooks/hooks.json`,
  `/root/Test/mem0/integrations/codex-plugin/hooks/adapter.py`).
- Current Codex hook documentation lists `PostToolUse` as the event that runs
  after tool output and accepts `hookSpecificOutput.additionalContext`:
  [Codex Hooks](https://learn.chatgpt.com/docs/hooks).
- `tests/test_bootstrap_runtime.py` verifies failure injection plus successful
  and unknown outcomes. Its first run failed on the unsupported event and
  unclassified failure payload; the final adapter-focused suite passes 68 tests:
  `/tmp/commontrace-goal-env/bin/python -m pytest tests/test_bootstrap_runtime.py tests/test_hook_bridge.py tests/test_hook_kit.py tests/test_native_hooks.py -q -p no:cacheprovider`.
