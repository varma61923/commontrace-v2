# Context completeness evaluation

Date: 2026-10-12.

## Adaptation

The checked-out Zep LoCoMo evaluator (`/root/Test/zep`, commit
`cfa2ab2bbc3c7085d636479b719bcf64608162cc`) grades whether retrieved context
contains the information needed to answer the question, with `COMPLETE`,
`PARTIAL`, and `INSUFFICIENT` outcomes. Its context judge is separate from the
generated-answer judge. CommonTrace adapts that measurement shape in
`benchmarks/judges/context_completeness.py` without importing Zep code or its
prompt text.

Run it alongside any retrieval mode with:

```bash
python -m benchmarks.conversation_bench --dataset locomo --data locomo10.json \
  --budget 1500 --context-completeness --completeness-model gpt-4o
```

The option sends the question, reference answer, and retrieved context as JSON
data to the evaluator. It does not ask the evaluator to score a generated
answer. The output includes per-question grade, reasoning, present and missing
elements; overall and per-category grade rates; model latency and cost; cache
hits; and a provenance binding for the model configuration, evaluator source,
prompt profile, output limit, and bounded request helper. The option is
independent of `--answer`, which keeps answer accuracy a separate measurement.

The evaluator uses CommonTrace's cache and cost guard, including pre-dispatch
cost reservation, historical cached-cost attribution, and request output caps.
The normal offline benchmark remains model-free unless this explicit option is
enabled.

## Validation

`tests/test_context_completeness_judge.py` covers prompt data boundaries,
structured grade validation, all three labels, benchmark summary integration,
source provenance, and cache reuse. The focused context, lexical-completeness,
and measurement suites report **36 passed**. Ruff and whitespace checks pass.

No live LLM grading run was made, so this implementation establishes the
measurement path and schema only; it does not establish any context-sufficiency
rate or model agreement result.
