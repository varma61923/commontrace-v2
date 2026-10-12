# CrewAI runtime validation

The CrewAI kickoff callbacks now have a provider-free live lifecycle check in
[`tests/test_optional_crewai_lifecycle.py`](../../tests/test_optional_crewai_lifecycle.py).
It constructs a real CrewAI `Agent`, `Task`, and `Crew`, and runs both
`Crew.kickoff()`, `Crew.kickoff_async()`, and native `Crew.akickoff()` with a
deterministic custom `BaseLLM`. Retrieved memory reaches the model prompt, the
callbacks receive CrewAI's kickoff input and output shapes, and every run
sends the expected successful result through `SessionEnd`.

The run used Python 3.12.3 and `crewai==1.15.27` in
`/tmp/commontrace-crewai-live-venv`. Pytest 9.1.1 came from the existing
`/tmp/commontrace-goal-env`; no packages were downloaded for the test runner.
The exact command was:

```sh
timeout --signal=TERM --kill-after=2s 20s env \
  PYTHONPATH="/tmp/commontrace-crewai-live-venv/lib/python3.12/site-packages:$PWD" \
  /tmp/commontrace-goal-env/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_optional_crewai_lifecycle.py
```

Result: `1 passed`, exit code 0. The installed CrewAI release emits deprecation
warnings for agent fields and Pydantic warns that bound callbacks cannot be
serialized for checkpointing.

The test makes `MemoryClient.turn()` deterministic and delays its synchronous
callback body by 30 ms. During `kickoff_async()`, a scheduled heartbeat runs
and the async ticker advances through both callbacks. During native
`akickoff()`, the heartbeat does not run during the before callback, and the
ticker does not advance during `SessionEnd`. This confirms the synchronous
callbacks can block native `akickoff()` while CrewAI's `kickoff_async()` runs
them off-loop. The test disables CrewAI telemetry before importing the optional
SDK because its 1.15.27 atexit exporter otherwise waits on a stopped executor;
the test restores the caller's environment afterward.

The model and gateway are test doubles: this verifies the installed CrewAI
lifecycle and CommonTrace adapter boundary, but it does not measure provider
transport, a live gateway, cold startup, or loaded-system latency. Zep's
[CrewAI integration](../../../zep/integrations/crewai/python)
uses explicit `search()` and `save()` storage operations and tests that
storage failures do not escape. CommonTrace retains the same fail-open goal
while routing kickoff retrieval and completion through its existing scoped
turn and `SessionEnd` paths.
