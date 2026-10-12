# Native hook accepted-request capture recovery

The native bridge already launched a detached capture-only writer when it could
not connect to the local broker. It did not launch that writer after a broker
accepted the request but returned malformed data or stopped responding. A host
could therefore receive an empty, fail-open result while the event never reached
the SQLite journal.

The bridge now gives each native invocation a stable event ID when the host did
not supply one. Any failed broker exchange starts the existing detached writer
with the same normalized payload and ID. `Journal.capture()` treats the retry as
idempotent when the broker committed first; if the broker did not commit, the
detached writer persists the event. Delivery acknowledgements remain tied to a
successfully written host response, so capture recovery cannot fabricate an
injection exposure.

The supplied Mem0 `agent-plugin-core` provided two useful reliability patterns.
Its [`flush_worker.py` at `73e7b876`](https://github.com/mem0ai/mem0/blob/73e7b8763a81f3fa43c6b79312ad049f3106498d/integrations/agent-plugin-core/python/flush_worker.py)
reads a persisted handoff in a detached process, removes the handoff only after a
successful checkpoint, and restores a failed `.running` handoff for retry. Its
[`test_spool_delivery.py` at `3362999`](https://github.com/mem0ai/mem0/blob/33629990956ca6336501deea2e9cd625e5fe09af/integrations/agent-plugin-core/tests/test_spool_delivery.py)
tests partial batch progress so already delivered telemetry is not resent, as
well as retained retries after failures. That telemetry spool is a different
data path from agent conversation capture; CommonTrace keeps its own SQLite
journal and uses the existing capture-only writer instead of adapting Mem0's
file spool.

The regression uses a local broker peer that reads the request and then returns
malformed JSON. It also covers a stalled accepted request and both sides of the
crash window: no broker commit, and a broker commit immediately before failure.
Each case leaves exactly one capture in the journal. See
`tests/test_hook_service.py::test_native_bridge_recovers_capture_after_accepted_broker_failure`.

Focused validation: `tests/test_hook_service.py` passes (7 tests); Ruff and the
repository documentation check pass. This closes capture recovery for failed
broker exchanges; it does not claim protection against host input rejected at
the native frame-size limit or failure to start the detached process itself.
