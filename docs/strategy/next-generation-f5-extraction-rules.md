# F5 extraction prompt rules

## Change

The existing `additive_extract` path already passes an observation timestamp to
its extractor and asks it to ground relative dates and preserve state changes.
This slice makes those instructions precise: the observation timestamp is the
only date anchor; exact relative terms may resolve to a calendar date, vague
terms stay approximate, and `valid_from` is emitted only when the source supports
the event date. A transition statement must retain both the prior and new state.

The prompt remains on the existing one-pass, add-only extraction path. The JSON
shape and accepted fact fields are unchanged. No operation was added and no
protocol or persistence format changed.

## Competitor evidence and adaptation

mem0's prompt gives the observation date as the sole temporal anchor and
distinguishes it from the current date (`mem0/mem0/configs/prompts.py:524-540`).
It also says that transitions need both the old and new arrangement and includes
examples (`mem0/mem0/configs/prompts.py:612-620`). This change adapts those
principles to CommonTrace's `observed_at` JSON input and `valid_from` field.

No mem0 source code or prompt text was copied verbatim. The implementation is a
small CommonTrace-specific rewrite that preserves its stricter explicit-evidence
and add-only constraints.

## Verification and limits

`tests/test_extraction_rules.py::test_transition_preserves_both_states_and_observation_date`
uses a deterministic completion stub to check that the observation timestamp
reaches the prompt, the date/transition constraints are present, and an admitted
transition retains both states and its supported `valid_from`. This is a prompt
and data-flow contract test; it does not measure a real model's extraction
accuracy or establish that the prompt alone improves quality.
