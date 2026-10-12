# Google ADK lifecycle runtime check

The provider-free lifecycle test runs an actual Google ADK `LlmAgent` through
ADK's `InMemoryRunner` and official CLI `MockModel`. It wires the existing
`before_model_callback(memory)` together with the companion
`after_agent_callback(memory)` from
`commontrace.integrations.google_adk`. The first callback scopes the
request by app, user and session, captures the user turn, and adds retrieved
context to the actual ADK `LlmRequest.contents`. The second callback limits
capture to model text from the current invocation and agent, then sends the
completed result through the shared `Middleware.after_run` path. Both return
`None`, preserving ADK's normal continuation and response.

This follows the ADK `after_agent_callback` lifecycle and adapts the
session-event scan shown in the Mem0 ADK guide. CommonTrace filters by both
invocation and agent before sending only the current agent's text to
`SessionEnd`, avoiding replay of older turns or sibling-agent messages.

Validated with Python 3.12.3, `google-adk==2.11.0`, and its resolved
`google-genai==2.29.0`. Reproduce with:

```bash
python3 -m venv /tmp/commontrace-adk-live-venv
/tmp/commontrace-adk-live-venv/bin/python -m pip install 'google-adk==2.11.0' pytest
PYTHONPATH=. /tmp/commontrace-adk-live-venv/bin/python -m pytest tests/test_optional_google_adk_lifecycle.py -q
```

The test passed with one actual LlmAgent model request; the retrieved
`Use dark mode for the dashboard.` record appeared in that request, the mocked
completion returned unchanged, the journal contained the captured user message,
and `SessionEnd` held `succeeded=true` with `Fixture response`. ADK emitted
deprecation warnings from its helper and a warning about absent token usage
metadata; neither affected the run.

The official test harness and callback lifecycle are in Google's
[`agent_test_runner.py`](https://github.com/google/adk-python/blob/main/src/google/adk/cli/agent_test_runner.py)
and [`base_agent.py`](https://github.com/google/adk-python/blob/main/src/google/adk/agents/base_agent.py).
The adaptation reference is Mem0's
[`google-ai-adk.mdx`](https://github.com/mem0ai/mem0/blob/main/docs/integrations/google-ai-adk.mdx),
which scans `session.events` from its `after_agent_callback` to capture a
completed conversation.

This is a provider-free framework lifecycle pass, not a provider transport,
model-quality, multi-agent orchestration, or latency measurement. The mock
model returns a fixed response. Completion capture stores current-agent text in
the CommonTrace session journal; it does not promise extraction into durable
long-term memory before the existing learning job runs. The callback is
best-effort and leaves ADK's result unchanged if CommonTrace storage fails.
