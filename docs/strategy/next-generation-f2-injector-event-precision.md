# F2 event-scoped injector relevance fixture

This measurement extends the labelled injector component fixture from
`UserPromptSubmit` to `PreToolUse`, `PostToolUseFailure`, and `PreCompact`. It
checks the shared HookKit admission path with event-shaped payloads and reports
retrieval quality and warm latency separately for each event. These are
maintainer-labelled component observations. They do not estimate production
precision, prove host delivery, or establish coding-task lift.

The shared event path builds its search text from prompt/query, tool name and
input, and error fields. It applies the record's event, tool, error, and path
triggers before relevance selection. Semantic candidates must also share at
least one informative lexical term with the event query; common stopwords and
the temporal connectors `before` and `after` do not count. This prevents a
generic timing word from admitting an otherwise unrelated lesson while leaving
the existing dense and sparse ranking thresholds intact. `PreCompact` can
re-offer eligible items at compaction. This fixture sends a synthetic prompt
field for its compact cases; it does not claim to parse or retrieve from host
transcript payloads. CommonTrace's native normalizer and Claude event serializer
are covered by focused tests alongside the semantic admission cases.

## Fixture and result

`benchmarks/fixtures/injector-labels.json` keeps each event's inputs and
maintainer-assigned relevant lesson IDs together. `UserPromptSubmit` has 14
unique cases; each of the other events has five: three labelled relevant and
two negative cases. The run used 20 repetitions, so the per-event evaluation
counts below are repeated measurements of those same five or fourteen cases,
not independent production examples. Recall is correct injected lesson IDs
divided by labelled relevant IDs; precision is correct injected IDs divided by
all injected IDs.

| Event | Unique cases | Expected relevant injections | Correct | Injections | Precision | Recall | Warm p50 | Warm p95 | First core call |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `UserPromptSubmit` | 14 | 200 | 200 | 200 | 1.000 | 1.000 | 3.84 ms | 9.05 ms | 47.83 ms |
| `PreToolUse` | 5 | 60 | 60 | 60 | 1.000 | 1.000 | 4.07 ms | 11.97 ms | 38.90 ms |
| `PostToolUseFailure` | 5 | 60 | 40 | 40 | 1.000 | 0.667 | 2.66 ms | 6.84 ms | 24.73 ms |
| `PreCompact` | 5 | 60 | 60 | 60 | 1.000 | 1.000 | 3.84 ms | 7.03 ms | 29.43 ms |

Across the repeated cases, precision was 1.000 and recall was 0.947 (360
correct of 380 expected relevant injections; 360 total injections). There were
no HookKit timeouts. The warm p95 for every event was below the 50 ms target in
this run. The cache-cold `UserPromptSubmit` sample was 47.83 ms; cold calls are
reported separately and are not included in the warm percentile. The measured
core call excludes interpreter startup and model download.

The topical-overlap gate removed the `PreToolUse` false positive: the validation
command shared only `before` with the pytest lesson, while it shared topical
terms with the labelled validation lesson. All 60 labelled `PreToolUse`
injections remained and fixture precision increased from 0.750 to 1.000.
`PostToolUseFailure` still missed 20 labelled relevant injections. The five
unique examples per event are too small to support tuning a production
decision threshold or estimating production precision.

The safety harness still uses adversarial constant vectors, so retrieval misses
cannot count as safety successes. It exercised six fixed poison strings through
each of the four semantic events plus five non-active status cases: zero
successful poison injections, and the clean utility directive was admitted for
all four events. This preserves the existing security assertions while adding
event coverage; it does not promise resistance to new attacks.

## Reproduction and source identity

The observed run used FastEmbed 0.8.0 with
`BAAI/bge-small-en-v1.5` revision
`aa8f8b060edb00e03bfdd08813a2949946c8ba55`, offline and with the model already
cached. The local command was:

```sh
FASTEMBED_CACHE_PATH=/tmp/commontrace-fastembed-cache COMMONTRACE_OFFLINE=1 \
  .venv/bin/python -m benchmarks.injector_bench \
  --model fastembed:BAAI/bge-small-en-v1.5 --repetitions 20 \
  --out /tmp/injector-event-evaluation-f2-topical-20.json
```

The run bound these source bytes:

| Input | SHA-256 |
|---|---|
| `benchmarks/injector_bench.py` | `b23cdebad7ea2dcc0124dec7bfaa4dadb5735941e0776ffa27ecd04e45444235` |
| `benchmarks/fixtures/injector-labels.json` | `e806649518955a24abb77eb770eca2e84ecc0b649cff4ac312f66e87f752e12a` |
| `commontrace/hook_kit.py` | `c43448d328f67633685fcc72ce25eb7910651d6bd5d82e05ecfeb2ae4fef1776` |
| `commontrace/native_hooks.py` | `e44d0c0a5bb1883995cf2dffcb241fb20b370ad18b619ae9ce542ed5e000e2ba` |

The source-bound JSON report contains every per-event latency sample and the
aggregate safety results; it was kept in `/tmp` for this run rather than added
to the repository. The benchmark emits a fresh report at the path passed to
`--out`.

## Competitor implementation review

Graphify queries mapped CommonTrace's event path to `HookKit.handle`, and the
Mem0 and Hindsight graphs to their hook adapters and recall handlers. Source
inspection shows that Mem0's Kimi adapter emits recalled context on
`UserPromptSubmit` and keeps tool failure/compaction as separate lifecycle
events; this supports preserving event boundaries, but does not implement
PreToolUse filtering. No Mem0 implementation code was copied. See
[`/root/Test/mem0/integrations/kimi-plugin/hooks/adapter.py`](/root/Test/mem0/integrations/kimi-plugin/hooks/adapter.py)
and
[`/root/Test/mem0/integrations/kimi-plugin/tests/test_kimi.py`](/root/Test/mem0/integrations/kimi-plugin/tests/test_kimi.py).

Hindsight's Codex recall hook exposes `filter_by_min_scores`, which applies
configurable semantic/reranker score floors after recall and lets results with
missing scores pass through. That separate admission stage informed this
change's topical-evidence gate; CommonTrace retains its current ranker and only
rejects semantic candidates whose sole overlap is generic. Hindsight's tests
also use an isolated hook runner and assert the exact `hookSpecificOutput`
event envelope, which informed CommonTrace's serializer tests. See
[`/root/Test/hindsight/hindsight-integrations/codex/tests/test_hooks.py`](/root/Test/hindsight/hindsight-integrations/codex/tests/test_hooks.py).
The score-floor implementation is in
[`/root/Test/hindsight/hindsight-integrations/codex/scripts/recall.py`](/root/Test/hindsight/hindsight-integrations/codex/scripts/recall.py).

EverOS keeps its Claude memory injection registered on `UserPromptSubmit` and
separates other lifecycle callbacks; its benchmark profile tests distinguish
positive fetched context from empty context. That review supported treating
host event boundaries explicitly and retaining negative fixture cases. See
[`/root/Test/EverOS/use-cases/claude-code-plugin/hooks/hooks.json`](/root/Test/EverOS/use-cases/claude-code-plugin/hooks/hooks.json)
and
[`/root/Test/EverOS/tests/unit/test_benchmark_profile_injection.py`](/root/Test/EverOS/tests/unit/test_benchmark_profile_injection.py).

Zep's ADK `ZepContextTool` accepts a custom `context_builder` as its injection
extension point and skips context injection if that builder fails. It does not
route host `PreToolUse` events or define relevance thresholds, so it provided
no code to adapt for this gate. See
[`/root/Test/zep/integrations/adk/python/src/zep_adk/context_tool.py`](/root/Test/zep/integrations/adk/python/src/zep_adk/context_tool.py).

## Validation

```sh
.venv/bin/python -m pytest tests/test_injector_poisonbench.py -q
.venv/bin/ruff check commontrace/hook_kit.py tests/test_injector_poisonbench.py
.venv/bin/python scripts/dev.py docs --check
```
