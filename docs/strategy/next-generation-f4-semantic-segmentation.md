# F4 opt-in semantic session segmentation

EverOS at `/root/Test/EverOS`, revision
`fcd41b57cab6c577211120e166f801038d21b7ae`, dispatches buffered conversation
messages to a boundary detector before extracting memory cells. Its
`src/everos/service/_boundary.py` selects a chat or agent detector, keeps the
agent path's tool items, and writes the detector's unconsumed tail back to the
buffer. The call to the chat detector passes configured hard token and message
limits; the adjacent agent-path comment documents that its detector does not
expose those limits.

CommonTrace already had an opt-in JSON boundary detector in
`commontrace/session_learning.py::segment`, bounded by the serialized prompt
budget and 32-message cap. It validates returned indices, keeps every capture
in source order, and advances the scoped reflection cursor only after the
existing learning path completes. Background learning already enables this
mode. This change exposes it to operators as
`commontrace evolve runtime learn --session-id ID --semantic-segments`; omitting
the flag preserves the prior deterministic batching path.

The boundary call now uses the distinct `session-segmentation` LLM purpose.
Configured purpose-specific model routes and the session token ledger can
therefore distinguish segmentation cost from reflection and case extraction.
The change does not alter the existing prompt budget, boundary validation, or
cursor sealing behavior.

Deterministic coverage is in `tests/test_session_learning_cli.py`: the CLI
passes `False` by default and `True` only when requested, while a scoped
`LLMRuntime` test verifies the purpose-specific model and ledger occasion.
These tests verify routing and attribution, not model boundary quality or
latency.
