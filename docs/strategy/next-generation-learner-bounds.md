# Evidence-preserving learner input bounds

The D2/D4 audit at `7e0a224` found that oversized captures were replaced with a
truncated JSON string before distillation. The size estimate excluded the list
and instruction wrapper, and JSON escapes could enlarge that string again.
Curator and arena preselection subsequently reintroduced the original captures
without any input-size check. A successful no-op could therefore seal work the
model had never received in full.

Keep the existing segment → case → curator → writer path. Check the complete
serialized prompt before every model call. Use UTF-8 bytes as a conservative
text-token upper bound by default; callers can supply their model's text-token
counter. This accounts for JSON, instructions, Unicode and escaping, and does
not claim to measure provider chat-template or system-prompt overhead. Keep
the 32-message, 10-prior-lesson and 9-case caps. Preselection is supplemental:
pack only complete selected captures into the remaining curator budget after
all offered cases and prior lessons. Arena source IDs must name only messages
actually supplied to both extractors.

Do not truncate an indivisible capture or required case/prior/proposal payload
to force a fit. Raise before dispatch and leave the capture cursor unchanged;
the durable worker can retry, and an explicit caller may choose a larger finite
budget or exact counter. Raw captures remain in the WAL journal. This favors
an observable retryable failure over silently marking unseen evidence distilled.
The default durable worker retries this failure and eventually parks it in the
existing dead-letter state. An oversized head capture blocks later captures in
that session until an explicit caller supplies a suitable finite cap/counter.
Semantic fragmentation of an indivisible tool output is not implemented; such
outputs must not be described as successfully distilled. This closes the
input-bound and lost-evidence defects, not all D2 acceptance requirements.

The design adapts the successful-work watermark rule inspected in cognee
`0ec7a9fa61c9ff04bf7e02e0d57af363a993a5e1`,
`cognee/modules/session_distillation/distill.py`, and the bounded lineage/input
selection inspected in EverOS `fcd41b57cab6c577211120e166f801038d21b7ae`,
`src/everos/memory/strategies/extract_agent_skill.py` and
`src/everos/service/_boundary.py` (both Apache-2.0). No source is copied. The
checked-out `/root/Test/letta` is the retired Letta server repository at
`5bcdd177d70fa2b31a754cfcd801e77b2e1ab16a`, not the brief's `letta-code`
`d31b879`; its reflection source is unavailable locally and was not claimed as
inspected or adapted. Graphify's existing source graph confirmed the shared
segment/learn path and the direct arena/preselector path before editing.

Validation and before/after observations are appended after the tests run.
These contracts do not establish SWE uplift, production extraction utility,
model-token usage, or the harmful-lesson retirement gate.

## Observed validation

The entire learner contract file passes: **32 passed in 93.23 seconds**. The
specific CASE wrapper regression also failed first on the prior implementation
and passes after reserving the complete outcome-payload size during segment
packing. Ruff passes for the learner runtime and tests. The scope remains bounded
input integrity; it does not satisfy the broader lesson learner, causal lift,
import, or DPO gates.

```sh
python -m pytest tests/test_session_learning.py -q
python -m ruff check commontrace/session_learning.py tests/test_session_learning.py
```
