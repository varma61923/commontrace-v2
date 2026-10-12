# F2 `PostToolUseFailure` timeout recall follow-up

## Finding

After the informative-overlap admission change, the labelled
`PostToolUseFailure` event still missed the `failure-timeouts` lesson. The
payload error matched that lesson's explicit `timed out` error trigger, and its
BGE-small cosine was 0.633, above the existing 0.6 dense floor. The final
lexical gate rejected it because the error used “timed out” while the lesson
used “timeouts”; their token sets had no informative overlap and BM25 was zero.

## Change

For `PostToolUseFailure` only, an error containing the explicit token forms
`timed out`, `time out`, `timeout`, or `timeouts` adds both `timeout` and
`timeouts` to the ranking query tokens. The original query remains the dense
embedding input. Candidates still need positive sparse score, dense cosine at
least 0.6, informative topical overlap, and the existing event/tool/error/path
trigger checks. The normalization does not apply to prompt, pre-tool, or
compaction events.

This adapts the error evidence already available at the native boundary. The
local Mem0 Codex adapter determines failure from explicit `tool_response`
fields (`exit_code`, `isError`, `success`) and carries the failed response as
error evidence in its tool record. Hindsight's Codex transcript parser preserves
nonzero `exec_command_end.exit_code` in the corresponding `tool_result` text.
CommonTrace's Codex normalizer likewise promotes only explicit failed
`PostToolUse` results to canonical `PostToolUseFailure`; this follow-up does not
change event classification or host payload handling. Graphify queries were
used to locate the CommonTrace `HookKit.handle` path and the Mem0 Codex adapter
and event recorder before inspecting their source.

Source references:

- [`commontrace/hook_kit.py`](../../commontrace/hook_kit.py)
- [`commontrace/native_hooks.py`](../../commontrace/native_hooks.py)
- [`/root/Test/mem0/integrations/codex-plugin/hooks/adapter.py`](/root/Test/mem0/integrations/codex-plugin/hooks/adapter.py)
- [`/root/Test/mem0/integrations/codex-plugin/core/memory_core.py`](/root/Test/mem0/integrations/codex-plugin/core/memory_core.py)
- [`/root/Test/hindsight/hindsight-integrations/codex/scripts/lib/content.py`](/root/Test/hindsight/hindsight-integrations/codex/scripts/lib/content.py)
- [`next-generation-codex-post-tool-failure.md`](next-generation-codex-post-tool-failure.md)

## Validation

The focused unit test covers the explicit timeout trigger and verifies that a
same-event unrelated lesson remains out under a matching-vector test embedder.
`tests/test_injector_poisonbench.py` passes (13 tests), and Ruff plus
`git diff --check` pass for the touched Python files.

The cached BGE-small fixture was rerun offline with 20 repetitions:

| Event | Unique cases | Expected injections | Correct | Injections | Precision | Recall | Warm p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `PostToolUseFailure` | 5 | 60 | 60 | 60 | 1.000 | 1.000 | 8.35 ms |

Before this change, the same three positive cases produced 40 correct
injections from 60 expected (0.667 recall) at 1.000 precision. In the rerun,
all four semantic events together produced 380 correct injections from 380
expected at 1.000 precision and recall; overall warm p95 was 10.11 ms, with no
timeouts. The 29-case adversarial poison check still had zero successful poison
injections, and clean utility directives were admitted for all four events.

Reproduction:

```sh
FASTEMBED_CACHE_PATH=/tmp/commontrace-fastembed-cache COMMONTRACE_OFFLINE=1 \
  .venv/bin/python -m benchmarks.injector_bench \
  --model fastembed:BAAI/bge-small-en-v1.5 --repetitions 20 \
  --out /tmp/injector-event-f2-timeout-normalization-20.json
```

The fixture has only five unique failure cases, including three positive cases;
20 repetitions reuse those same cases. These maintainer labels demonstrate
component behavior only. They do not estimate production precision or recall,
cover other timeout phrasings, establish host delivery, or show coding-task
uplift.

The run used FastEmbed 0.8.0 and fixture SHA-256
`e806649518955a24abb77eb770eca2e84ecc0b649cff4ac312f66e87f752e12a`. The
source-bound benchmark SHA-256 was
`b23cdebad7ea2dcc0124dec7bfaa4dadb5735941e0776ffa27ecd04e45444235`; the
`commontrace/hook_kit.py` SHA-256 was
`18afe9bc5ff84c9baa0ac8b531a8084b15849588e236a42b9e17935ed3ced33f`.
