# Relevance-first prior lesson selection

EverOS at `/root/Test/EverOS`, revision
`fcd41b57cab6c577211120e166f801038d21b7ae`, keeps its skill prompt bounded
with `MAX_SKILLS_IN_PROMPT = 10`. In
`src/everos/memory/strategies/extract_agent_skill.py`,
`_select_existing_skills` hydrates the markdown source of truth and uses the
current case vector to rank skills when the cluster exceeds that cap. The same
strategy collects only the chosen skills' `source_case_ids`, deduplicates the
lineage, excludes the target case, ranks hydrated support by quality and
timestamp, and caps it at nine.

CommonTrace already preserves explicit lesson lineage: captured sessions
produce case records with `source_message_ids`; accepted lessons carry
`relations.derives`, `provenance.source_case_ids`, `source_versions`, and
`source_traces`. The reflection prompt also already caps cases at nine, prior
lessons at ten, and serialized input at 6,000 tokens. Those existing controls
made another case-id field or prompt cap redundant.

The narrower gap was prior selection. `commontrace/session_learning.py` asked
`Journal.search` for ten prior lessons using an empty query. Empty-query search
orders by record ID, so when the library grew past ten, the curator could miss
the relevant lesson it needed to update or avoid duplicating. The selection
now searches with the captured session intent, retaining the existing kind,
status, scope, and top-ten bounds. If the session has no task intent, the empty
query behavior remains the fallback.

`tests/test_session_learning.py::test_curator_prior_slice_uses_task_relevance_before_top_k`
seeds fifteen unrelated lessons ahead of a lexicographically late relevant
lesson and verifies the curator receives the relevant candidate and can update
it. `test_required_prior_evidence_cannot_be_dropped_to_fit` keeps its oversized
prior relevant to the task, so it still verifies that required evidence is
never silently dropped to fit the token budget. This change is functional
coverage only; it has no measured benchmark or model-quality claim.
