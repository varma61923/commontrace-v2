# LangGraph pre-model memory hook

`commontrace.integrations.langgraph.create_pre_model_hook(memory)` returns a
synchronous hook for LangGraph's prebuilt agent API:

```python
from langgraph.prebuilt import create_react_agent
from commontrace.integrations.langgraph import create_pre_model_hook, record_outcome


def scope(config):
    return {"tenant": config["configurable"]["tenant_id"]}


agent = create_react_agent(
    model,
    tools,
    pre_model_hook=create_pre_model_hook(memory, recall_kwargs=scope),
    checkpointer=checkpointer,
    prompt="Stable system instructions.",
)
config = {"configurable": {"thread_id": "conversation", "tenant_id": "tenant-a"}}
result = agent.invoke({"messages": [("user", "How should retries work?")]}, config)
record_outcome(memory, config, state=result, recall_kwargs=scope, succeeded=True)
```

The hook reads the latest human message and recalls through the supplied
`CausalMemory`. It hashes the thread ID, human message ID, configured scope
labels and forwarded retrieval arguments into a causal turn identity. Tool
messages do not create new occasions; a second human message with identical
text does. `record_outcome(..., state=result, recall_kwargs=scope)` derives the
same identity to join outcomes to assignments. The older `with_lessons` wrapper
and `record_outcome` without `state` retain their original thread-level identity.

A bounded in-process cache (256 turns by default, configurable with `cache_size`)
reuses the retrieved context throughout the turn's tool loop, including empty
and failed retrievals. Its key partitions tenants and forwarded scope arguments.
Recreating the hook, a process restart or cache eviction may re-retrieve a turn;
the deterministic occasion ID remains the same. LangGraph assigns message IDs
through its message reducer. Custom graphs without IDs use the human message's
position and text; callers that trim such histories must provide stable message
IDs or `configurable.commontrace_turn_id` to distinguish turns.

The callback returns only `llm_input_messages`, so the read-only reference block
reaches each model call without accumulating in the conversation's `messages`
channel. LangGraph can checkpoint `llm_input_messages` as a separate auxiliary
channel; this is not a promise that the reference text is absent from the whole
checkpoint. A configured `prompt` keeps fixed system instructions ahead of the
temporary memory message. Missing identity and recall failures pass through the
original messages without memory. The hook forwards scope to the retrieval
callable; that callable must enforce authorization. No authorization decision is
made from client-supplied labels.

## Runtime gap found and fixed

On LangGraph 1.2.1, a callback parameter annotated `dict[str, Any] | None` is not
eligible for framework config injection. The old callback therefore received
its default `None` when invoked by a real graph, silently skipped recall, and
emitted an SDK annotation warning. The hook and legacy wrapper now annotate the
parameter `RunnableConfig`, which the runtime recognizes. An optional import
fallback keeps the module importable in a minimal CommonTrace installation.

The previous real-graph tests failed three retrieval assertions before this
change; all pass afterward. The dependency-free tests could not detect that
runtime failure. Additional native lifecycle tests run the real
`create_react_agent`, message reducer, tool dispatcher and `InMemorySaver`, with
a deterministic provider-free chat model. Both `invoke` and `ainvoke` execute
two model calls per human turn, retrieve once, then retrieve again for a second
identical human prompt. They verify scoped arguments, unchanged checkpoint
conversation history, one reference block per model call, and assignment/outcome
identity for distinct successful and failed turns. An unavailable store fails
open once per turn and is retried on the next human turn.

## Reproduce

Run from the repository root using an isolated environment:

```bash
python3 -m venv /tmp/commontrace-langgraph-live-venv
/tmp/commontrace-langgraph-live-venv/bin/pip install \
  langgraph==1.2.1 langgraph-prebuilt==1.1.0 langchain-core==1.6.9 pytest==9.1.1
/tmp/commontrace-langgraph-live-venv/bin/python -m pytest \
  tests/test_langgraph_premodel_hook.py \
  tests/test_integrations_langgraph.py \
  tests/test_optional_langgraph_lifecycle.py -q
```

Measured on 2026-10-12: 21 tests pass. The SDK emits three expected
`create_react_agent` deprecation warnings. The factory remains supported in the
validated version but is deprecated in favor of `langchain.agents.create_agent`;
this adapter targets the explicit LangGraph pre-model contract requested in the
build brief. No external provider transport, retrieval quality, prompt-cache
hit rate or sub-50-ms latency gate is established by these lifecycle tests.

## Source adaptation

Graphify traversal of the local competitor graphs identified the Zep integration
and Mem0 LangGraph guide, followed by direct source inspection:

- Zep revision `cfa2ab2bbc3c7085d636479b719bcf64608162cc`,
  `integrations/langgraph/python/src/zep_langgraph/hooks.py`: temporary system
  context via `llm_input_messages` and fail-open retrieval, rather than replacing
  a model node or appending context to permanent history. Zep's implementation
  re-fetches on each model call; CommonTrace adds per-turn reuse and causal IDs.
- Mem0 revision `b7ad69afda6b6ed030347c66d48a13e4de9dec08`,
  `docs/integrations/langgraph.mdx`: scoped memory retrieval inside a graph node.
- Installed official LangGraph source:
  `langgraph/prebuilt/chat_agent_executor.py` documents and implements the
  `pre_model_hook`/`llm_input_messages` contract, while
  `langgraph/_internal/_runnable.py` defines accepted config annotations.

This adapts the existing Zep pattern and retains CommonTrace's existing recall,
holdout and outcome path. It introduces no new persistence or storage backend.
