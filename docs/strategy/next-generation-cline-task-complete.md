# Cline TaskComplete lifecycle hook

CommonTrace already serializes Cline task start, resume, prompt, tool, and
cancel hooks through the shared native hook broker, but its Cline event map did
not install `TaskComplete`. That left successful task endings without the
terminal capture and session-learning enqueue that the shared `SessionEnd`
event provides.

The Cline serializer now maps `TaskComplete` to CommonTrace `SessionEnd`. The
normalizer retains the task ID as the session ID, and the broker captures the
terminal event and queues session learning. It does not infer a successful
causal outcome from the event name; `HookKit` records an outcome only when the
host supplies an explicit boolean `succeeded` value.

The local Hindsight Cline integration provides supporting precedent: its
standalone adapter includes `TaskComplete` as a lifecycle event and its handler
retains the accumulated task transcript at that point
(`hindsight-integrations/cline/hindsight_cline/hooks/TaskComplete:1-9`,
`.../hooks/lib/hooks_impl.py:88-120`). Its newer coding-agent installer labels
the old file-hook approach as legacy, so this is evidence for the event's
lifecycle role rather than a claim that the old installation method is current
(`hindsight-integrations/coding-agents/src/installer.ts:1538-1549`).

Graphify's CommonTrace graph connects the native serializer to the shared
`HookKit` and journal path. That confirmed the change belongs in the host event
map rather than a second capture pipeline. The graph predates this worktree's
latest edits; the focused tests are the current behavioral evidence.

The regression first failed because `TaskComplete` was absent from the Cline
map. It now checks that installation creates the executable hook, that the
event resolves to `SessionEnd`, and that task cancellation or completion does
not fabricate an outcome. Verification:

```sh
env -u COMMONTRACE_ROOT -u JUSTDOIT_ROOT \
  /tmp/commontrace-goal-env/bin/python -m pytest \
  tests/test_native_hooks.py tests/test_hook_kit.py tests/test_hook_bridge.py \
  -q -p no:cacheprovider
```

This verifies serialization, broker behavior, and bridge transport with local
fixtures. Cline itself is not installed here, so host-level discovery and
execution remain unverified.
